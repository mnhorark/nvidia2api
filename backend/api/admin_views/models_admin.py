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
class ModelListView(AdminRequiredMixin, APIView):

    def get(self, request):
        channel = current_channel(request)
        qs = channel.models.order_by('model_name')
        q = request.query_params.get('q')
        if q:
            qs = qs.filter(model_name__icontains=q)
        return Response(ModelSerializer(qs, many=True).data)

    def post(self, request):
        channel = current_channel(request)
        name = (request.data.get('model_name') or '').strip()
        if not name:
            return admin_error('model_name required', 'bad_request', 400)
        enabled = _parse_bool(request.data.get('enabled', False))
        if enabled is None:
            return _bad_bool('enabled')
        defaults = {'display_name': request.data.get('display_name', ''), 'alias': (request.data.get('alias') or '').strip(), 'aliases': _normalize_aliases(request.data.get('aliases')), 'description': request.data.get('description', ''), 'provider': request.data.get('provider') or channel.slug, 'endpoint': (request.data.get('endpoint') or '').strip(), 'enabled': enabled}
        rec, created = channel.models.get_or_create(model_name=name, defaults=defaults)
        if not created:
            for f in ('display_name', 'alias', 'aliases', 'description', 'endpoint', 'enabled'):
                if f in request.data and request.data[f] is not None:
                    setattr(rec, f, defaults[f])
        self._apply_proxy_group(rec, request)
        rec.save()
        return Response(ModelSerializer(rec).data, status=201 if created else 200)

    @staticmethod
    def _apply_proxy_group(rec, request):
        gid = request.data.get('proxy_group')
        if gid in (None, '', 'null'):
            if 'proxy_group' in request.data:
                rec.proxy_group = None
        else:
            try:
                gid = int(gid)
            except (TypeError, ValueError):
                return
            group = rec.channel.proxy_groups.filter(pk=gid).first()
            rec.proxy_group = group
        rec.save()


class ModelSyncView(AdminRequiredMixin, APIView):

    def post(self, request):
        channel_param = (request.data.get('channel') or '').strip()
        channel = channel_service.resolve(channel_param) if channel_param else current_channel(request)
        prune = str(request.data.get('prune') or '').lower() in ('1', 'true', 'yes')
        prune_only = str(request.data.get('prune_only') or '').lower() in ('1', 'true', 'yes')
        try:
            return Response(upstream_service.sync_models(
                channel, prune=prune, prune_only=prune_only))
        except ValueError as exc:
            msg = str(exc)
            code = 'no_available_key' if msg == 'no_available_key' else 'upstream_error'
            status = 503 if code == 'no_available_key' else 502
            return admin_error(msg, code, status)


class ModelDetailView(AdminRequiredMixin, APIView):

    def _get(self, pk):
        try:
            return AIModel.objects.get(pk=pk)
        except AIModel.DoesNotExist:
            return None

    def patch(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        if 'aliases' in request.data:
            rec.aliases = _normalize_aliases(request.data['aliases'])
        if 'route_priority' in request.data:
            priority, err = _require_int(request.data, 'route_priority')
            if err:
                return err
            rec.route_priority = priority
        err = _apply_bool(request.data, 'enabled', rec)
        if err is not None:
            return err
        for f in ('display_name', 'alias', 'description', 'status', 'endpoint'):
            if f in request.data:
                if f == 'endpoint':
                    rec.endpoint = (request.data[f] or '').strip()
                else:
                    setattr(rec, f, request.data[f])
        if 'proxy_group' in request.data:
            gid = request.data.get('proxy_group')
            if gid in (None, '', 'null'):
                rec.proxy_group = None
            else:
                try:
                    gid = int(gid)
                except (TypeError, ValueError):
                    gid = None
                rec.proxy_group = rec.channel.proxy_groups.filter(pk=gid).first() if gid is not None else None
        rec.save()
        return Response(ModelSerializer(rec).data)

    def delete(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        rec.delete()
        return Response(status=204)


class ModelBatchView(AdminRequiredMixin, APIView):
    """POST {ids: [...], action: "enable"|"disable"|"delete"}"""

    def post(self, request):
        channel = current_channel(request)
        ids = _parse_ids(request)
        action = request.data.get('action')
        if not ids or action not in ('enable', 'disable', 'delete'):
            return admin_error('ids 与合法 action 必填', 'bad_request', 400)
        qs = channel.models.filter(id__in=ids)
        matched = qs.count()
        if action == 'delete':
            qs.delete()
        else:
            qs.update(enabled=action == 'enable')
        model_registry.invalidate()
        return Response({'matched': matched, 'action': action})
