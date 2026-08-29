"""Runtime system parameters.

Values live in `SystemSetting` (editable via the admin API) and fall back to
environment-driven Django settings. Read at call time so changes take effect
without a restart (except MAX_CONCURRENT_REQUESTS, which sizes a semaphore at
process start).

参数是**按渠道隔离**的：同一个 key 在不同渠道可以有不同的取值。
不传 channel 时使用平台默认渠道。
"""
from __future__ import annotations

from django.conf import settings

from apps.core.models import SystemSetting

# key -> (type, default, description)
RUNTIME_PARAMS: dict[str, tuple[str, object, str]] = {
    "default_upstream_rpm": ("int", lambda: settings.DEFAULT_NVIDIA_RPM, "渠道 Key 默认每分钟请求数"),
    "max_routes_per_request": ("int", lambda: settings.MAX_ROUTES_PER_REQUEST, "单次请求最大并行线路数"),
    "retry_count": ("int", 0, "请求失败后的自动重试次数（竞速全部失败后重试，上限 5，0=不重试）"),
    "proxy_timeout": ("float", lambda: settings.PROXY_TIMEOUT, "代理测速超时（秒）"),
    "upstream_connect_timeout": ("float", lambda: settings.UPSTREAM_CONNECT_TIMEOUT, "上游连接超时（秒）"),
    "upstream_read_timeout": ("float", lambda: settings.UPSTREAM_READ_TIMEOUT, "上游读超时（秒）"),
    "max_concurrent_requests": ("int", lambda: settings.MAX_CONCURRENT_REQUESTS, "平台最大并发请求数（需重启生效）"),
    "proxy_failure_cooldown_seconds": ("int", 60, "代理连续失败后的冷却时间（秒）"),
    "proxy_unhealthy_threshold": ("int", 3, "代理连续失败多少次后标记为 unhealthy"),
    "key_cooldown_seconds": ("int", 60, "渠道 Key 失败后冷却时间（秒）"),
    "thinking_passthrough": ("bool", True, "透传客户端的思考强度参数（reasoning_effort / chat_template_kwargs 等）"),
    "thinking_strip_models": ("str", "", "不支持思考参数的模型名子串，英文逗号分隔；命中时剥离思考参数"),
}

# 兼容旧库里已经写入的 key
LEGACY_KEY_ALIASES = {"default_nvidia_rpm": "default_upstream_rpm"}


def _normalize_key(key: str) -> str:
    return LEGACY_KEY_ALIASES.get(key, key)


def _cast(raw: str, type_name: str):
    if type_name == "int":
        return int(raw)
    if type_name == "float":
        return float(raw)
    if type_name == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on")
    return raw


def _resolve_channel(channel):
    if channel is None:
        from services import channel_service
        return channel_service.default_channel()
    return channel


def get(key: str, channel=None):
    """Current effective value for a runtime param."""
    key = _normalize_key(key)
    type_name, default, _desc = RUNTIME_PARAMS[key]
    ch = _resolve_channel(channel)
    rec = SystemSetting.objects.filter(channel=ch, key=key).first()
    if rec is None or rec.value == "":
        return default() if callable(default) else default
    try:
        return _cast(rec.value, type_name)
    except (TypeError, ValueError):
        return default() if callable(default) else default


def all_params(channel=None) -> list[dict]:
    ch = _resolve_channel(channel)
    stored = {s.key: s.value for s in SystemSetting.objects.filter(channel=ch)}
    out = []
    for key, meta in RUNTIME_PARAMS.items():
        raw = stored.get(key)
        value = get(key, ch)
        out.append({
            "key": key,
            "type": meta[0],
            "value": value,
            "default": meta[1]() if callable(meta[1]) else meta[1],
            "description": meta[2],
            # 空串表示「回落默认值」，前端据此显示未覆盖状态
            "overridden": bool(raw not in (None, "")),
        })
    return out


def set_params(updates: dict, channel=None) -> None:
    ch = _resolve_channel(channel)
    for key, value in updates.items():
        key = _normalize_key(key)
        if key not in RUNTIME_PARAMS:
            continue
        rec, _ = SystemSetting.objects.get_or_create(
            channel=ch, key=key,
            defaults={"description": RUNTIME_PARAMS[key][2]},
        )
        rec.value = "" if value is None else str(value)
        rec.save(update_fields=["value", "updated_at"])


def reset_params(keys: list[str] | None = None, channel=None) -> None:
    """清空覆盖值，回落到默认。keys 为空表示全部重置。"""
    ch = _resolve_channel(channel)
    qs = SystemSetting.objects.filter(channel=ch)
    if keys:
        qs = qs.filter(key__in=[_normalize_key(k) for k in keys])
    qs.delete()
