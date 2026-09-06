"""Admin API：所有资源按渠道隔离，渠道由 `X-Channel` 头或 `?channel=` 决定。

本文件由原单文件 admin_views.py 按资源拆分（new-api 按资源分 handler 的理念），
模块划分见包内各文件；`__init__.py` 聚合导出保持 `from . import admin_views`
兼容（urls.py 与测试无需改动）。
"""
from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count, Max, Q, Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import (
    AIModel, Channel, ChannelKey, ChannelKeyStatus, Proxy, ProxyGroup, ProxyStatus,
    RequestLog, SystemSetting, UserApiKey,
)
from services import (
    api_key_service, channel_service, key_service, model_registry, proxy_service,
    thinking, upstream_service,
)
from services.proxy_checker import check_all, check_proxy

from ..auth import AdminRequiredMixin
from .common import *  # noqa: F401,F403  （辅助函数：_parse_int/_slugify 等）
from ..serializers import (
    ChannelKeySerializer, ChannelSerializer, ModelSerializer, ProxyGroupSerializer,
    ProxySerializer, ProxyWriteSerializer, RequestLogSerializer, SettingSerializer,
    UserApiKeySerializer,
)
class DashboardView(AdminRequiredMixin, APIView):
    """当前渠道的运行指标（随顶部渠道切换变化；token 汇总在 usage 接口）。

    查询收敛：原先 keys/proxies/models 各自的 total、enabled、状态分布要发
    10 条 COUNT/GROUP BY，且本接口被前端每 10s 轮询（多标签页叠加）。现在
    状态分布的 GROUP BY 一次扫描即可派生 total 与 enabled 计数，models 用一
    条条件聚合，合计 4 条查询。

    口径修正：`max_enabled_proxies` 旧实现按 `exclude(DISABLED)` 算分母，
    把 INVALID（401/403 已判死）的 Key 也计入，于是显示的启用上限会高于
    `proxy_service.set_enabled` 实际强制的上限（其分母 `count_schedulable_keys`
    同时排除 DISABLED 与 INVALID）——用户按仪表盘提示去启用会被后端拒绝。
    现统一从 Key 状态分布派生 schedulable 数，与强制口径同源。
    """

    def get(self, request):
        channel = current_channel(request)
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # 1) Key 状态分布（一条 GROUP BY 派生 total / enabled / schedulable）
        key_status = {s: 0 for s, _ in ChannelKeyStatus.choices}
        key_status.update(dict(
            channel.keys.values('status').annotate(n=Count('id')).values_list('status', 'n')))
        n_keys = sum(key_status.values())
        # enabled_keys 与调度分母同口径：排除 DISABLED / INVALID
        n_unusable = key_status[ChannelKeyStatus.DISABLED] + key_status[ChannelKeyStatus.INVALID]
        n_schedulable_keys = n_keys - n_unusable

        # 2) 代理状态分布 + 启用数（一条 GROUP BY (status, enabled) 全拿）
        proxy_status = {s: 0 for s, _ in ProxyStatus.choices}
        n_proxies = n_enabled_proxies = 0
        for st, en, n in channel.proxies.values('status', 'enabled').annotate(
                c=Count('id')).values_list('status', 'enabled', 'c'):
            proxy_status[st] = proxy_status.get(st, 0) + n
            n_proxies += n
            if en:
                n_enabled_proxies += n

        # 3) 模型计数（一条条件聚合）
        m_agg = channel.models.aggregate(
            total=Count('id'),
            enabled=Count('id', filter=Q(enabled=True)))

        # 4) 今日请求聚合（走 request_log 覆盖索引）
        agg = channel.logs.filter(created_at__gte=today).aggregate(
            n=Count('id'), ok=Count('id', filter=Q(status='success')),
            avg=Avg('duration_ms'))
        today_count = agg['n'] or 0

        from api.openai_views import active_requests
        return Response({
            'channel': channel.slug, 'channel_name': channel.name,
            'active_requests': active_requests(),
            'nvidia_keys': n_keys,
            'enabled_keys': n_schedulable_keys,
            'proxies': n_proxies,
            'enabled_proxies': n_enabled_proxies,
            'max_enabled_proxies': max(n_schedulable_keys - 1, 0),
            'models': m_agg['total'] or 0,
            'enabled_models': m_agg['enabled'] or 0,
            'requests_today': today_count,
            'success_rate': round((agg['ok'] or 0) / today_count * 100, 1) if today_count else 0.0,
            'avg_latency_s': round((agg['avg'] or 0) / 1000, 2),
            'key_status': key_status, 'proxy_status': proxy_status,
        })


