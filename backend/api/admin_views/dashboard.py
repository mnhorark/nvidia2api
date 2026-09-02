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
    """当前渠道的运行指标（随顶部渠道切换变化；token 汇总在 usage 接口）。"""

    def get(self, request):
        channel = current_channel(request)
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        keys = channel.keys.all()
        proxies = channel.proxies.all()
        logs_today = channel.logs.filter(created_at__gte=today)
        # 状态分布：一次 GROUP BY 代替 5/6 次逐状态 COUNT（仪表盘每次轮询约减 10 次查询）
        key_status = {s: 0 for s, _ in ChannelKeyStatus.choices}
        key_status.update(dict(
            channel.keys.values('status').annotate(n=Count('id')).values_list('status', 'n')))
        proxy_status = {s: 0 for s, _ in ProxyStatus.choices}
        proxy_status.update(dict(
            channel.proxies.values('status').annotate(n=Count('id')).values_list('status', 'n')))
        agg = logs_today.aggregate(n=Count('id'), ok=Count('id', filter=Q(status='success')), avg=Avg('duration_ms'))
        today_count = agg['n'] or 0
        n_active_keys = keys.exclude(status=ChannelKeyStatus.DISABLED).count()
        from api.openai_views import active_requests
        return Response({'channel': channel.slug, 'channel_name': channel.name, 'active_requests': active_requests(), 'nvidia_keys': keys.count(), 'enabled_keys': keys.exclude(status__in=[ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID]).count(), 'proxies': proxies.count(), 'enabled_proxies': proxies.filter(enabled=True).count(), 'max_enabled_proxies': max(n_active_keys - 1, 0), 'models': channel.models.count(), 'enabled_models': channel.models.filter(enabled=True).count(), 'requests_today': today_count, 'success_rate': round((agg['ok'] or 0) / today_count * 100, 1) if today_count else 0.0, 'avg_latency_s': round((agg['avg'] or 0) / 1000, 2), 'key_status': key_status, 'proxy_status': proxy_status})


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

        # 1) 分桶聚合——一条 GROUP BY 完成
        rows = (base.annotate(b=trunc('created_at', tzinfo=tz))
                .values('b')
                .annotate(requests=Count('id'),
                          success=Count('id', filter=Q(status='success')),
                          prompt=Sum('prompt_tokens'),
                          completion=Sum('completion_tokens'),
                          cached=Sum('cached_tokens'),
                          total=Sum('total_tokens')))
        for row in rows:
            # TruncHour 返回 datetime（可随时区转）；TruncDate 返回 date，
            # （带 tzinfo 时 Trunc 本身已做时区换算），date 直接格式化即可。
            b = row['b']
            key = b.astimezone(tz).strftime(fmt) if hasattr(b, 'astimezone') else b.strftime(fmt)
            b = buckets.get(key)
            if not b:
                continue
            b['requests'] = row['requests'] or 0
            b['success'] = row['success'] or 0
            b['prompt_tokens'] = row['prompt'] or 0
            b['completion_tokens'] = row['completion'] or 0
            b['cached_tokens'] = row['cached'] or 0
            b['total_tokens'] = row['total'] or 0

        # 2) 区间汇总
        agg = base.aggregate(
            requests=Count('id'),
            success=Count('id', filter=Q(status='success')),
            prompt=Sum('prompt_tokens'), completion=Sum('completion_tokens'),
            cached=Sum('cached_tokens'), total=Sum('total_tokens'),
            avg_duration=Avg('duration_ms', filter=~Q(duration_ms=0)),
            avg_ttft=Avg('first_token_ms', filter=~Q(first_token_ms=0)),
        )
        n_req = agg['requests'] or 0
        totals = {
            'requests': n_req,
            'success': agg['success'] or 0,
            'total_tokens': agg['total'] or 0,
            'prompt_tokens': agg['prompt'] or 0,
            'completion_tokens': agg['completion'] or 0,
            'cached_tokens': agg['cached'] or 0,
            'success_rate': round((agg['success'] or 0) / n_req * 100, 1) if n_req else 0.0,
            'avg_latency_s': round((agg['avg_duration'] or 0) / 1000, 2) if agg['avg_duration'] is not None else None,
            'avg_ttft_ms': round(agg['avg_ttft'] or 0, 1) if agg['avg_ttft'] is not None else None,
            'cache_hit_rate': round((agg['cached'] or 0) / agg['prompt'] * 100, 1) if agg['prompt'] else 0.0,
        }

        # 3) 模型分布（Top 20）
        model_rows = list(
            base.values('model')
            .annotate(requests=Count('id'),
                      success=Count('id', filter=Q(status='success')),
                      total_tokens=Sum('total_tokens'),
                      avg_duration=Avg('duration_ms', filter=~Q(duration_ms=0)))
            .order_by('-total_tokens', 'model')[:20])
        for m in model_rows:
            m['model'] = m['model'] or '(unknown)'
            m['total_tokens'] = m['total_tokens'] or 0
            m['success_rate'] = round(m['success'] / m['requests'] * 100, 1) if m['requests'] else 0.0
            avg = m.pop('avg_duration')
            m['avg_latency_s'] = round(avg / 1000, 2) if avg else None

        # 4) 渠道分布
        channel_rows = list(
            base.values('channel__name')
            .annotate(requests=Count('id'), total_tokens=Sum('total_tokens'))
            .order_by('-total_tokens'))
        for c in channel_rows:
            c['name'] = c.pop('channel__name') or '(无渠道)'
            c['total_tokens'] = c['total_tokens'] or 0

        # 5) 用户 Key 分布（Top 20）
        key_rows = list(
            base.values('user_api_key__name')
            .annotate(requests=Count('id'), total_tokens=Sum('total_tokens'))
            .order_by('-total_tokens')[:20])
        for k in key_rows:
            k['name'] = k.pop('user_api_key__name') or '(未知 Key)'
            k['total_tokens'] = k['total_tokens'] or 0

        # 6) 上一周期环比
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
