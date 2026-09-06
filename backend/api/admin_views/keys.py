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
class ChannelKeyListView(AdminRequiredMixin, APIView):

    def get(self, request):
        channel = current_channel(request)
        qs = channel.keys.order_by('id')
        return Response(ChannelKeySerializer(qs, many=True).data)

    def post(self, request):
        channel = current_channel(request)
        name = (request.data.get('name') or '').strip()
        key = (request.data.get('api_key') or '').strip()
        rpm_raw = request.data.get('rpm_limit')
        rpm = _parse_int(rpm_raw) if rpm_raw not in (None, '') else channel.default_rpm or 40
        if rpm is None:
            return _bad_param('rpm_limit')
        if not name:
            name = f'{channel.name} Key {channel.keys.count() + 1:03d}'
        allow_dup = bool(getattr(channel, 'allow_duplicate_keys', False))
        if key and (not allow_dup) and key_service._key_stored_in_channel(channel, key):
            return admin_error('duplicate key', 'duplicate', 400)
        rec = ChannelKey.objects.create(channel=channel, name=name, api_key=key, rpm_limit=rpm)
        return Response(ChannelKeySerializer(rec).data, status=201)


class ChannelKeyImportView(AdminRequiredMixin, APIView):

    def post(self, request):
        text = request.data.get('text', '')
        if not text.strip():
            return admin_error('text required', 'bad_request', 400)
        return Response(key_service.bulk_import_keys(text, current_channel(request)))


class ChannelKeyDetailView(AdminRequiredMixin, APIView):

    def _get(self, pk):
        try:
            return ChannelKey.objects.get(pk=pk)
        except ChannelKey.DoesNotExist:
            return None

    def get(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        data = ChannelKeySerializer(rec).data
        if request.query_params.get('reveal') == '1':
            from services.crypto import decrypt_secret
            data['api_key'] = decrypt_secret(rec.api_key)
            # 明文回看是"高权限且不可撤销"的动作：管理面只有一个静态共享
            # Token，泄漏后即可批量拉走全部上游 Key，事前拦不住就必须留下
            # 事后可查的流水（只记动作与来源，绝不记明文本身）。
            from services.audit_service import log_secret_access
            from apps.core.models import SecretAccessAction
            log_secret_access(SecretAccessAction.REVEAL_KEY,
                              request=request, channel=rec.channel, target=rec)
        return Response(data)

    def patch(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        name = request.data.get('name')
        if name:
            rec.name = name.strip()
        if 'rpm_limit' in request.data:
            rpm = _parse_int(request.data['rpm_limit'])
            if rpm is None:
                return _bad_param('rpm_limit')
            rec.rpm_limit = rpm
        action = request.data.get('action')
        enabled = request.data.get('enabled')
        if enabled is False or action == 'disable':
            rec.status = ChannelKeyStatus.DISABLED
        elif enabled is True or action == 'enable':
            rec.status = ChannelKeyStatus.AVAILABLE
            rec.cooldown_until = None
        rec.save()
        return Response(ChannelKeySerializer(rec).data)

    def delete(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        rec.delete()
        return Response(status=204)


class ChannelKeyTestView(AdminRequiredMixin, APIView):

    def post(self, request, pk):
        try:
            rec = ChannelKey.objects.get(pk=pk)
        except ChannelKey.DoesNotExist:
            return admin_error('not found', 'not_found', 404, 'not_found_error')
        return Response(key_service.test_key(rec))


class KeyBatchView(AdminRequiredMixin, APIView):
    """POST {ids: [...], action: "enable"|"disable"|"delete"|"test"|"set_rpm"}"""

    def post(self, request):
        channel = current_channel(request)
        ids = _parse_ids(request)
        action = request.data.get('action')
        if not ids or action not in ('enable', 'disable', 'delete', 'test', 'set_rpm'):
            return admin_error('ids 与合法 action 必填', 'bad_request', 400)
        qs = list(channel.keys.filter(id__in=ids))
        if action == 'delete':
            channel.keys.filter(id__in=ids).delete()
            return Response({'matched': len(qs), 'action': action})
        if action == 'test':
            results = []
            for k in qs:
                results.append({'id': k.id, 'name': k.name, **key_service.test_key(k)})
            return Response({'matched': len(qs), 'action': action, 'results': results})
        if action == 'set_rpm':
            rpm = _parse_int(request.data.get('rpm'))
            if rpm is None or rpm < 0:
                return _bad_param('rpm')
            changed = channel.keys.filter(id__in=ids).update(rpm_limit=rpm)
            return Response({'matched': len(qs), 'action': action, 'succeeded': changed})
        if action == 'enable':
            status = ChannelKeyStatus.AVAILABLE
            qs = [k for k in qs if k.status != ChannelKeyStatus.AVAILABLE]
        else:
            status = ChannelKeyStatus.DISABLED
            qs = [k for k in qs if k.status != ChannelKeyStatus.DISABLED]
        changed = 0
        for k in qs:
            k.status = status
            k.cooldown_until = None
            k.save(update_fields=['status', 'cooldown_until', 'updated_at'])
            changed += 1
        return Response({'matched': len(qs) + changed, 'action': action, 'succeeded': changed})
