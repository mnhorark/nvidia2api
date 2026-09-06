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
class SettingsView(AdminRequiredMixin, APIView):

    def get(self, request):
        from services import sysconfig
        channel = current_channel(request)
        return Response({'channel': channel.slug, 'settings': sysconfig.all_params(channel)})

    def patch(self, request):
        from services import sysconfig
        channel = current_channel(request)
        updates = request.data.get('settings')
        if not isinstance(updates, dict):
            key = request.data.get('key')
            if not key:
                return admin_error('settings or key required', 'bad_request', 400,
                           'invalid_request_error')
            updates = {key: request.data.get('value')}
        sysconfig.set_params(updates, channel)
        return Response({'channel': channel.slug, 'settings': sysconfig.all_params(channel)})

    def delete(self, request):
        """清空当前渠道的覆盖值，回落到默认。"""
        from services import sysconfig
        channel = current_channel(request)
        keys = request.query_params.get('keys')
        sysconfig.reset_params(keys.split(',') if keys else None, channel)
        return Response({'channel': channel.slug, 'settings': sysconfig.all_params(channel)})
