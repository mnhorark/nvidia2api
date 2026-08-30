"""Admin API：所有资源按渠道隔离，渠道由 `X-Channel` 头或 `?channel=` 决定。"""
from __future__ import annotations

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
    api_key_service, channel_service, key_service, proxy_service, thinking,
    upstream_service,
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


class LoginView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        u = request.data.get("username", "")
        p = request.data.get("password", "")
        from django.conf import settings
        if u == settings.ADMIN_USERNAME and p == settings.ADMIN_PASSWORD:
            return Response({"token": settings.ADMIN_TOKEN})
        return Response({"detail": "Invalid credentials"}, status=401)


# ---------------------------------------------------------------- channels

class ChannelListView(AdminRequiredMixin, APIView):
    def get(self, request):
        channels = channel_service.list_channels()
        current = current_channel(request)
        return Response({
            "results": ChannelSerializer(channels, many=True).data,
            "current": current.slug,
        })

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"error": {"message": "name required", "code": "bad_request"}},
                            status=400)
        slug = (request.data.get("slug") or "").strip() or _slugify(name)
        if Channel.objects.filter(slug=slug).exists():
            return Response({"error": {"message": f"渠道标识 {slug} 已存在",
                                       "code": "duplicate"}}, status=400)
        base_url = (request.data.get("base_url") or "").strip()
        if not base_url:
            return Response({"error": {"message": "base_url required", "code": "bad_request"}},
                            status=400)
        make_default = bool(request.data.get("is_default")) or not Channel.objects.exists()
        channel = Channel(
            name=name, slug=slug, base_url=base_url,
            chat_path=(request.data.get("chat_path") or "/chat/completions").strip(),
            models_path=(request.data.get("models_path") or "/models").strip(),
            key_prefix=(request.data.get("key_prefix") or "").strip(),
            auth_scheme=request.data.get("auth_scheme") or "bearer",
            default_rpm=int(request.data.get("default_rpm") or 40),
            enabled=bool(request.data.get("enabled", True)),
            is_default=make_default,
            notes=request.data.get("notes") or "",
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
        if "default_rpm" in request.data:
            channel.default_rpm = int(request.data["default_rpm"])
        if "enabled" in request.data:
            channel.enabled = bool(request.data["enabled"])
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
        rpm = int(request.data.get("rpm_limit") or channel.default_rpm or 40)
        if not key:
            return Response({"error": {"message": "api_key required", "code": "bad_request"}},
                            status=400)
        if channel.keys.filter(api_key=key).exists():
            return Response({"error": {"message": "duplicate key", "code": "duplicate"}},
                            status=400)
        if not name:
            name = f"{channel.name} Key {channel.keys.count() + 1:03d}"
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
            data["api_key"] = rec.api_key
        return Response(data)

    def patch(self, request, pk):
        rec = self._get(pk)
        if not rec:
            return Response({"detail": "not found"}, status=404)
        name = request.data.get("name")
        if name:
            rec.name = name.strip()
        if "rpm_limit" in request.data:
            rec.rpm_limit = int(request.data["rpm_limit"])
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
        if "enabled" in request.data:
            ok, msg = proxy_service.set_enabled(p, bool(request.data["enabled"]))
            if not ok:
                return Response(
                    {"error": {"message": msg, "code": "proxy_limit_exceeded"}}, status=400
                )
        for f in ("name", "protocol", "host", "port", "username", "password", "group"):
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
        rec, created = channel.models.get_or_create(model_name=name, defaults={
            "display_name": request.data.get("display_name", ""),
            "description": request.data.get("description", ""),
            "provider": request.data.get("provider") or channel.slug,
            "enabled": request.data.get("enabled", False),
        })
        return Response(ModelSerializer(rec).data, status=201 if created else 200)


class ModelSyncView(AdminRequiredMixin, APIView):
    def post(self, request):
        channel = current_channel(request)
        try:
            return Response(upstream_service.sync_models(channel))
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
        for f in ("display_name", "alias", "route_priority", "description",
                  "enabled", "status"):
            if f in request.data:
                setattr(rec, f, request.data[f])
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
        return Response({"matched": matched, "action": action})


class ProxyBatchView(AdminRequiredMixin, APIView):
    """POST {ids: [...], action: "enable"|"disable"|"delete"|"test"}"""

    def post(self, request):
        channel = current_channel(request)
        ids = _parse_ids(request)
        action = request.data.get("action")
        if not ids or action not in ("enable", "disable", "delete", "test"):
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

class UserApiKeyListView(AdminRequiredMixin, APIView):
    """用户 Key 是平台级的，跨渠道共享。"""

    def get(self, request):
        return Response(UserApiKeySerializer(UserApiKey.objects.order_by("-id"), many=True).data)

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"error": {"message": "name required", "code": "bad_request"}},
                            status=400)
        rec, raw = api_key_service.create_key(
            name, rate_limit=int(request.data.get("rate_limit") or 0)
        )
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
        if "enabled" in request.data:
            rec.enabled = bool(request.data["enabled"])
        if "rate_limit" in request.data:
            rec.rate_limit = int(request.data["rate_limit"])
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
        qs = qs[:200]
        return Response({"results": RequestLogSerializer(qs, many=True).data,
                         "channel": channel.slug})


