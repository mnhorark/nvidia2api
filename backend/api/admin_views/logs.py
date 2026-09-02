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
    ProxySerializer, ProxyWriteSerializer, RequestLogSerializer,
    RequestLogListSerializer, SettingSerializer,
    UserApiKeySerializer,
)
class LogListView(AdminRequiredMixin, APIView):

    def get(self, request):
        channel = current_channel(request)
        qs = channel.logs.order_by('-id')
        model = request.query_params.get('model')
        status = request.query_params.get('status')
        if model:
            qs = qs.filter(model=model)
        if status:
            qs = qs.filter(status=status)
        limit_raw = request.query_params.get('limit')
        limit = _parse_int(limit_raw) if limit_raw not in (None, '') else 100
        if limit is None:
            return _bad_param('limit')
        limit = max(1, min(limit, 500))
        offset_raw = request.query_params.get('offset')
        offset = _parse_int(offset_raw) if offset_raw not in (None, '') else 0
        if offset is None:
            return _bad_param('offset')
        offset = max(offset, 0)
        total = qs.count()
        # 列表用轻量序列化：不带上 routes/thinking 等高成本明细字段
        page = list(qs[offset:offset + limit])
        return Response({'results': RequestLogListSerializer(page, many=True).data, 'channel': channel.slug, 'total': total, 'limit': limit, 'offset': offset, 'has_more': offset + len(page) < total})


class LogDetailView(AdminRequiredMixin, APIView):
    """单条日志全文（含线路竞速明细与思考参数），按展开时懒加载，避免列表轮询重复搬运大字段。"""

    def get(self, request, pk):
        channel = current_channel(request)
        log = channel.logs.filter(pk=pk).first()
        if log is None:
            return Response({'error': 'log_not_found'}, status=404)
        return Response(RequestLogSerializer(log).data)


class LogCleanView(AdminRequiredMixin, APIView):
    """清理过期请求日志。默认清理当前渠道；`all=1` 清理所有渠道。

    `days` 可显式覆盖系统参数 log_retention_days；0 表示本次不清理。
    """

    def post(self, request):
        from services import cleanup
        all_channels = str(request.data.get('all') or '').lower() in ('1', 'true', 'yes')
        days_raw = request.data.get('days')
        days = _parse_int(days_raw) if days_raw not in (None, '') else None
        if days is None and days_raw not in (None, ''):
            return _bad_param('days')
        channel = None if all_channels else current_channel(request)
        result = cleanup.clean_old_logs(days=days, channel=channel)
        return Response(result)
