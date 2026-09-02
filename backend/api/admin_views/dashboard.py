"""Admin API：所有资源按渠道隔离，渠道由 `X-Channel` 头或 `?channel=` 决定。

本文件由原单文件 admin_views.py 按资源拆分（new-api 按资源分 handler 的理念），
模块划分见包内各文件；`__init__.py` 聚合导出保持 `from . import admin_views`
兼容（urls.py 与测试无需改动）。
"""
from __future__ import annotations

import time
from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
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

    返回按天分桶、区间汇总、上一周期环比、按模型分布、按渠道分布。
    """

    def get(self, request):
        tz = request.query_params.get('tz', '') or None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            tz = ZoneInfo(tz) if tz else timezone.get_current_timezone()
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.get_current_timezone()
        now = timezone.localtime(timezone.now(), tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        now_floor = now.replace(minute=0, second=0, microsecond=0)

        # 优先 hours 模式：最近 N 小时（整点对齐，支持跨天），例如 hours=5
        hours_raw = request.query_params.get('hours')
        if hours_raw is not None:
            hours = _parse_int(hours_raw)
            if hours is None or hours <= 0:
                return _bad_param('hours')
            hours = min(hours, 24)
            hourly = True
            start = now_floor - timedelta(hours=hours - 1)
            prev_start = start - timedelta(hours=hours)
        else:
            days_raw = request.query_params.get('days', 7)
            days = _parse_int(days_raw)
            if days is None:
                return _bad_param('days')
            days = max(1, min(days, 30))
            hourly = days == 1
            start = today - timedelta(days=days - 1)
            prev_start = start - timedelta(days=days)

        def _bucket() -> dict:
            return {'date': '', 'prompt_tokens': 0, 'completion_tokens': 0, 'cached_tokens': 0, 'total_tokens': 0, 'requests': 0, 'success': 0}
        buckets: dict = {}
        if hourly:
            cur = start
            while cur <= now_floor:
                key = cur.strftime('%H:00')
                buckets[key] = {**_bucket(), 'date': key}
                cur += timedelta(hours=1)
        else:
            cur = start
            while cur <= today:
                key = cur.strftime('%Y-%m-%d')
                buckets[key] = {**_bucket(), 'date': key}
                cur += timedelta(days=1)
        logs = RequestLog.objects.filter(created_at__gte=start).values('created_at', 'model', 'prompt_tokens', 'completion_tokens', 'cached_tokens', 'total_tokens', 'status', 'duration_ms', 'first_token_ms', 'channel__name', 'user_api_key__name').iterator(chunk_size=2000)
        totals = {'requests': 0, 'success': 0, 'total_tokens': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'cached_tokens': 0}
        models: dict[str, dict] = {}
        channels: dict[str, dict] = {}
        keys: dict[str, dict] = {}
        sum_duration = sum_ttft = 0.0
        n_duration = n_ttft = 0
        for row in logs:
            ok = row['status'] == 'success'
            ts = timezone.localtime(row['created_at'], tz)
            key = ts.strftime('%H:00') if hourly else ts.strftime('%Y-%m-%d')
            b = buckets.get(key)
            if b:
                b['prompt_tokens'] += row['prompt_tokens'] or 0
                b['completion_tokens'] += row['completion_tokens'] or 0
                b['cached_tokens'] += row['cached_tokens'] or 0
                b['total_tokens'] += row['total_tokens'] or 0
                b['requests'] += 1
                if ok:
                    b['success'] += 1
            totals['requests'] += 1
            totals['prompt_tokens'] += row['prompt_tokens'] or 0
            totals['completion_tokens'] += row['completion_tokens'] or 0
            totals['cached_tokens'] += row['cached_tokens'] or 0
            totals['total_tokens'] += row['total_tokens'] or 0
            if ok:
                totals['success'] += 1
            if row['duration_ms']:
                sum_duration += row['duration_ms']
                n_duration += 1
            if row['first_token_ms']:
                sum_ttft += row['first_token_ms']
                n_ttft += 1
            name = row['model'] or '(unknown)'
            m = models.setdefault(name, {'model': name, 'requests': 0, 'success': 0, 'total_tokens': 0, '_duration': 0.0, '_n': 0})
            m['requests'] += 1
            m['total_tokens'] += row['total_tokens'] or 0
            if ok:
                m['success'] += 1
            if row['duration_ms']:
                m['_duration'] += row['duration_ms']
                m['_n'] += 1
            cname = row['channel__name'] or '(无渠道)'
            c = channels.setdefault(cname, {'name': cname, 'requests': 0, 'total_tokens': 0})
            c['requests'] += 1
            c['total_tokens'] += row['total_tokens'] or 0
            kname = row['user_api_key__name'] or '(未知 Key)'
            k = keys.setdefault(kname, {'name': kname, 'requests': 0, 'total_tokens': 0})
            k['requests'] += 1
            k['total_tokens'] += row['total_tokens'] or 0
        model_rows = []
        for m in models.values():
            n = m.pop('_n')
            dur = m.pop('_duration')
            model_rows.append({**m, 'success_rate': round(m['success'] / m['requests'] * 100, 1) if m['requests'] else 0.0, 'avg_latency_s': round(dur / n / 1000, 2) if n else None})
        model_rows.sort(key=lambda r: (-r['total_tokens'], r['model']))
        channel_rows = sorted(channels.values(), key=lambda r: -r['total_tokens'])
        prev = RequestLog.objects.filter(created_at__gte=prev_start, created_at__lt=start).aggregate(requests=Count('id'), total_tokens=Sum('total_tokens'), success=Count('id', filter=Q(status='success')))
        totals.update({'success_rate': round(totals['success'] / totals['requests'] * 100, 1) if totals['requests'] else 0.0, 'avg_latency_s': round(sum_duration / n_duration / 1000, 2) if n_duration else None, 'avg_ttft_ms': round(sum_ttft / n_ttft, 1) if n_ttft else None, 'cache_hit_rate': round(totals['cached_tokens'] / totals['prompt_tokens'] * 100, 1) if totals['prompt_tokens'] else 0.0})
        prev_requests = prev['requests'] or 0
        prev_totals = {'requests': prev_requests, 'total_tokens': prev['total_tokens'] or 0, 'success_rate': round((prev['success'] or 0) / prev_requests * 100, 1) if prev_requests else 0.0}
        return Response({'granularity': 'hour' if hourly else 'day', 'days': list(buckets.values()), 'totals': totals, 'prev_totals': prev_totals, 'models': model_rows[:20], 'channels': channel_rows, 'keys': sorted(keys.values(), key=lambda r: -r['total_tokens'])[:20]})
