"""Admin API：所有资源按渠道隔离，渠道由 `X-Channel` 头或 `?channel=` 决定。"""
from __future__ import annotations

import threading
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

from .auth import AdminRequiredMixin
from .serializers import (
    ChannelKeySerializer, ChannelSerializer, ModelSerializer, ProxyGroupSerializer,
    ProxySerializer, ProxyWriteSerializer, RequestLogSerializer, SettingSerializer,
    UserApiKeySerializer,
)


def current_channel(request) -> Channel:
    return channel_service.resolve_from_request(request)


def _parse_int(value):
    """宽松转 int；非法值返回 None（由调用方决定返回 400）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bad_param(name: str) -> Response:
    return Response({"error": {"message": f"参数 {name} 必须是整数",
                               "code": "bad_request"}}, status=400)


def _bad_bool(name: str) -> Response:
    return Response({"error": {"message": f"参数 {name} 必须是布尔值",
                               "code": "bad_request"}}, status=400)


_TRUE_LITERALS = {"1", "true", "yes", "on"}
_FALSE_LITERALS = {"0", "false", "no", "off", ""}


def _parse_bool(value):
    """显式解析布尔值；无法识别返回 None。

    不能用 `bool(value)`：Python 里 `bool("false")` 是 True，前端传字符串
    "false" 意为关闭，却会被静默当成开启。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    return None


def _require_bool(data: dict, name: str):
    """从请求数据取布尔字段；非法返回 (None, error_response)。"""
    if name not in data:
        return None, None
    parsed = _parse_bool(data[name])
    if parsed is None:
        return None, _bad_bool(name)
    return parsed, None


def _apply_bool(data: dict, name: str, obj, field: str | None = None):
    """把请求里的布尔字段写到对象上；非法返回 error_response，成功返回 None。"""
    parsed, err = _require_bool(data, name)
    if err is not None:
        return err
    if parsed is not None:
        setattr(obj, field or name, parsed)
    return None


def _require_int(data: dict, name: str, *, minimum: int | None = None,
                 maximum: int | None = None):
    """从请求数据取整数字段；非法/越界返回 (None, error_response)。"""
    if name not in data:
        return None, None
    parsed = _parse_int(data[name])
    if parsed is None:
        return None, _bad_param(name)
    if minimum is not None and parsed < minimum:
        return None, Response({"error": {"message": f"参数 {name} 不能小于 {minimum}",
                                         "code": "bad_request"}}, status=400)
    if maximum is not None and parsed > maximum:
        return None, Response({"error": {"message": f"参数 {name} 不能大于 {maximum}",
                                         "code": "bad_request"}}, status=400)
    return parsed, None


# 登录接口内存限流：每来源每分钟最多 10 次失败
_LOGIN_FAIL_LIMIT = 10
_LOGIN_FAIL_WINDOW = 60.0
# 清理节流：桶数量超过该值时才做一次全量清扫，避免每次失败都 O(n) 遍历
_LOGIN_FAIL_SWEEP_THRESHOLD = 256
_login_fail_lock = threading.Lock()
_login_fail_bucket: dict[str, list[float]] = {}


def _login_client_key(request) -> str:
    """限流维度：反代后 REMOTE_ADDR 全是网关地址，全员共桶会被他人拖累锁定。

    取 X-Forwarded-For 首跳参与分桶，使反代后的不同真实客户端各自计数。
    该值可被伪造，因此只作为"防误伤"的可用性措施，不作为防爆破的唯一手段
    ——真正的防线是强口令（生产环境已由 settings 门禁强制非默认凭据）。
    """
    remote = request.META.get("REMOTE_ADDR", "") or "unknown"
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "") or ""
    hop = xff.split(",")[0].strip() if xff else ""
    return f"{remote}|{hop}" if hop else remote


def _login_fail_exceeded(key: str) -> bool:
    now = time.monotonic()
    with _login_fail_lock:
        bucket = [t for t in _login_fail_bucket.get(key, []) if now - t < _LOGIN_FAIL_WINDOW]
        if len(bucket) >= _LOGIN_FAIL_LIMIT:
            _login_fail_bucket[key] = bucket
            return True
        bucket.append(now)
        _login_fail_bucket[key] = bucket
        # 过期桶不清会慢性膨胀：源 IP 多变时（尤其带 XFF 分桶后）增长更快
        if len(_login_fail_bucket) > _LOGIN_FAIL_SWEEP_THRESHOLD:
            for k in [k for k, v in _login_fail_bucket.items()
                      if not v or now - v[-1] >= _LOGIN_FAIL_WINDOW]:
                _login_fail_bucket.pop(k, None)
        return False


class LoginView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        u = request.data.get("username", "")
        p = request.data.get("password", "")
        from django.conf import settings
        import hmac
        # 常量时间比较：普通 `==` 在首字符不同即返回，可被计时侧信道逐字节猜口令
        ok_user = hmac.compare_digest(str(u), str(settings.ADMIN_USERNAME))
        ok_pass = hmac.compare_digest(str(p), str(settings.ADMIN_PASSWORD))
        if ok_user and ok_pass:
            return Response({"token": settings.ADMIN_TOKEN})
        if _login_fail_exceeded(_login_client_key(request)):
            return Response({"detail": "Too many failed attempts, try again later"},
                            status=429)
        return Response({"detail": "Invalid credentials"}, status=401)


# ---------------------------------------------------------------- channels