class DashboardView(AdminRequiredMixin, APIView):
    """全渠道汇总的仪表盘指标（不限于当前渠道）。"""

    def get(self, request):
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        keys = ChannelKey.objects.all()
        proxies = Proxy.objects.all()
        logs_today = RequestLog.objects.filter(created_at__gte=today)

        key_status = {s: keys.filter(status=s).count() for s, _ in ChannelKeyStatus.choices}
        proxy_status = {s: proxies.filter(status=s).count() for s, _ in ProxyStatus.choices}
        today_count = logs_today.count()
        success_count = logs_today.filter(status="success").count()
        agg = logs_today.filter(duration_ms__gt=0).aggregate(
            avg=Avg("duration_ms"), tokens=Sum("total_tokens"))

        per_channel = []
        for c in Channel.objects.order_by("id"):
            cl = logs_today.filter(channel=c)
            n = cl.count()
            ok = cl.filter(status="success").count()
            per_channel.append({
                "slug": c.slug, "name": c.name,
                "requests_today": n,
                "success_rate": round(ok / n * 100, 1) if n else 0.0,
                "tokens_today": cl.aggregate(t=Sum("total_tokens"))["t"] or 0,
                "enabled_keys": c.keys.exclude(
                    status__in=[ChannelKeyStatus.DISABLED,
                                ChannelKeyStatus.INVALID]).count(),
                "enabled_models": c.models.filter(enabled=True).count(),
            })

        n_active_keys = keys.exclude(status=ChannelKeyStatus.DISABLED).count()
        return Response({
            "channels": per_channel,
            "channel_count": len(per_channel),
            "nvidia_keys": keys.count(),
            "enabled_keys": keys.exclude(
                status__in=[ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID]).count(),
            "proxies": proxies.count(),
            "enabled_proxies": proxies.filter(enabled=True).count(),
            "max_enabled_proxies": max(n_active_keys - 1, 0),
            "models": AIModel.objects.count(),
            "enabled_models": AIModel.objects.filter(enabled=True).count(),
            "requests_today": today_count,
            "tokens_today": agg["tokens"] or 0,
            "success_rate": round(success_count / today_count * 100, 1) if today_count else 0.0,
            "avg_latency_s": round((agg["avg"] or 0) / 1000, 2),
            "key_status": key_status,
            "proxy_status": proxy_status,
        })


