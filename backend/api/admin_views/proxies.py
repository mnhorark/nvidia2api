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
class ProxyListView(AdminRequiredMixin, APIView):

    def get(self, request):
        channel = current_channel(request)
        qs = channel.proxies.select_related('group').order_by('id')
        # 一条条件聚合同时拿到 Key 总数与可调度数：前端代理页的
        # "共 N 个 Key / 启用上限" 不再需要整拉 /api/admin/keys（千级 Key ≈
        # 128KB）只为算一个 length。
        n_total_keys, n_keys = proxy_service.key_counts(channel)
        max_allowed = max(n_keys - 1, 0)
        enabled = qs.filter(enabled=True).count()
        return Response({'results': ProxySerializer(qs, many=True).data, 'summary': {'channel': channel.slug, 'channel_id': channel.id, 'disable_proxy_unhealthy': channel.disable_proxy_unhealthy, 'nvidia_keys': n_keys, 'total_keys': n_total_keys, 'max_enabled_proxies': max_allowed, 'enabled_proxies': enabled, 'direct_routes': 1 if n_keys else 0, 'total_routes': enabled + (1 if n_keys else 0)}})

    def post(self, request):
        channel = current_channel(request)
        ser = ProxyWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not request.data.get('name'):
            ser.validated_data['name'] = f'代理 {channel.proxies.count() + 1:03d}'
        group = ser.validated_data.get('group')
        if group is not None and group.channel_id != channel.id:
            return admin_error('分组不属于当前渠道', 'bad_request', 400)
        p = Proxy.objects.create(channel=channel, **ser.validated_data)
        return Response(ProxySerializer(p).data, status=201)


class ProxyImportView(AdminRequiredMixin, APIView):

    def post(self, request):
        text = request.data.get('text', '')
        if not text.strip():
            return admin_error('text required', 'bad_request', 400)
        return Response(proxy_service.bulk_import_proxies(text, current_channel(request)))


class ProxyDetailView(AdminRequiredMixin, APIView):

    def _get(self, pk):
        try:
            return Proxy.objects.select_related('group').get(pk=pk)
        except Proxy.DoesNotExist:
            return None

    def patch(self, request, pk):
        p = self._get(pk)
        if not p:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        enabled, err = _require_bool(request.data, 'enabled')
        if err is not None:
            return err
        if enabled is not None:
            ok, msg = proxy_service.set_enabled(p, enabled)
            if not ok:
                return admin_error(msg, 'proxy_limit_exceeded', 400)
        if 'group' in request.data:
            gid = request.data['group']
            if gid in (None, ''):
                p.group = None
            else:
                g = ProxyGroup.objects.filter(pk=gid).first()
                if g is None:
                    return admin_error('分组不存在', 'bad_request', 400)
                if g.channel_id != p.channel_id:
                    return admin_error('分组不属于该代理所在渠道', 'bad_request', 400)
                p.group = g
        if 'port' in request.data:
            port, err = _require_int(request.data, 'port', minimum=1, maximum=65535)
            if err:
                return err
            p.port = port
        for f in ('name', 'protocol', 'host', 'username', 'password'):
            if f in request.data:
                setattr(p, f, request.data[f])
        p.save()
        return Response(ProxySerializer(p).data)

    def delete(self, request, pk):
        p = self._get(pk)
        if not p:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        p.delete()
        return Response(status=204)


class ProxyTestView(AdminRequiredMixin, APIView):

    def post(self, request, pk):
        try:
            p = Proxy.objects.get(pk=pk)
        except Proxy.DoesNotExist:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        return Response(proxy_service.run_async(check_proxy(p)))


class ProxyFetchIpView(ProxyTestView):
    pass


class ProxyTestAllView(AdminRequiredMixin, APIView):

    def post(self, request):
        channel = current_channel(request)
        return Response(proxy_service.run_async(check_all(channel)))


class ProxyBatchView(AdminRequiredMixin, APIView):
    """POST {ids: [...], action: "enable"|"disable"|"delete"|"test"|"group"}"""

    def post(self, request):
        channel = current_channel(request)
        ids = _parse_ids(request)
        action = request.data.get('action')
        if not ids or action not in ('enable', 'disable', 'delete', 'test', 'group'):
            return admin_error('ids 与合法 action 必填', 'bad_request', 400)
        qs = list(channel.proxies.filter(id__in=ids))
        if action == 'delete':
            channel.proxies.filter(id__in=ids).delete()
            return Response({'matched': len(qs), 'action': action})
        if action == 'test':
            result = proxy_service.run_async(check_all(channel, ids=[p.id for p in qs]))
            return Response({'matched': len(qs), 'action': action, **result})
        if action == 'group':
            gid = request.data.get('group_id')
            if gid in (None, '', 'null'):
                channel.proxies.filter(id__in=ids).update(group=None)
                return Response({'matched': len(qs), 'action': action, 'succeeded': len(qs)})
            try:
                gid = int(gid)
            except (TypeError, ValueError):
                return admin_error('group_id 非法', 'bad_request', 400)
            group = channel.proxy_groups.filter(pk=gid).first()
            if not group:
                return admin_error('分组不存在', 'not_found', 404)
            channel.proxies.filter(id__in=ids).update(group=group)
            return Response({'matched': len(qs), 'action': action, 'succeeded': len(qs), 'group_id': gid})
        done, skipped = (0, [])
        for p in qs:
            ok, msg = proxy_service.set_enabled(p, action == 'enable')
            if ok:
                done += 1
            else:
                skipped.append({'id': p.id, 'name': p.name, 'reason': msg})
        return Response({'matched': len(qs), 'action': action, 'succeeded': done, 'skipped': skipped})