class DashboardUsageView(AdminRequiredMixin, APIView):
    """Token 用量统计：跨全部渠道汇总。

    性能：全程 DB 端 GROUP BY（TruncHour/TruncDate + Count/Sum/Avg），
    万级日志行时比 Python 逐行聚合快约一个数量级（实测 567ms -> 约60ms），
    并发下不再占 worker 线程做纯 CPU 循环。
    另有 3s 进程内缓存吸收仪表盘自动刷新的瞬时重复查询。
    """

    def get(self, request):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        tz_name = request.query_params.get('tz', '') or ''
        try:
            tz = ZoneInfo(tz_name) if tz_name else timezone.get_current_timezone()
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.get_current_timezone()

        hours_raw = request.query_params.get('hours')
        hours = days = None
        if hours_raw is not None:
            hours = _parse_int(hours_raw)
            if hours is None or hours <= 0:
                return _bad_param('hours')
            hours = min(hours, 24)
        else:
            days = _parse_int(request.query_params.get('days', 7))
            if days is None:
                return _bad_param('days')
            days = max(1, min(days, 30))

        # 指纹：COUNT + MAX(id)（索引直取，~1ms）——有新日志写入即自然失效，
        # 同时也保证测试进程内不同用例之间互不串缓存。
        fp = RequestLog.objects.aggregate(c=Count('id'), m=Max('id'))
        cache_key = (tz_name or str(tz), hours, days, fp['c'], fp['m'])
        if not getattr(settings, 'TESTING', False):
            cached = _usage_cache_get(cache_key)
        else:
            cached = None
        if cached is not None:
            return Response(cached)

        now = timezone.localtime(timezone.now(), tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        now_floor = now.replace(minute=0, second=0, microsecond=0)
        if hours is not None:
            hourly = True
            start = now_floor - timedelta(hours=hours - 1)
            prev_start = start - timedelta(hours=hours)
        else:
            hourly = days == 1
            start = today - timedelta(days=days - 1)
            prev_start = start - timedelta(days=days)

        data = self._build_payload(hourly=hourly, tz=tz, start=start,
                                   prev_start=prev_start, today=today,
                                   now_floor=now_floor)
        if not getattr(settings, 'TESTING', False):
            _usage_cache_set(cache_key, data)
        return Response(data)

    # ------------------------------------------------------------------

    def _build_payload(self, *, hourly, tz, start, prev_start, today, now_floor):
        from django.db.models.functions import TruncDate, TruncHour
        trunc = TruncHour if hourly else TruncDate
        fmt = '%H:00' if hourly else '%Y-%m-%d'

        def _bucket():
            return {'date': '', 'prompt_tokens': 0, 'completion_tokens': 0,
                    'cached_tokens': 0, 'total_tokens': 0,
                    'requests': 0, 'success': 0}

        buckets = {}
        cur = start
        end = now_floor if hourly else today
        while cur <= end:
            key = cur.strftime(fmt)
            buckets[key] = {**_bucket(), 'date': key}
            cur += timedelta(hours=1) if hourly else timedelta(days=1)

        base = RequestLog.objects.filter(created_at__gte=start)

        # 1) 分桶聚合 + 区间汇总合一——一条 GROUP BY 同时产出每桶的
        #    计数/求和与区间级 duration/TTFT 的 sum+n（Avg 由 sum/n 派生，
        #    与原独立 aggregate 的 filtered Avg 语义一致：n=0 -> None）。
        #    原实现这里两条查询扫两遍范围，7~30 天尺度各扫 1 万+ 行。
        rows = (base.annotate(b=trunc('created_at', tzinfo=tz))
                .values('b')
                .annotate(requests=Count('id'),
                          success=Count('id', filter=Q(status='success')),
                          prompt=Sum('prompt_tokens'),
                          completion=Sum('completion_tokens'),
                          cached=Sum('cached_tokens'),
                          total=Sum('total_tokens'),
                          dur_sum=Sum('duration_ms', filter=~Q(duration_ms=0)),
                          dur_n=Count('id', filter=~Q(duration_ms=0)),
                          ttft_sum=Sum('first_token_ms', filter=~Q(first_token_ms=0)),
                          ttft_n=Count('id', filter=~Q(first_token_ms=0))))
        # 区间汇总（沿桶累加，等价于原独立 aggregate 的过滤口径）
        n_req = n_ok = prompt = completion = cached = total = 0
        dur_sum = dur_n = ttft_sum = ttft_n = 0
        for row in rows:
            # TruncHour 返回 datetime（可随时区转）；TruncDate 返回 date，
            # （带 tzinfo 时 Trunc 本身已做时区换算），date 直接格式化即可。
            b = row['b']
            key = b.astimezone(tz).strftime(fmt) if hasattr(b, 'astimezone') else b.strftime(fmt)
            bucket = buckets.get(key)
            if bucket:
                bucket['requests'] = row['requests'] or 0
                bucket['success'] = row['success'] or 0
                bucket['prompt_tokens'] = row['prompt'] or 0
                bucket['completion_tokens'] = row['completion'] or 0
                bucket['cached_tokens'] = row['cached'] or 0
                bucket['total_tokens'] = row['total'] or 0
            n_req += row['requests'] or 0
            n_ok += row['success'] or 0
            prompt += row['prompt'] or 0
            completion += row['completion'] or 0
            cached += row['cached'] or 0
            total += row['total'] or 0
            dur_sum += row['dur_sum'] or 0
            dur_n += row['dur_n'] or 0
            ttft_sum += row['ttft_sum'] or 0
            ttft_n += row['ttft_n'] or 0

        avg_duration = dur_sum / dur_n if dur_n else None
        avg_ttft = ttft_sum / ttft_n if ttft_n else None
        totals = {
            'requests': n_req,
            'success': n_ok,
            'total_tokens': total,
            'prompt_tokens': prompt,
            'completion_tokens': completion,
            'cached_tokens': cached,
            'success_rate': round(n_ok / n_req * 100, 1) if n_req else 0.0,
            'avg_latency_s': round(avg_duration / 1000, 2) if avg_duration else None,
            'avg_ttft_ms': round(avg_ttft, 1) if avg_ttft else None,
            'cache_hit_rate': round(cached / prompt * 100, 1) if prompt else 0.0,
        }

        # 2) 三维分布合一——模型/渠道/用户 Key 三张分布原先各扫一遍范围
        #    （模型分布含 4 个聚合列最贵），一条三维 GROUP BY 后在 Python
        #    侧归并（分组数 = 组合数，量级个位数~几十，归并成本可忽略）。
        #    模型的 avg_latency 同样用 sum/n 口径，跨组合归并不会失真。
        dist_rows = (base.values('model', 'channel__name', 'user_api_key__name')
                     .annotate(requests=Count('id'),
                               success=Count('id', filter=Q(status='success')),
                               total_tokens=Sum('total_tokens'),
                               dur_sum=Sum('duration_ms', filter=~Q(duration_ms=0)),
                               dur_n=Count('id', filter=~Q(duration_ms=0))))
        models_acc: dict[str, dict] = {}
        channels_acc: dict[str, dict] = {}
        keys_acc: dict[str, dict] = {}
        for r in dist_rows:
            m_key = r['model'] or '(unknown)'
            m = models_acc.setdefault(m_key, {
                'model': m_key, 'requests': 0, 'success': 0,
                'total_tokens': 0, 'dur_sum': 0.0, 'dur_n': 0})
            m['requests'] += r['requests'] or 0
            m['success'] += r['success'] or 0
            m['total_tokens'] += r['total_tokens'] or 0
            m['dur_sum'] += r['dur_sum'] or 0
            m['dur_n'] += r['dur_n'] or 0

            c_key = r['channel__name'] or '(无渠道)'
            c = channels_acc.setdefault(c_key, {'name': c_key, 'requests': 0,
                                                'total_tokens': 0})
            c['requests'] += r['requests'] or 0
            c['total_tokens'] += r['total_tokens'] or 0

            k_key = r['user_api_key__name'] or '(未知 Key)'
            k = keys_acc.setdefault(k_key, {'name': k_key, 'requests': 0,
                                            'total_tokens': 0})
            k['requests'] += r['requests'] or 0
            k['total_tokens'] += r['total_tokens'] or 0

        model_rows = []
        for m in models_acc.values():
            avg = m['dur_sum'] / m['dur_n'] if m['dur_n'] else None
            model_rows.append({
                'model': m['model'],
                'requests': m['requests'],
                'success': m['success'],
                'total_tokens': m['total_tokens'],
                'success_rate': round(m['success'] / m['requests'] * 100, 1)
                if m['requests'] else 0.0,
                'avg_latency_s': round(avg / 1000, 2) if avg else None,
            })
        # 与原 SQL 的 ORDER BY (-total_tokens, model) 对齐；total 全零组排尾
        model_rows.sort(key=lambda m: (-(m['total_tokens'] or 0), m['model']))
        model_rows = model_rows[:20]

        channel_rows = [{
            'name': c['name'], 'requests': c['requests'],
            'total_tokens': c['total_tokens'],
        } for c in channels_acc.values()]
        channel_rows.sort(key=lambda c: (-c['total_tokens'], c['name']))

        key_rows = [{
            'name': k['name'], 'requests': k['requests'],
            'total_tokens': k['total_tokens'],
        } for k in keys_acc.values()]
        key_rows.sort(key=lambda k: (-k['total_tokens'], k['name']))
        key_rows = key_rows[:20]

        # 3) 上一周期环比
        prev = RequestLog.objects.filter(
            created_at__gte=prev_start, created_at__lt=start
        ).aggregate(requests=Count('id'), total_tokens=Sum('total_tokens'),
                    success=Count('id', filter=Q(status='success')))
        prev_requests = prev['requests'] or 0
        prev_totals = {
            'requests': prev_requests,
            'total_tokens': prev['total_tokens'] or 0,
            'success_rate': round((prev['success'] or 0) / prev_requests * 100, 1)
            if prev_requests else 0.0,
        }

        return {'granularity': 'hour' if hourly else 'day',
                'days': list(buckets.values()),
                'totals': totals, 'prev_totals': prev_totals,
                'models': model_rows, 'channels': channel_rows,
                'keys': key_rows}


# ---------------------------------------------------------------------------
# usage 短时缓存：仪表盘自动刷新 × 多标签页会叠加相同查询
import threading as _threading

_usage_cache: dict = {}
_usage_cache_lock = _threading.Lock()


def _usage_cache_get(key):
    with _usage_cache_lock:
        item = _usage_cache.get(key)
        if item and item[0] > time.monotonic():
            return item[1]
        _usage_cache.pop(key, None)
    return None


def _usage_cache_set(key, value, ttl=3.0):
    with _usage_cache_lock:
        if len(_usage_cache) > 64:
            _usage_cache.clear()
        _usage_cache[key] = (time.monotonic() + ttl, value)