class ChannelListView(AdminRequiredMixin, APIView):
    def get(self, request):
        # 分组计数代替多 join 的 annotate：SQLite 上多表 join+distinct 很慢
        from django.db.models import Count
        k_agg = {cid: (n, ok) for cid, n, ok in
                 ChannelKey.objects.values("channel_id").annotate(
                     n=Count("id"),
                     ok=Count("id", filter=~Q(status__in=[
                         ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID])),
                 ).values_list("channel_id", "n", "ok")}
        p_agg = {cid: (n, ok) for cid, n, ok in
                 Proxy.objects.values("channel_id").annotate(
                     n=Count("id"),
                     ok=Count("id", filter=Q(enabled=True)),
                 ).values_list("channel_id", "n", "ok")}
        m_agg = {cid: (n, ok) for cid, n, ok in
                 AIModel.objects.values("channel_id").annotate(
                     n=Count("id"),
                     ok=Count("id", filter=Q(enabled=True)),
                 ).values_list("channel_id", "n", "ok")}

        counts = {}
        for cid, (n, ok) in k_agg.items():
            counts.setdefault(cid, {})["key_count"] = n
            counts[cid]["enabled_key_count"] = ok
        for cid, (n, ok) in p_agg.items():
            counts.setdefault(cid, {})["proxy_count"] = n
            counts[cid]["enabled_proxy_count"] = ok
        for cid, (n, ok) in m_agg.items():
            counts.setdefault(cid, {})["model_count"] = n
            counts[cid]["enabled_model_count"] = ok

        channels = []
        for c in Channel.objects.order_by("id"):
            cc = counts.get(c.id, {})
            setattr(c, "key_count", cc.get("key_count", 0))
            setattr(c, "enabled_key_count", cc.get("enabled_key_count", 0))
            setattr(c, "proxy_count", cc.get("proxy_count", 0))
            setattr(c, "enabled_proxy_count", cc.get("enabled_proxy_count", 0))
            setattr(c, "model_count", cc.get("model_count", 0))
            setattr(c, "enabled_model_count", cc.get("enabled_model_count", 0))
            channels.append(c)
        return Response({
            "results": ChannelSerializer(channels, many=True).data,
            "current": current_channel(request).slug,
        })

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"error": {"message": "name required", "code": "bad_request"}},
                            status=400)
        rpm, err = _require_int(request.data, "default_rpm", minimum=0)
        if err:
            return err
        slug = (request.data.get("slug") or "").strip() or _slugify(name)
        if Channel.objects.filter(slug=slug).exists():
            return Response({"error": {"message": f"渠道标识 {slug} 已存在",
                                       "code": "duplicate"}}, status=400)
        base_url = (request.data.get("base_url") or "").strip()
        if not base_url:
            return Response({"error": {"message": "base_url required", "code": "bad_request"}},
                            status=400)
        # 严格解析：与下面 enabled / allow_dup 等保持一致，非法布尔值返回 400，
        # 而不是被 `or` 静默吞成 False（传 "maybe" 应报错，不该悄悄关掉开关）。
        make_default, err = _require_bool(request.data, "is_default")
        if err:
            return err
        # 首个渠道自动成为默认渠道
        make_default = bool(make_default) or not Channel.objects.exists()
        enabled = _parse_bool(request.data.get("enabled", True))
        if enabled is None:
            return _bad_bool("enabled")
        allow_dup = _parse_bool(request.data.get("allow_duplicate_keys", False))
        if allow_dup is None:
            return _bad_bool("allow_duplicate_keys")
        disable_key_invalid = _parse_bool(request.data.get("disable_key_invalid", False))
        if disable_key_invalid is None:
            return _bad_bool("disable_key_invalid")
        disable_proxy_unhealthy = _parse_bool(
            request.data.get("disable_proxy_unhealthy", False))
        if disable_proxy_unhealthy is None:
            return _bad_bool("disable_proxy_unhealthy")
        channel = Channel(
            name=name, slug=slug, base_url=base_url,
            chat_path=(request.data.get("chat_path") or "/chat/completions").strip(),
            models_path=(request.data.get("models_path") or "/models").strip(),
            key_prefix=(request.data.get("key_prefix") or "").strip(),
            auth_scheme=request.data.get("auth_scheme") or "bearer",
            default_rpm=40 if rpm is None else rpm,
            enabled=enabled,
            is_default=make_default,
            notes=request.data.get("notes") or "",
            allow_duplicate_keys=allow_dup,
            disable_key_invalid=disable_key_invalid,
            disable_proxy_unhealthy=disable_proxy_unhealthy,
        )
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
            return Response({"detail": "not found"}, status=404)
        for f in ("name", "base_url", "chat_path", "models_path", "key_prefix",
                  "auth_scheme", "notes"):
            if f in request.data:
                setattr(channel, f, (request.data[f] or "").strip()
                        if isinstance(request.data[f], str) else request.data[f])
        for flag in ("allow_duplicate_keys", "disable_key_invalid",
                     "disable_proxy_unhealthy"):
            err = _apply_bool(request.data, flag, channel)
            if err is not None:
                return err
        if "disable_proxy_unhealthy" in request.data and channel.disable_proxy_unhealthy:
            # 立即恢复存量"异常/冷却"代理，让开关即刻生效并持久化：
            # 不因历史失败把代理继续排除在竞速池外。仅在显式提交该字段时执行，
            # 避免改个渠道名就顺手清掉全部代理冷却。
            channel.proxies.filter(status=ProxyStatus.UNHEALTHY).update(
                status=ProxyStatus.DEGRADED, cooldown_until=None)
            channel.proxies.exclude(cooldown_until=None).update(cooldown_until=None)
        if "default_rpm" in request.data:
            rpm, err = _require_int(request.data, "default_rpm", minimum=0)
            if err:
                return err
            channel.default_rpm = rpm
            # 勾选"应用到现有 Key"时，把该渠道所有 Key 的独立 RPM 一并覆盖
            if request.data.get("apply_rpm_to_keys"):
                channel.keys.update(rpm_limit=channel.default_rpm)
        err = _apply_bool(request.data, "enabled", channel)
        if err is not None:
            return err
        if request.data.get("is_default"):
            channel.is_default = True
        elif "is_default" in request.data and not request.data["is_default"]:
            # 不允许取消最后一个默认渠道
            if Channel.objects.exclude(pk=pk).filter(is_default=True).exists():
                channel.is_default = False
        channel.save()
        return Response(ChannelSerializer(channel).data)

    def delete(self, request, pk):
        channel = self._get(pk)
        if not channel:
            return Response({"detail": "not found"}, status=404)
        if channel.is_default and Channel.objects.count() == 1:
            return Response({"error": {"message": "至少保留一个渠道",
                                       "code": "last_channel"}}, status=400)
        channel.delete()
        channel_service.ensure_default_channel()
        return Response(status=204)


class ChannelTestView(AdminRequiredMixin, APIView):
    def post(self, request, pk):
        try:
            channel = Channel.objects.get(pk=pk)
        except Channel.DoesNotExist:
            return Response({"detail": "not found"}, status=404)
        return Response(channel_service.test_channel(channel))


def _slugify(name: str) -> str:
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "channel"


# ---------------------------------------------------------------- channel keys

class ChannelKeyListView(AdminRequiredMixin, APIView):
    def get(self, request):
        channel = current_channel(request)
        qs = channel.keys.order_by("id")
        return Response(ChannelKeySerializer(qs, many=True).data)

    def post(self, request):
        channel = current_channel(request)
        name = (request.data.get("name") or "").strip()
        key = (request.data.get("api_key") or "").strip()
        rpm_raw = request.data.get("rpm_limit")
        rpm = (_parse_int(rpm_raw) if rpm_raw not in (None, "")
               else (channel.default_rpm or 40))
        if rpm is None:
            return _bad_param("rpm_limit")
        if not name:
            name = f"{channel.name} Key {channel.keys.count() + 1:03d}"
        # 空 key = 匿名线路槽位（无鉴权渠道，如 LLM7 / Zen），允许多条并存
        allow_dup = bool(getattr(channel, "allow_duplicate_keys", False))
        if key and not allow_dup and key_service._key_stored_in_channel(channel, key):
            return Response({"error": {"message": "duplicate key", "code": "duplicate"}},
                            status=400)
        rec = ChannelKey.objects.create(channel=channel, name=name, api_key=key,
                                        rpm_limit=rpm)
        return Response(ChannelKeySerializer(rec).data, status=201)