class DashboardUsageView(AdminRequiredMixin, APIView):
    """Token 用量统计：跨全部渠道汇总。

    返回按天分桶、区间汇总、上一周期环比、按模型分布、按渠道分布。
    """

    def get(self, request):
        days = max(1, min(int(request.query_params.get("days", 7)), 30))
        now = timezone.localtime(timezone.now())
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

        logs = RequestLog.objects.filter(created_at__gte=start).values(
            "created_at", "model", "prompt_tokens", "completion_tokens",
            "cached_tokens", "total_tokens", "status", "duration_ms",
            "first_token_ms", "channel__name", "user_api_key__name",
        )

        totals = {"requests": 0, "success": 0, "total_tokens": 0,
                  "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        models: dict[str, dict] = {}
        channels: dict[str, dict] = {}
        keys: dict[str, dict] = {}
        sum_duration = sum_ttft = 0.0
        n_duration = n_ttft = 0

        for row in logs:
            ok = row["status"] == "success"
            key = timezone.localtime(row["created_at"]).strftime("%Y-%m-%d")
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

        channel = channel_service.resolve(
            (request.data.get("channel") or "").strip() or None)
        if channel is None:
            channel = current_channel(request)

        model = (request.data.get("model") or "").strip()
        prompt = request.data.get("prompt")
        messages = request.data.get("messages")
        if prompt and not messages:
            messages = [{"role": "user", "content": str(prompt)}]
        if not model or not messages:
            return Response({"error": {"message": "model and prompt/messages required",
                                       "code": "bad_request"}}, status=400)
        if not channel.models.filter(model_name=model, enabled=True).exists():
            return Response({"error": {"message": f"模型 {model} 不存在或未启用",
                                       "code": "model_not_found"}}, status=404)

        body = {
            k: v for k, v in request.data.items()
            if k in self.ALLOWED and k not in thinking.THINKING_PARAM_KEYS and v is not None
        }
        body["model"] = model
        body["messages"] = messages
        body.update(thinking.build_upstream(request.data, model))

        if request.data.get("stream"):
            return self._stream(body, model, channel)

        routes = build_routes(channel)
        started = timezone.now().timestamp()
        request_id = ks.new_request_id()
        log = RequestLog.objects.create(channel=channel, request_id=request_id, model=model,
                                        routes_count=len(routes))
        if not routes:
            log.status, log.http_status, log.error_type = "error", 503, "no_available_route"
            log.save()
            return Response({"error": {"message": "当前没有可用线路（没有可用的渠道 Key）",
                                       "code": "no_available_route"}}, status=503)
        import time
        t0 = time.monotonic()
        try:
            result = race_chat(routes, body)
        except AllRoutesFailed as exc:
            log.status, log.error_type = "error", "all_routes_failed"
            log.http_status = 502
            log.routes = exc.report
            log.save()
            return Response({"error": {"message": f"所有线路均失败: {exc}",
                                       "code": "upstream_error"},
                             "routes": exc.report}, status=502)
        except NoRouteAvailable:
            log.status, log.error_type = "error", "no_available_route"
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

    def _stream(self, body, model, channel):
        """SSE: race streaming connections, first valid chunk wins, rest cancelled.

        Emits a leading `data: {"meta": {...}}` event describing the winning route,
        then relays upstream chunks verbatim, terminated by data: [DONE].
        """
        import asyncio
        import json

        from django.http import StreamingHttpResponse

        from services import key_service as ks
        from services.load_balancer import build_routes
        from services.race_engine import AllRoutesFailed, NoRouteAvailable, race_stream

        routes = build_routes(channel)
        request_id = ks.new_request_id()
        log = RequestLog.objects.create(
            channel=channel, request_id=request_id, model=model,
            routes_count=len(routes), is_stream=True
        )
        # Ask upstream for usage in the last SSE chunk so we can record tokens.
        body = dict(body)
        body.setdefault("stream_options", {}).update({"include_usage": True})
        if not routes:
            log.status, log.http_status, log.error_type = "error", 503, "no_available_route"
            log.save()
            return Response({"error": {"message": "当前没有可用线路",
                                       "code": "no_available_route"}}, status=503)

        def gen():
            import time
            t0 = time.monotonic()
            loop = asyncio.new_event_loop()
            winner = None
            try:
                winner = loop.run_until_complete(race_stream(routes, body))

                duration = round((time.monotonic() - t0) * 1000, 1)
                log.status, log.http_status = "success", 200
                log.duration_ms = duration
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

                stream = winner.lines()
                usage: dict = {}
                while True:
                    chunk = loop.run_until_complete(_next_line(stream))
                    if chunk is None:
                        break
                    try:
                        payload = json.loads(chunk[5:].strip()) if chunk.startswith("data:") else {}
                        if isinstance(payload, dict) and payload.get("usage"):
                            usage = payload["usage"]
                    except Exception:  # noqa: BLE001
                        pass
                    yield chunk

                total_ms = round((time.monotonic() - t0) * 1000, 1)
                log.duration_ms = total_ms
                log.first_token_ms = duration
                log.prompt_tokens = usage.get("prompt_tokens", 0) or 0
                log.completion_tokens = usage.get("completion_tokens", 0) or 0
                log.total_tokens = usage.get("total_tokens", 0) or 0
                details = (usage.get("prompt_tokens_details") or {})
                log.cached_tokens = details.get("cached_tokens", 0) or 0
                log.save()
                yield "data: " + json.dumps({
                    "summary": {
                        "duration_ms": total_ms,
                        "first_token_ms": duration,
                        "prompt_tokens": log.prompt_tokens,
                        "completion_tokens": log.completion_tokens,
                        "total_tokens": log.total_tokens,
                        "cached_tokens": log.cached_tokens,
                    }
                }) + "\n\n"
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                log.status, log.http_status, log.error_type = "error", 502, "all_routes_failed"
                if isinstance(exc, AllRoutesFailed):
                    log.routes = exc.report
                log.save()
                yield "data: " + json.dumps({
                    "error": {"message": f"所有线路均失败: {exc}", "type": "api_error",
                              "param": None, "code": "upstream_error"}
                }) + "\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001
                log.status, log.error_type = "error", "stream_error"
                log.save()
                yield "data: " + json.dumps({
                    "error": {"message": f"stream error: {exc}", "type": "api_error",
                              "param": None, "code": "stream_error"}
                }) + "\n\n"
                yield "data: [DONE]\n\n"
            finally:
                try:
                    if winner is not None:
                        loop.run_until_complete(winner.close())
                except Exception:  # noqa: BLE001
                    pass
                loop.close()

        async def _next_line(ait):
            try:
                return await ait.__anext__()
            except StopAsyncIteration:
                return None

        response = StreamingHttpResponse(gen(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
