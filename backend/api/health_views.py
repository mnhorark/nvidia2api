"""健康检查与可观测性端点。

- `GET /healthz`              存活探针（无需鉴权，供 Docker/K8s 使用）
- `GET /api/admin/health`     全量健康信息（鉴权）
- `GET /metrics`              Prometheus 文本格式指标（只读，无需鉴权）
"""
from __future__ import annotations

from django.db import connection
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.core.models import (
    AIModel, Channel, ChannelKey, Proxy, RequestLog, UserApiKey,
)

from .auth import admin_required


def _db_ok() -> bool:
    try:
        connection.ensure_connection()
        return True
    except Exception:  # noqa: BLE001
        return False


@csrf_exempt
def liveness(request):
    """存活探针：进程在且数据库可达即 200，否则 503。"""
    if request.method != "GET":
        return HttpResponse(status=405)
    if not _db_ok():
        return JsonResponse({"status": "unhealthy", "database": False}, status=503)
    return JsonResponse({"status": "ok", "database": True})


@csrf_exempt
@admin_required
def admin_health(request):
    """全量健康信息：数据库、各资源计数、渠道状态汇总。"""
    if request.method != "GET":
        return HttpResponse(status=405)
    db_ok = _db_ok()
    payload = {
        "status": "ok" if db_ok else "unhealthy",
        "database": db_ok,
        "time": timezone.now().isoformat(),
        "counts": {
            "channels": Channel.objects.count(),
            "keys": ChannelKey.objects.count(),
            "proxies": Proxy.objects.count(),
            "models": AIModel.objects.count(),
            "user_api_keys": UserApiKey.objects.count(),
            "request_logs_24h": RequestLog.objects.filter(
                created_at__gte=timezone.now() - timezone.timedelta(days=1)
            ).count(),
        },
        "channel_health": _channel_health(),
        "key_status": _status_counts(ChannelKey),
        "proxy_status": _status_counts(Proxy),
    }
    if not db_ok:
        return JsonResponse(payload, status=503)
    return JsonResponse(payload)


def _channel_health() -> list[dict]:
    out = []
    for ch in Channel.objects.order_by("-is_default", "id"):
        out.append({
            "id": ch.id, "name": ch.name, "slug": ch.slug,
            "enabled": ch.enabled, "is_default": ch.is_default,
        })
    return out


def _status_counts(model) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in model.objects.values("status"):
        key = row["status"]
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Prometheus 指标
# ---------------------------------------------------------------------------

@csrf_exempt
def metrics(request):
    """Prometheus 文本格式指标，从数据库实时汇总。

    覆盖：
    - 请求量与状态分布（最近 24h）
    - token 用量
    - Key / Proxy / 模型 / 渠道 状态计数
    - 活跃用户 Key 数
    """
    if request.method != "GET":
        return HttpResponse(status=405)
    lines: list[str] = []

    # 24h 请求统计
    since = timezone.now() - timezone.timedelta(days=1)
    log_qs = RequestLog.objects.filter(created_at__gte=since)
    total = log_qs.count()
    lines.append("# HELP nvidia2api_requests_total 24h requests by status")
    lines.append("# TYPE nvidia2api_requests_total counter")
    by_status: dict[str, int] = {}
    for row in log_qs.values("status"):
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    for status, count in sorted(by_status.items()):
        lines.append(f'nvidia2api_requests_total{{status="{status}"}} {count}')
    lines.append(f"nvidia2api_requests_total {{status=\"total\"}} {total}")

    agg = log_qs.aggregate(
        p=Sum("prompt_tokens"), c=Sum("completion_tokens"),
        cached=Sum("cached_tokens"),
    )
    lines.append("# HELP nvidia2api_tokens_total 24h token usage")
    lines.append("# TYPE nvidia2api_tokens_total counter")
    lines.append(f'nvidia2api_tokens_total{{type="prompt"}} {agg["p"] or 0}')
    lines.append(f'nvidia2api_tokens_total{{type="completion"}} {agg["c"] or 0}')
    lines.append(f'nvidia2api_tokens_total{{type="cached"}} {agg["cached"] or 0}')

    lines.append("# HELP nvidia2api_upstream_status resource status counts")
    lines.append("# TYPE nvidia2api_upstream_status gauge")
    for name, model, field in (
        ("key", ChannelKey, "status"),
        ("proxy", Proxy, "status"),
        ("model", AIModel, "enabled"),
    ):
        counts: dict[str, int] = {}
        for row in model.objects.values(field):
            key = str(row[field])
            counts[key] = counts.get(key, 0) + 1
        for status, count in sorted(counts.items()):
            lines.append(f'nvidia2api_upstream_status{{resource="{name}",status="{status}"}} {count}')
    lines.append(f'nvidia2api_upstream_status{{resource="channel",status="enabled"}} '
                 f'{Channel.objects.filter(enabled=True).count()}')
    lines.append(f'nvidia2api_upstream_status{{resource="channel",status="total"}} '
                 f'{Channel.objects.count()}')
    lines.append(f'nvidia2api_active_user_keys {UserApiKey.objects.filter(enabled=True).count()}')

    return HttpResponse("\n".join(lines) + "\n",
                        content_type="text/plain; version=0.0.4; charset=utf-8")
