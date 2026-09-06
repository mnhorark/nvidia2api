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
class UserApiKeyListView(AdminRequiredMixin, APIView):
    """用户 Key 是平台级的，跨渠道共享。"""

    def get(self, request):
        return Response(UserApiKeySerializer(UserApiKey.objects.order_by('-id'), many=True).data)

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            return admin_error('name required', 'bad_request', 400)
        rl_raw = request.data.get('rate_limit')
        rate_limit = _parse_int(rl_raw) if rl_raw not in (None, '') else 0
        if rate_limit is None:
            return _bad_param('rate_limit')
        quota_raw = request.data.get('quota')
        quota = _parse_int(quota_raw) if quota_raw not in (None, '') else 0
        if quota is None or quota < 0:
            return _bad_param('quota')
        rec, raw = api_key_service.create_key(name, rate_limit=rate_limit, quota=quota)
        data = UserApiKeySerializer(rec).data
        data['key'] = raw
        return Response(data, status=201)


class UserApiKeyDetailView(AdminRequiredMixin, APIView):

    def _get(self, pk):
        try:
            return UserApiKey.objects.get(pk=pk)
        except UserApiKey.DoesNotExist:
            return None

    def patch(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        err = _apply_bool(request.data, 'enabled', rec)
        if err is not None:
            return err
        if 'rate_limit' in request.data:
            rl, err = _require_int(request.data, 'rate_limit', minimum=0)
            if err:
                return err
            rec.rate_limit = rl
        if 'quota' in request.data:
            q, err = _require_int(request.data, 'quota', minimum=0)
            if err:
                return err
            rec.quota = q
        if 'name' in request.data:
            rec.name = request.data['name']
        rec.save()
        return Response(UserApiKeySerializer(rec).data)

    def delete(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        rec.delete()
        return Response(status=204)
