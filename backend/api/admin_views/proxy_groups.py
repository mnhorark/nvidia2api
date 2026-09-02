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
class ProxyGroupListView(AdminRequiredMixin, APIView):

    def get(self, request):
        channel = current_channel(request)
        qs = channel.proxy_groups.annotate(proxy_count=Count('proxies')).order_by('id')
        return Response(ProxyGroupSerializer(qs, many=True).data)

    def post(self, request):
        channel = current_channel(request)
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': {'message': 'name required', 'code': 'bad_request'}}, status=400)
        if channel.proxy_groups.filter(name=name).exists():
            return Response({'error': {'message': 'duplicate group', 'code': 'duplicate'}}, status=400)
        g = ProxyGroup.objects.create(channel=channel, name=name, description=request.data.get('description', ''), country=request.data.get('country', ''), enabled=request.data.get('enabled', True))
        data = ProxyGroupSerializer(g).data
        data['proxy_count'] = 0
        return Response(data, status=201)


class ProxyGroupDetailView(AdminRequiredMixin, APIView):

    def _get(self, pk):
        try:
            return ProxyGroup.objects.get(pk=pk)
        except ProxyGroup.DoesNotExist:
            return None

    def patch(self, request, pk):
        g = self._get(pk)
        if not g:
            return Response({'detail': 'not found'}, status=404)
        for f in ('name', 'description', 'country', 'enabled'):
            if f in request.data:
                setattr(g, f, request.data[f])
        g.save()
        data = ProxyGroupSerializer(g).data
        data['proxy_count'] = Proxy.objects.filter(group=g).count()
        return Response(data)

    def delete(self, request, pk):
        g = self._get(pk)
        if not g:
            return Response({'detail': 'not found'}, status=404)
        Proxy.objects.filter(group=g).update(group=None)
        g.delete()
        return Response(status=204)