class ChannelKeyImportView(AdminRequiredMixin, APIView):
    def post(self, request):
        text = request.data.get("text", "")
        if not text.strip():
            return Response({"error": {"message": "text required", "code": "bad_request"}},
                            status=400)
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
            return Response({"detail": "not found"}, status=404)
        data = ChannelKeySerializer(rec).data
        if request.query_params.get("reveal") == "1":
            from services.crypto import decrypt_secret
            data["api_key"] = decrypt_secret(rec.api_key)
        return Response(data)

    def patch(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return Response({"detail": "not found"}, status=404)
        name = request.data.get("name")
        if name:
            rec.name = name.strip()
        if "rpm_limit" in request.data:
            rpm = _parse_int(request.data["rpm_limit"])
            if rpm is None:
                return _bad_param("rpm_limit")
            rec.rpm_limit = rpm
        action = request.data.get("action")
        enabled = request.data.get("enabled")
        if enabled is False or action == "disable":
            rec.status = ChannelKeyStatus.DISABLED
        elif enabled is True or action == "enable":
            rec.status = ChannelKeyStatus.AVAILABLE
            rec.cooldown_until = None
        rec.save()
        return Response(ChannelKeySerializer(rec).data)

    def delete(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return Response({"detail": "not found"}, status=404)
        rec.delete()
        return Response(status=204)


class ChannelKeyTestView(AdminRequiredMixin, APIView):
    def post(self, request, pk):
        try:
            rec = ChannelKey.objects.get(pk=pk)
        except ChannelKey.DoesNotExist:
            return Response({"detail": "not found"}, status=404)
        return Response(key_service.test_key(rec))


# ---------------------------------------------------------------- proxies

class ProxyGroupListView(AdminRequiredMixin, APIView):
    def get(self, request):
        channel = current_channel(request)
        qs = channel.proxy_groups.annotate(proxy_count=Count("proxies")).order_by("id")
        return Response(ProxyGroupSerializer(qs, many=True).data)

    def post(self, request):
        channel = current_channel(request)
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"error": {"message": "name required", "code": "bad_request"}},
                            status=400)
        if channel.proxy_groups.filter(name=name).exists():
            return Response({"error": {"message": "duplicate group", "code": "duplicate"}},
                            status=400)
        g = ProxyGroup.objects.create(
            channel=channel, name=name,
            description=request.data.get("description", ""),
            country=request.data.get("country", ""),
            enabled=request.data.get("enabled", True),
        )
        data = ProxyGroupSerializer(g).data
        data["proxy_count"] = 0
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
            return Response({"detail": "not found"}, status=404)
        for f in ("name", "description", "country", "enabled"):
            if f in request.data:
                setattr(g, f, request.data[f])
        g.save()
        data = ProxyGroupSerializer(g).data
        data["proxy_count"] = Proxy.objects.filter(group=g).count()
        return Response(data)

    def delete(self, request, pk):
        g = self._get(pk)
        if not g:
            return Response({"detail": "not found"}, status=404)
        Proxy.objects.filter(group=g).update(group=None)
        g.delete()
        return Response(status=204)


class ProxyListView(AdminRequiredMixin, APIView):
    def get(self, request):
        channel = current_channel(request)
        qs = channel.proxies.select_related("group").order_by("id")
        n_keys = channel.keys.exclude(status=ChannelKeyStatus.DISABLED).count()
        max_allowed = max(n_keys - 1, 0)
        enabled = qs.filter(enabled=True).count()
        return Response({
            "results": ProxySerializer(qs, many=True).data,
            "summary": {
                "channel": channel.slug,
                "channel_id": channel.id,
                "disable_proxy_unhealthy": channel.disable_proxy_unhealthy,
                "nvidia_keys": n_keys,
                "max_enabled_proxies": max_allowed,
                "enabled_proxies": enabled,
                "direct_routes": 1 if n_keys else 0,
                "total_routes": enabled + (1 if n_keys else 0),
            },
        })

    def post(self, request):
        channel = current_channel(request)
        ser = ProxyWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not request.data.get("name"):
            ser.validated_data["name"] = f"代理 {channel.proxies.count() + 1:03d}"
        group = ser.validated_data.get("group")
        if group is not None and group.channel_id != channel.id:
            return Response({"error": {"message": "分组不属于当前渠道",
                                       "code": "bad_request"}}, status=400)
        p = Proxy.objects.create(channel=channel, **ser.validated_data)
        return Response(ProxySerializer(p).data, status=201)


class ProxyImportView(AdminRequiredMixin, APIView):
    def post(self, request):
        text = request.data.get("text", "")
        if not text.strip():
            return Response({"error": {"message": "text required", "code": "bad_request"}},
                            status=400)
        return Response(proxy_service.bulk_import_proxies(text, current_channel(request)))


class ProxyDetailView(AdminRequiredMixin, APIView):
    def _get(self, pk):
        try:
            return Proxy.objects.select_related("group").get(pk=pk)
        except Proxy.DoesNotExist:
            return None

    def patch(self, request, pk):
        p = self._get(pk)
        if not p:
            return Response({"detail": "not found"}, status=404)
        enabled, err = _require_bool(request.data, "enabled")
        if err is not None:
            return err
        if enabled is not None:
            ok, msg = proxy_service.set_enabled(p, enabled)
            if not ok:
                return Response(
                    {"error": {"message": msg, "code": "proxy_limit_exceeded"}}, status=400
                )
        if "group" in request.data:
            gid = request.data["group"]
            if gid in (None, ""):
                p.group = None
            else:
                g = ProxyGroup.objects.filter(pk=gid).first()
                if g is None:
                    return Response({"error": {"message": "分组不存在",
                                               "code": "bad_request"}}, status=400)
                if g.channel_id != p.channel_id:
                    return Response({"error": {"message": "分组不属于该代理所在渠道",
                                               "code": "bad_request"}}, status=400)
                p.group = g
        if "port" in request.data:
            port, err = _require_int(request.data, "port", minimum=1, maximum=65535)
            if err:
                return err
            p.port = port
        for f in ("name", "protocol", "host", "username", "password"):
            if f in request.data:
                setattr(p, f, request.data[f])
        p.save()
        return Response(ProxySerializer(p).data)

    def delete(self, request, pk):
        p = self._get(pk)
        if not p:
            return Response({"detail": "not found"}, status=404)
        p.delete()
        return Response(status=204)


class ProxyTestView(AdminRequiredMixin, APIView):
    def post(self, request, pk):
        try:
            p = Proxy.objects.get(pk=pk)
        except Proxy.DoesNotExist:
            return Response({"detail": "not found"}, status=404)
        return Response(proxy_service.run_async(check_proxy(p)))


class ProxyFetchIpView(ProxyTestView):
    pass  # check_proxy already performs IP + geo lookup


