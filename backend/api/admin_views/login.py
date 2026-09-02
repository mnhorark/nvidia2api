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
class LoginView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        u = request.data.get('username', '')
        p = request.data.get('password', '')
        from django.conf import settings
        import hmac
        ok_user = hmac.compare_digest(str(u), str(settings.ADMIN_USERNAME))
        ok_pass = hmac.compare_digest(str(p), str(settings.ADMIN_PASSWORD))
        if ok_user and ok_pass:
            from ..auth import valid_admin_tokens
            tokens = valid_admin_tokens()
            return Response({'token': tokens[0] if tokens else ''})
        if _login_fail_exceeded(_login_client_key(request)):
            return Response({'detail': 'Too many failed attempts, try again later'}, status=429)
        return Response({'detail': 'Invalid credentials'}, status=401)
