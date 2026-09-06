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
class ChannelListView(AdminRequiredMixin, APIView):

    def get(self, request):
        from django.db.models import Count
        k_agg = {cid: (n, ok) for cid, n, ok in ChannelKey.objects.values('channel_id').annotate(n=Count('id'), ok=Count('id', filter=~Q(status__in=[ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID]))).values_list('channel_id', 'n', 'ok')}
        p_agg = {cid: (n, ok) for cid, n, ok in Proxy.objects.values('channel_id').annotate(n=Count('id'), ok=Count('id', filter=Q(enabled=True))).values_list('channel_id', 'n', 'ok')}
        m_agg = {cid: (n, ok) for cid, n, ok in AIModel.objects.values('channel_id').annotate(n=Count('id'), ok=Count('id', filter=Q(enabled=True))).values_list('channel_id', 'n', 'ok')}
        counts = {}
        for cid, (n, ok) in k_agg.items():
            counts.setdefault(cid, {})['key_count'] = n
            counts[cid]['enabled_key_count'] = ok
        for cid, (n, ok) in p_agg.items():
            counts.setdefault(cid, {})['proxy_count'] = n
            counts[cid]['enabled_proxy_count'] = ok
        for cid, (n, ok) in m_agg.items():
            counts.setdefault(cid, {})['model_count'] = n
            counts[cid]['enabled_model_count'] = ok
        channels = []
        for c in Channel.objects.order_by('id'):
            cc = counts.get(c.id, {})
            setattr(c, 'key_count', cc.get('key_count', 0))
            setattr(c, 'enabled_key_count', cc.get('enabled_key_count', 0))
            setattr(c, 'proxy_count', cc.get('proxy_count', 0))
            setattr(c, 'enabled_proxy_count', cc.get('enabled_proxy_count', 0))
            setattr(c, 'model_count', cc.get('model_count', 0))
            setattr(c, 'enabled_model_count', cc.get('enabled_model_count', 0))
            channels.append(c)
        return Response({'results': ChannelSerializer(channels, many=True).data, 'current': current_channel(request).slug})

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            return admin_error('name required', 'bad_request', 400)
        rpm, err = _require_int(request.data, 'default_rpm', minimum=0)
        if err:
            return err
        slug = (request.data.get('slug') or '').strip() or _slugify(name)
        if Channel.objects.filter(slug=slug).exists():
            return admin_error(f'渠道标识 {slug} 已存在', 'duplicate', 400)
        base_url = (request.data.get('base_url') or '').strip()
        if not base_url:
            return admin_error('base_url required', 'bad_request', 400)
        make_default, err = _require_bool(request.data, 'is_default')
        if err:
            return err
        make_default = bool(make_default) or not Channel.objects.exists()
        enabled = _parse_bool(request.data.get('enabled', True))
        if enabled is None:
            return _bad_bool('enabled')
        allow_dup = _parse_bool(request.data.get('allow_duplicate_keys', False))
        if allow_dup is None:
            return _bad_bool('allow_duplicate_keys')
        disable_key_invalid = _parse_bool(request.data.get('disable_key_invalid', False))
        if disable_key_invalid is None:
            return _bad_bool('disable_key_invalid')
        disable_proxy_unhealthy = _parse_bool(request.data.get('disable_proxy_unhealthy', False))
        if disable_proxy_unhealthy is None:
            return _bad_bool('disable_proxy_unhealthy')
        channel = Channel(name=name, slug=slug, base_url=base_url, chat_path=(request.data.get('chat_path') or '/chat/completions').strip(), models_path=(request.data.get('models_path') or '/models').strip(), key_prefix=(request.data.get('key_prefix') or '').strip(), auth_scheme=request.data.get('auth_scheme') or 'bearer', default_rpm=40 if rpm is None else rpm, enabled=enabled, is_default=make_default, notes=request.data.get('notes') or '', allow_duplicate_keys=allow_dup, disable_key_invalid=disable_key_invalid, disable_proxy_unhealthy=disable_proxy_unhealthy)
        channel.save()
        return Response(ChannelSerializer(channel).data, status=201)


class ChannelDetailView(AdminRequiredMixin, APIView):

    def _get(self, pk):
        try:
            return Channel.objects.get(pk=pk)
        except Channel.DoesNotExist:
            return None

    def patch(self, request, pk):
        channel = self._get(pk)
        if not channel:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        for f in ('name', 'base_url', 'chat_path', 'models_path', 'key_prefix', 'auth_scheme', 'notes'):
            if f in request.data:
                setattr(channel, f, (request.data[f] or '').strip() if isinstance(request.data[f], str) else request.data[f])
        for flag in ('allow_duplicate_keys', 'disable_key_invalid', 'disable_proxy_unhealthy'):
            err = _apply_bool(request.data, flag, channel)
            if err is not None:
                return err
        if 'disable_proxy_unhealthy' in request.data and channel.disable_proxy_unhealthy:
            channel.proxies.filter(status=ProxyStatus.UNHEALTHY).update(status=ProxyStatus.DEGRADED, cooldown_until=None)
            channel.proxies.exclude(cooldown_until=None).update(cooldown_until=None)
        if 'default_rpm' in request.data:
            rpm, err = _require_int(request.data, 'default_rpm', minimum=0)
            if err:
                return err
            channel.default_rpm = rpm
            if request.data.get('apply_rpm_to_keys'):
                channel.keys.update(rpm_limit=channel.default_rpm)
        err = _apply_bool(request.data, 'enabled', channel)
        if err is not None:
            return err
        if request.data.get('is_default'):
            channel.is_default = True
        elif 'is_default' in request.data and (not request.data['is_default']):
            if Channel.objects.exclude(pk=pk).filter(is_default=True).exists():
                channel.is_default = False
        channel.save()
        return Response(ChannelSerializer(channel).data)

    def delete(self, request, pk):
        channel = self._get(pk)
        if not channel:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        if channel.is_default and Channel.objects.count() == 1:
            return admin_error('至少保留一个渠道', 'last_channel', 400)
        channel.delete()
        channel_service.ensure_default_channel()
        return Response(status=204)


class ChannelTestView(AdminRequiredMixin, APIView):

    def post(self, request, pk):
        try:
            channel = Channel.objects.get(pk=pk)
        except Channel.DoesNotExist:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        return Response(channel_service.test_channel(channel))