class ProxyTestAllView(AdminRequiredMixin, APIView):
    def post(self, request):
        channel = current_channel(request)
        return Response(proxy_service.run_async(check_all(channel)))


# ---------------------------------------------------------------- models

class ModelListView(AdminRequiredMixin, APIView):
    def get(self, request):
        channel = current_channel(request)
        qs = channel.models.order_by("model_name")
        q = request.query_params.get("q")
        if q:
            qs = qs.filter(model_name__icontains=q)
        return Response(ModelSerializer(qs, many=True).data)

    def post(self, request):
        channel = current_channel(request)
        name = (request.data.get("model_name") or "").strip()
        if not name:
            return Response({"error": {"message": "model_name required",
                                       "code": "bad_request"}}, status=400)
        enabled = _parse_bool(request.data.get("enabled", False))
        if enabled is None:
            return _bad_bool("enabled")
        defaults = {
            "display_name": request.data.get("display_name", ""),
            "alias": (request.data.get("alias") or "").strip(),
            "aliases": _normalize_aliases(request.data.get("aliases")),
            "description": request.data.get("description", ""),
            "provider": request.data.get("provider") or channel.slug,
            "endpoint": (request.data.get("endpoint") or "").strip(),
            "enabled": enabled,
        }
        rec, created = channel.models.get_or_create(model_name=name, defaults=defaults)
        if not created:
            # 重复添加：把新提交的展示名/别名等应用到已有模型
            for f in ("display_name", "alias", "aliases", "description",
                      "endpoint", "enabled"):
                if f in request.data and request.data[f] is not None:
                    setattr(rec, f, defaults[f])
        self._apply_proxy_group(rec, request)
        rec.save()
        return Response(ModelSerializer(rec).data, status=201 if created else 200)

    @staticmethod
    def _apply_proxy_group(rec, request):
        gid = request.data.get("proxy_group")
        if gid in (None, "", "null"):
            if "proxy_group" in request.data:
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
        # 请求体里的 channel 字段优先，其次 X-Channel 头（当前渠道作用域），
        # 避免前端同步模型时必须切换全局渠道导致整台重挂载
        channel_param = (request.data.get("channel") or "").strip()
        channel = (
            channel_service.resolve(channel_param) if channel_param
            else current_channel(request)
        )
        prune = str(request.data.get("prune") or "").lower() in ("1", "true", "yes")
        try:
            return Response(upstream_service.sync_models(channel, prune=prune))
        except ValueError as exc:
            msg = str(exc)
            code = "no_available_key" if msg == "no_available_key" else "upstream_error"
            status = 503 if code == "no_available_key" else 502
            return Response({"error": {"message": msg, "code": code}}, status=status)


class ModelDetailView(AdminRequiredMixin, APIView):
    def _get(self, pk):
        try:
            return AIModel.objects.get(pk=pk)
        except AIModel.DoesNotExist:
            return None

    def patch(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return Response({"detail": "not found"}, status=404)
        if "aliases" in request.data:
            rec.aliases = _normalize_aliases(request.data["aliases"])
        if "route_priority" in request.data:
            priority, err = _require_int(request.data, "route_priority")
            if err:
                return err
            rec.route_priority = priority
        err = _apply_bool(request.data, "enabled", rec)
        if err is not None:
            return err
        for f in ("display_name", "alias", "description", "status", "endpoint"):
            if f in request.data:
                if f == "endpoint":
                    rec.endpoint = (request.data[f] or "").strip()
                else:
                    setattr(rec, f, request.data[f])
        if "proxy_group" in request.data:
            gid = request.data.get("proxy_group")
            if gid in (None, "", "null"):
                rec.proxy_group = None
            else:
                try:
                    gid = int(gid)
                except (TypeError, ValueError):
                    gid = None
                rec.proxy_group = (
                    rec.channel.proxy_groups.filter(pk=gid).first()
                    if gid is not None else None
                )
        rec.save()
        return Response(ModelSerializer(rec).data)

    def delete(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return Response({"detail": "not found"}, status=404)
        rec.delete()
        return Response(status=204)


# ---------------------------------------------------------------- batch ops

def _parse_ids(request) -> list[int]:
    ids = request.data.get("ids") or []
    if not isinstance(ids, list):
        return []
    return [int(i) for i in ids if str(i).isdigit()]


def _normalize_aliases(value) -> list[str]:
    """规范化附加别名：支持 JSON 数组或逗号分隔字符串；去空格、去空、去重。"""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        for part in str(item or "").split(","):
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return out


class ModelBatchView(AdminRequiredMixin, APIView):
    """POST {ids: [...], action: "enable"|"disable"|"delete"}"""

    def post(self, request):
        channel = current_channel(request)
        ids = _parse_ids(request)
        action = request.data.get("action")
        if not ids or action not in ("enable", "disable", "delete"):
            return Response({"error": {"message": "ids 与合法 action 必填",
                                       "code": "bad_request"}}, status=400)
        qs = channel.models.filter(id__in=ids)
        matched = qs.count()
        if action == "delete":
            qs.delete()
        else:
            qs.update(enabled=(action == "enable"))
        # queryset.update() 不触发 post_save 信号，注册表缓存需显式失效，
        # 否则批量启停后 /v1/models 与解析结果会短暂停留在旧状态。
        model_registry.invalidate()
        return Response({"matched": matched, "action": action})


class ProxyBatchView(AdminRequiredMixin, APIView):
    """POST {ids: [...], action: "enable"|"disable"|"delete"|"test"|"group"}"""

    def post(self, request):
        channel = current_channel(request)
        ids = _parse_ids(request)
        action = request.data.get("action")
        if not ids or action not in ("enable", "disable", "delete", "test", "group"):
            return Response({"error": {"message": "ids 与合法 action 必填",
                                       "code": "bad_request"}}, status=400)
        qs = list(channel.proxies.filter(id__in=ids))
        if action == "delete":
            channel.proxies.filter(id__in=ids).delete()
            return Response({"matched": len(qs), "action": action})
        if action == "test":
            result = proxy_service.run_async(
                check_all(channel, ids=[p.id for p in qs]))
            return Response({"matched": len(qs), "action": action, **result})
        if action == "group":
            gid = request.data.get("group_id")
            if gid in (None, "", "null"):
                channel.proxies.filter(id__in=ids).update(group=None)
                return Response({"matched": len(qs), "action": action,
                                 "succeeded": len(qs)})
            try:
                gid = int(gid)
            except (TypeError, ValueError):
                return Response({"error": {"message": "group_id 非法",
                                           "code": "bad_request"}}, status=400)
            group = channel.proxy_groups.filter(pk=gid).first()
            if not group:
                return Response({"error": {"message": "分组不存在",
                                           "code": "not_found"}}, status=404)
            channel.proxies.filter(id__in=ids).update(group=group)
            return Response({"matched": len(qs), "action": action,
                             "succeeded": len(qs), "group_id": gid})
        # enable / disable：逐个走 set_enabled 以保留「启用数 ≤ Key 数 - 1」限制
        done, skipped = 0, []
        for p in qs:
            ok, msg = proxy_service.set_enabled(p, action == "enable")
            if ok:
                done += 1
            else:
                skipped.append({"id": p.id, "name": p.name, "reason": msg})
        return Response({"matched": len(qs), "action": action,
                         "succeeded": done, "skipped": skipped})


# ---------------------------------------------------------------- user api keys

class KeyBatchView(AdminRequiredMixin, APIView):
    """POST {ids: [...], action: "enable"|"disable"|"delete"|"test"|"set_rpm"}"""

    def post(self, request):
        channel = current_channel(request)
        ids = _parse_ids(request)
        action = request.data.get("action")
        if not ids or action not in ("enable", "disable", "delete", "test", "set_rpm"):
            return Response({"error": {"message": "ids 与合法 action 必填",
                                       "code": "bad_request"}}, status=400)
        qs = list(channel.keys.filter(id__in=ids))
        if action == "delete":
            channel.keys.filter(id__in=ids).delete()
            return Response({"matched": len(qs), "action": action})
        if action == "test":
            results = []
            for k in qs:
                results.append({"id": k.id, "name": k.name,
                                **key_service.test_key(k)})
            return Response({"matched": len(qs), "action": action,
                             "results": results})
        if action == "set_rpm":
            rpm = _parse_int(request.data.get("rpm"))
            if rpm is None or rpm < 0:
                return _bad_param("rpm")
            changed = channel.keys.filter(id__in=ids).update(rpm_limit=rpm)
            return Response({"matched": len(qs), "action": action,
                             "succeeded": changed})
        # enable / disable：仅对非目标状态的行生效，避免反复打状态
        if action == "enable":
            status = ChannelKeyStatus.AVAILABLE
            qs = [k for k in qs if k.status != ChannelKeyStatus.AVAILABLE]
        else:
            status = ChannelKeyStatus.DISABLED
            qs = [k for k in qs if k.status != ChannelKeyStatus.DISABLED]
        changed = 0
        for k in qs:
            k.status = status
            k.cooldown_until = None
            k.save(update_fields=["status", "cooldown_until", "updated_at"])
            changed += 1
        return Response({"matched": len(qs) + changed, "action": action,
                         "succeeded": changed})


class UserApiKeyListView(AdminRequiredMixin, APIView):
    """用户 Key 是平台级的，跨渠道共享。"""

    def get(self, request):
        return Response(UserApiKeySerializer(UserApiKey.objects.order_by("-id"), many=True).data)

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"error": {"message": "name required", "code": "bad_request"}},
                            status=400)
        rl_raw = request.data.get("rate_limit")
        rate_limit = _parse_int(rl_raw) if rl_raw not in (None, "") else 0
        if rate_limit is None:
            return _bad_param("rate_limit")
        quota_raw = request.data.get("quota")
        quota = _parse_int(quota_raw) if quota_raw not in (None, "") else 0
        if quota is None or quota < 0:
            return _bad_param("quota")
        rec, raw = api_key_service.create_key(name, rate_limit=rate_limit, quota=quota)
        data = UserApiKeySerializer(rec).data
        data["key"] = raw  # full key shown once at creation only
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
            return Response({"detail": "not found"}, status=404)
        err = _apply_bool(request.data, "enabled", rec)
        if err is not None:
            return err
        if "rate_limit" in request.data:
            rl, err = _require_int(request.data, "rate_limit", minimum=0)
            if err:
                return err
            rec.rate_limit = rl
        if "quota" in request.data:
            q, err = _require_int(request.data, "quota", minimum=0)
            if err:
                return err
            rec.quota = q
        if "name" in request.data:
            rec.name = request.data["name"]
        rec.save()
        return Response(UserApiKeySerializer(rec).data)

    def delete(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return Response({"detail": "not found"}, status=404)
        rec.delete()
        return Response(status=204)


# ---------------------------------------------------------------- logs / dashboard / settings

class LogListView(AdminRequiredMixin, APIView):
    def get(self, request):
        channel = current_channel(request)
        qs = channel.logs.order_by("-id")
        model = request.query_params.get("model")
        status = request.query_params.get("status")
        if model:
            qs = qs.filter(model=model)
        if status:
            qs = qs.filter(status=status)
        # 分页：limit 默认 100，上限 500；offset 用于"加载更多"
        limit_raw = request.query_params.get("limit")
        limit = _parse_int(limit_raw) if limit_raw not in (None, "") else 100
        if limit is None:
            return _bad_param("limit")
        limit = max(1, min(limit, 500))
        offset_raw = request.query_params.get("offset")
        offset = _parse_int(offset_raw) if offset_raw not in (None, "") else 0
        if offset is None:
            return _bad_param("offset")
        offset = max(offset, 0)
        total = qs.count()
        page = list(qs[offset: offset + limit])
        return Response({
            "results": RequestLogSerializer(page, many=True).data,
            "channel": channel.slug,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < total,
        })


class LogCleanView(AdminRequiredMixin, APIView):
    """清理过期请求日志。默认清理当前渠道；`all=1` 清理所有渠道。

    `days` 可显式覆盖系统参数 log_retention_days；0 表示本次不清理。
    """

    def post(self, request):
        from services import cleanup

        all_channels = str(request.data.get("all") or "").lower() in ("1", "true", "yes")
        days_raw = request.data.get("days")
        days = _parse_int(days_raw) if days_raw not in (None, "") else None
        if days is None and days_raw not in (None, ""):
            return _bad_param("days")
        channel = None if all_channels else current_channel(request)
        result = cleanup.clean_old_logs(days=days, channel=channel)
        return Response(result)


class DashboardView(AdminRequiredMixin, APIView):
    """当前渠道的运行指标（随顶部渠道切换变化；token 汇总在 usage 接口）。"""

    def get(self, request):
        channel = current_channel(request)
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        keys = channel.keys.all()
        proxies = channel.proxies.all()
        logs_today = channel.logs.filter(created_at__gte=today)

        key_status = {s: keys.filter(status=s).count() for s, _ in ChannelKeyStatus.choices}
        proxy_status = {s: proxies.filter(status=s).count() for s, _ in ProxyStatus.choices}
        # 一次聚合取今日请求数 / 成功数 / 平均耗时
        agg = logs_today.aggregate(
            n=Count("id"),
            ok=Count("id", filter=Q(status="success")),
            avg=Avg("duration_ms"),
        )
        today_count = agg["n"] or 0

        n_active_keys = keys.exclude(status=ChannelKeyStatus.DISABLED).count()
        from api.openai_views import active_requests
        return Response({
            "channel": channel.slug,
            "channel_name": channel.name,
            "active_requests": active_requests(),
            "nvidia_keys": keys.count(),
            "enabled_keys": keys.exclude(
                status__in=[ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID]).count(),
            "proxies": proxies.count(),
            "enabled_proxies": proxies.filter(enabled=True).count(),
            "max_enabled_proxies": max(n_active_keys - 1, 0),
            "models": channel.models.count(),
            "enabled_models": channel.models.filter(enabled=True).count(),
            "requests_today": today_count,
            "success_rate": round((agg["ok"] or 0) / today_count * 100, 1)
            if today_count else 0.0,
            "avg_latency_s": round((agg["avg"] or 0) / 1000, 2),
            "key_status": key_status,
            "proxy_status": proxy_status,
        })


class DashboardUsageView(AdminRequiredMixin, APIView):
    """Token 用量统计：跨全部渠道汇总。

    返回按天分桶、区间汇总、上一周期环比、按模型分布、按渠道分布。
    """

    def get(self, request):
        days_raw = request.query_params.get("days", 7)
        days = _parse_int(days_raw)
        if days is None:
            return _bad_param("days")
        days = max(1, min(days, 30))
        # 按浏览器时区分桶，避免"今日"在服务器时区下显示成凌晨时段
        tz = request.query_params.get("tz", "") or None
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz) if tz else timezone.get_current_timezone()
        now = timezone.localtime(timezone.now(), tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # days=1 时为“今日”视图，按小时分桶（只到当前小时）
        hourly = days == 1
        start = today - timedelta(days=days - 1)
        prev_start = start - timedelta(days=days)

        def _bucket() -> dict:
            return {"date": "", "prompt_tokens": 0, "completion_tokens": 0,
                    "cached_tokens": 0, "total_tokens": 0,
                    "requests": 0, "success": 0}

        buckets: dict = {}
        if hourly:
            for h in range(now.hour + 1):
                key = f"{h:02d}:00"
                buckets[key] = {**_bucket(), "date": key}
        else:
            cur = start
            while cur <= today:
                key = cur.strftime("%Y-%m-%d")
                buckets[key] = {**_bucket(), "date": key}
                cur += timedelta(days=1)

        # .iterator() 流式取：默认一次遍历会把整个区间的结果集缓存进内存，
        # 30 天 × 大流量下是几十 MB 级的一次性占用；分块取只保留当前块。
        # （真要做成 DB 侧 GROUP BY 需要按 SQLite 的 strftime 分桶，改动面大，
        #  当前行数级别下按行聚合仍是最简单可靠的选择。）
        logs = RequestLog.objects.filter(created_at__gte=start).values(
            "created_at", "model", "prompt_tokens", "completion_tokens",
            "cached_tokens", "total_tokens", "status", "duration_ms",
            "first_token_ms", "channel__name", "user_api_key__name",
        ).iterator(chunk_size=2000)

        totals = {"requests": 0, "success": 0, "total_tokens": 0,
                  "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        models: dict[str, dict] = {}
        channels: dict[str, dict] = {}
        keys: dict[str, dict] = {}
        sum_duration = sum_ttft = 0.0
        n_duration = n_ttft = 0

        for row in logs:
            ok = row["status"] == "success"
            ts = timezone.localtime(row["created_at"], tz)
            key = ts.strftime("%H:00") if hourly else ts.strftime("%Y-%m-%d")
            b = buckets.get(key)
            if b:
                b["prompt_tokens"] += row["prompt_tokens"] or 0
                b["completion_tokens"] += row["completion_tokens"] or 0
                b["cached_tokens"] += row["cached_tokens"] or 0
                b["total_tokens"] += row["total_tokens"] or 0
                b["requests"] += 1
                if ok:
                    b["success"] += 1

            totals["requests"] += 1
            totals["prompt_tokens"] += row["prompt_tokens"] or 0
            totals["completion_tokens"] += row["completion_tokens"] or 0
            totals["cached_tokens"] += row["cached_tokens"] or 0
            totals["total_tokens"] += row["total_tokens"] or 0
            if ok:
                totals["success"] += 1
            if row["duration_ms"]:
                sum_duration += row["duration_ms"]
                n_duration += 1
            if row["first_token_ms"]:
                sum_ttft += row["first_token_ms"]
                n_ttft += 1

            name = row["model"] or "(unknown)"
            m = models.setdefault(name, {
                "model": name, "requests": 0, "success": 0, "total_tokens": 0,
                "_duration": 0.0, "_n": 0,
            })
            m["requests"] += 1
            m["total_tokens"] += row["total_tokens"] or 0
            if ok:
                m["success"] += 1
            if row["duration_ms"]:
                m["_duration"] += row["duration_ms"]
                m["_n"] += 1

            cname = row["channel__name"] or "(无渠道)"
            c = channels.setdefault(cname, {"name": cname, "requests": 0,
                                            "total_tokens": 0})
            c["requests"] += 1
            c["total_tokens"] += row["total_tokens"] or 0

            kname = row["user_api_key__name"] or "(未知 Key)"
            k = keys.setdefault(kname, {"name": kname, "requests": 0,
                                        "total_tokens": 0})
            k["requests"] += 1
            k["total_tokens"] += row["total_tokens"] or 0

        model_rows = []
        for m in models.values():
            n = m.pop("_n")
            dur = m.pop("_duration")
            model_rows.append({
                **m,
                "success_rate": round(m["success"] / m["requests"] * 100, 1)
                if m["requests"] else 0.0,
                "avg_latency_s": round(dur / n / 1000, 2) if n else None,
            })
        model_rows.sort(key=lambda r: (-r["total_tokens"], r["model"]))

        channel_rows = sorted(channels.values(),
                              key=lambda r: -r["total_tokens"])

        prev = RequestLog.objects.filter(
            created_at__gte=prev_start, created_at__lt=start,
        ).aggregate(requests=Count("id"), total_tokens=Sum("total_tokens"),
                    success=Count("id", filter=Q(status="success")))

        totals.update({
            "success_rate": round(
                totals["success"] / totals["requests"] * 100, 1)
            if totals["requests"] else 0.0,
            "avg_latency_s": round(sum_duration / n_duration / 1000, 2)
            if n_duration else None,
            "avg_ttft_ms": round(sum_ttft / n_ttft, 1) if n_ttft else None,
            # 上游缓存命中率：缓存读取 / 输入
            "cache_hit_rate": round(
                totals["cached_tokens"] / totals["prompt_tokens"] * 100, 1)
            if totals["prompt_tokens"] else 0.0,
        })

        prev_requests = prev["requests"] or 0
        prev_totals = {
            "requests": prev_requests,
            "total_tokens": prev["total_tokens"] or 0,
            "success_rate": round((prev["success"] or 0) / prev_requests * 100, 1)
            if prev_requests else 0.0,
        }

        return Response({"granularity": "hour" if hourly else "day",
                         "days": list(buckets.values()),
                         "totals": totals, "prev_totals": prev_totals,
                         "models": model_rows[:20], "channels": channel_rows,
                         "keys": sorted(keys.values(),
                                        key=lambda r: -r["total_tokens"])[:20]})


class SettingsView(AdminRequiredMixin, APIView):
    def get(self, request):
        from services import sysconfig
        channel = current_channel(request)
        return Response({"channel": channel.slug,
                         "settings": sysconfig.all_params(channel)})

    def patch(self, request):
        from services import sysconfig
        channel = current_channel(request)
        updates = request.data.get("settings")
        if not isinstance(updates, dict):
            key = request.data.get("key")
            if not key:
                return Response({"detail": "settings or key required"}, status=400)
            updates = {key: request.data.get("value")}
        sysconfig.set_params(updates, channel)
        return Response({"channel": channel.slug,
                         "settings": sysconfig.all_params(channel)})

    def delete(self, request):
        """清空当前渠道的覆盖值，回落到默认。"""
        from services import sysconfig
        channel = current_channel(request)
        keys = request.query_params.get("keys")
        sysconfig.reset_params(keys.split(",") if keys else None, channel)
        return Response({"channel": channel.slug,
                         "settings": sysconfig.all_params(channel)})


class AdminChatView(AdminRequiredMixin, APIView):
    """Playground: run a real chat completion through the race engine."""

    ALLOWED = {"model", "messages", "temperature", "top_p", "max_tokens",
               "frequency_penalty", "presence_penalty", "stream"}

    def post(self, request):
        from services.load_balancer import build_routes
        from services.race_engine import AllRoutesFailed, NoRouteAvailable, race_chat
        from services import key_service as ks

        channel_param = (request.data.get("channel") or "").strip()
        # 优先请求体里的 channel 字段，其次 X-Channel 头（当前渠道作用域）
        channel = (
            channel_service.resolve(channel_param) if channel_param
            else current_channel(request)
        )

        model = (request.data.get("model") or "").strip()
        prompt = request.data.get("prompt")
        messages = request.data.get("messages")
        if prompt and not messages:
            messages = [{"role": "user", "content": str(prompt)}]
        if not model or not messages:
            return Response({"error": {"message": "model and prompt/messages required",
                                       "code": "bad_request"}}, status=400)
        model_rec = channel.models.filter(model_name=model).first()
        if not model_rec or not model_rec.enabled:
            return Response({"error": {"message": f"模型 {model} 不存在或未启用",
                                       "code": "model_not_found"}}, status=404)

        body = {
            k: v for k, v in request.data.items()
            if k in self.ALLOWED and k not in thinking.THINKING_PARAM_KEYS and v is not None
        }
        body["model"] = model
        body["messages"] = messages
        body.update(thinking.build_upstream(request.data, model))

        # 记录思考参数：客户端原始传入 + 实际下发到上游，供日志页排查
        client_thinking = {
            k: request.data.get(k) for k in thinking.THINKING_PARAM_KEYS
            if k in request.data and request.data.get(k) is not None
        }
        upstream_thinking = thinking.build_upstream(request.data, model)
        proxy_group = model_rec.proxy_group_id if model_rec else None

        if request.data.get("stream"):
            return self._stream(body, model, channel, proxy_group=proxy_group,
                                endpoint=model_rec.endpoint,
                                client_thinking=client_thinking,
                                upstream_thinking=upstream_thinking)

        routes = build_routes(channel, proxy_group=proxy_group,
                              endpoint=model_rec.endpoint)
        started = timezone.now().timestamp()
        request_id = ks.new_request_id()
        log = RequestLog.objects.create(channel=channel, request_id=request_id, model=model,
                                        routes_count=len(routes),
                                        client_thinking=client_thinking,
                                        upstream_thinking=upstream_thinking)
        if not routes:
            log.status, log.http_status, log.error_type = "failed", 503, "no_available_route"
            log.save()
            return Response({"error": {"message": "当前没有可用线路（没有可用的渠道 Key）",
                                       "code": "no_available_route"}}, status=503)
        import time
        t0 = time.monotonic()
        try:
            result = race_chat(routes, body)
        except AllRoutesFailed as exc:
            log.status, log.error_type = "failed", "all_routes_failed"
            log.http_status = 502
            log.routes = exc.report
            log.save()
            return Response({"error": {"message": f"所有线路均失败: {exc}",
                                       "code": "upstream_error"},
                             "routes": exc.report}, status=502)
        except NoRouteAvailable:
            log.status, log.error_type = "failed", "no_available_route"
            log.http_status = 503
            log.save()
            return Response({"error": {"message": "当前没有可用线路",
                                       "code": "no_available_route"}}, status=503)
        duration = round((time.monotonic() - t0) * 1000, 1)
        r = result.route
        usage = (result.payload or {}).get("usage") or {}
        log.status, log.http_status = "success", 200
        log.duration_ms = duration
        log.winner_route_type = r.kind
        log.winner_key_name = r.key.name
        log.winner_proxy_name = r.proxy.name if r.proxy else ""
        log.proxy_public_ip = r.proxy.public_ip if r.proxy else ""
        log.prompt_tokens = usage.get("prompt_tokens", 0) or 0
        log.completion_tokens = usage.get("completion_tokens", 0) or 0
        log.total_tokens = usage.get("total_tokens", 0) or 0
        log.cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        log.routes = result.report or []
        log.save()
        return Response({
            "request_id": request_id,
            "channel": channel.slug,
            "payload": result.payload,
            "meta": {
                "route_type": r.kind,
                "key_name": r.key.name,
                "proxy_name": r.proxy.name if r.proxy else "",
                "duration_ms": duration,
                "usage": usage,
                "routes": result.report or [],
            },
        })

    def _stream(self, body, model, channel, proxy_group=None,
                endpoint=None, client_thinking=None, upstream_thinking=None):
        """SSE（异步生成器）: 竞速胜出后逐块转发上游 SSE，实现真正 token-by-token 流式。

        必须是 async 生成器：Django 对**同步**流式内容会用
        `sync_to_async(list(...))` 一次性消费完整个生成器才下发，导致"假流式"——
        思考 token 与正文全部攒到请求结束才一次性吐出。async 生成器则被 ASGI
        逐块下发，思考（reasoning_content）与正文 token 边到边实时转发。

        心跳与掐线（参考 new-api / sub-api / cliproxy 思路）：
        - stream_heartbeat_interval：上游静默时向客户端发 `: keep-alive` 心跳，
          防 NAT/负载均衡/客户端把连接误判为死，链路保活；
        - stream_probe_interval × stream_max_idle_probes：判死的"心跳机制"——
          连续 N 个探测周期无任何数据（含思考 token），判定线路死亡；
          思考模型会持续吐 reasoning token，正常"正在思考"不会被掐断；
        - 已向客户端交付正文后断流：绝不发 error 事件（否则客户端 SSE 解析报
          "error decoding response body"），干净收尾 [DONE]。
        """
        import asyncio
        import json

        from django.http import StreamingHttpResponse

        from services import key_service as ks
        from services import sysconfig
        from services.load_balancer import build_routes
        from services.race_engine import AllRoutesFailed, NoRouteAvailable, race_stream

        from .openai_views import _chunk_has_content, _drain

        routes = build_routes(channel, proxy_group=proxy_group, endpoint=endpoint)
        request_id = ks.new_request_id()
        log = RequestLog.objects.create(
            channel=channel, request_id=request_id, model=model,
            routes_count=len(routes), is_stream=True,
            client_thinking=client_thinking or {}, upstream_thinking=upstream_thinking or {},
        )
        # 请求流式 usage：部分上游默认流式不返回 usage，需 include_usage 才在收尾 chunk 给出
        body = dict(body)
        body.setdefault("stream_options", {}).update({"include_usage": True})
        if not routes:
            log.status, log.http_status, log.error_type = "failed", 503, "no_available_route"
            log.save()
            return Response({"error": {"message": "当前没有可用线路",
                                       "code": "no_available_route"}}, status=503)

        import time as _time
        started = _time.monotonic()
        probe_interval = float(sysconfig.get("stream_probe_interval", channel) or 0)
        max_idle_probes = int(sysconfig.get("stream_max_idle_probes", channel) or 0)
        heartbeat = float(sysconfig.get("stream_heartbeat_interval", channel) or 0)
        max_duration = float(sysconfig.get("stream_max_duration", channel) or 0)

        async def gen():
            winner = None
            sent_content = False
            try:
                winner = await race_stream(routes, body)

                duration = round((_time.monotonic() - started) * 1000, 1)
                log.status, log.http_status = "success", 200
                log.winner_route_type = winner.route.kind
                log.winner_key_name = winner.route.key.name
                log.winner_proxy_name = winner.route.proxy.name if winner.route.proxy else ""
                log.proxy_public_ip = winner.route.proxy.public_ip if winner.route.proxy else ""
                log.routes = winner.report or []
                log.save()

                yield "data: " + json.dumps({
                    "meta": {
                        "request_id": request_id,
                        "channel": channel.slug,
                        "route_type": winner.route.kind,
                        "key_name": winner.route.key.name,
                        "proxy_name": winner.route.proxy.name if winner.route.proxy else "",
                        "first_chunk_ms": duration,
                        "routes": winner.report or [],
                    }
                }) + "\n\n"

                usage: dict = {}
                completion_text: list[str] = []
                stream_ok = False
                done_sent = False
                try:
                    async for chunk in _drain(winner, probe_interval, max_idle_probes, heartbeat,
                                      max_duration):
                        # 首字 = 首个正文（content/tool_calls）到达时间，非首个思考 chunk
                        if _chunk_has_content(chunk):
                            if not sent_content:
                                log.first_token_ms = round(
                                    (_time.monotonic() - started) * 1000, 1)
                            sent_content = True
                        if chunk.strip() == "data: [DONE]":
                            done_sent = True
                        try:
                            if chunk.startswith("data:"):
                                payload = json.loads(chunk[5:].strip())
                                if isinstance(payload, dict):
                                    if payload.get("usage"):
                                        usage = payload["usage"]
                                    # 累积正文，供上游未返回流式 usage 时本地估算 token
                                    choices = payload.get("choices")
                                    if choices:
                                        delta = choices[0].get("delta") or {}
                                        for key in ("content", "reasoning_content", "reasoning"):
                                            v = delta.get(key)
                                            if isinstance(v, str) and v:
                                                completion_text.append(v)
                                                break
                        except Exception:  # noqa: BLE001
                            pass
                        yield chunk
                    stream_ok = True
                finally:
                    total_ms = round((_time.monotonic() - started) * 1000, 1)
                    log.duration_ms = total_ms
                    log.prompt_tokens = usage.get("prompt_tokens", 0) or 0
                    log.completion_tokens = usage.get("completion_tokens", 0) or 0
                    if not log.prompt_tokens:
                        from services import tokenizer
                        log.prompt_tokens = tokenizer.estimate_messages_tokens(
                            body.get("messages"))
                    if not log.completion_tokens:
                        from services import tokenizer
                        log.completion_tokens = tokenizer.estimate_tokens(
                            "".join(completion_text))
                    log.total_tokens = (log.prompt_tokens or 0) + (log.completion_tokens or 0)
                    details = usage.get("prompt_tokens_details") or {}
                    log.cached_tokens = details.get("cached_tokens", 0) or 0
                    log.save()
                    # 仅正常走完整个流（未被超时/异常/客户端断开打断）才补 summary + [DONE]
                    if stream_ok:
                        yield "data: " + json.dumps({
                            "summary": {
                                "duration_ms": total_ms,
                                "first_token_ms": log.first_token_ms or duration,
                                "prompt_tokens": log.prompt_tokens,
                                "completion_tokens": log.completion_tokens,
                                "total_tokens": log.total_tokens,
                                "cached_tokens": log.cached_tokens,
                            }
                        }) + "\n\n"
                        yield "data: [DONE]\n\n"
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                log.status, log.http_status, log.error_type = "failed", 502, "all_routes_failed"
                if isinstance(exc, AllRoutesFailed):
                    log.routes = exc.report
                log.save()
                yield "data: " + json.dumps({
                    "error": {"message": f"所有线路均失败: {exc}", "type": "api_error",
                              "param": None, "code": "upstream_error"}
                }) + "\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001  含 TimeoutError
                # 已向客户端交付过正文：响应已提交。上游断流时绝不发 error 事件
                # （会破坏客户端 SSE 解析），干净收尾 [DONE]。
                if sent_content or done_sent:
                    log.duration_ms = round((_time.monotonic() - started) * 1000, 1)
                    log.save()
                    if not done_sent:
                        yield "data: [DONE]\n\n"
                    return
                # 未交付任何内容：按线路失败上报错误（可让客户端看到原因）
                is_stall = isinstance(exc, TimeoutError)
                log.status, log.http_status = "failed", 504 if is_stall else 502
                log.error_type = "stream_idle_timeout" if is_stall else "stream_error"
                log.duration_ms = round((_time.monotonic() - started) * 1000, 1)
                log.save()
                if is_stall:
                    msg = ("上游连续无响应（"
                           f"{int(max_idle_probes or 0)}×{round(probe_interval or 0, 1)} 秒"
                           "未收到任何数据），已判定线路死亡。可调大 stream_probe_interval"
                           " / stream_max_idle_probes")
                else:
                    msg = f"stream error: {exc}"
                yield "data: " + json.dumps({
                    "error": {"message": msg, "type": "api_error",
                              "param": None, "code": "stream_error"}
                }) + "\n\n"
                yield "data: [DONE]\n\n"
            finally:
                if winner is not None:
                    try:
                        await winner.close()
                    except Exception:  # noqa: BLE001
                        pass

        response = StreamingHttpResponse(gen(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
