"""Runtime system parameters.

Values live in `SystemSetting` (editable via the admin API) and fall back to
environment-driven Django settings. Read at call time so changes take effect
without a restart（含 max_concurrent_requests 动态并发闸门：后台修改即时生效，
环境变量仅作为未覆盖时的初始默认）。

参数是**按渠道隔离**的：同一个 key 在不同渠道可以有不同的取值。
不传 channel 时使用平台默认渠道。
"""
from __future__ import annotations

import threading
import time as _time

from django.conf import settings
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.core.models import Channel, SystemSetting

# ---------------------------------------------------------------------------
# 读取缓存（性能关键）：get() 位于请求热路径——竞速引擎对**每条线路**读
# upstream_connect/read_timeout 与 stream_first_byte_timeout，并发闸门对**每个
# 请求**读 max_concurrent_requests。不缓存时一次 10 线路的流式请求要打 30+
# 次 SELECT（其中竞速阶段全部落在事件循环线程上）。
#
# 一致性策略与 model_registry 相同：写路径即时失效（信号 + set/reset 显式
# 调用），兜底 TTL 3s 防外部直改 DB 造成的长期陈旧。Channel 信号也失效——
# 默认渠道切换会改变 channel=None 时的参数作用域锚点。
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 3.0
_ANCHOR_KEY = ("__anchor__", None)

_cache: dict[tuple, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def invalidate() -> None:
    """清空读取缓存（写参数 / 渠道变更后调用；信号路径自动触发）。"""
    with _cache_lock:
        _cache.clear()


def _cache_get(key: tuple):
    hit = _cache.get(key)
    if hit is not None and hit[0] > _time.monotonic():
        return True, hit[1]
    return False, None


def _cache_set(key: tuple, value: object) -> None:
    with _cache_lock:
        _cache[key] = (_time.monotonic() + _CACHE_TTL_SECONDS, value)


@receiver(post_save, sender=SystemSetting)
@receiver(post_delete, sender=SystemSetting)
def _on_setting_changed(sender, **kwargs):
    invalidate()


@receiver(post_save, sender=Channel)
@receiver(post_delete, sender=Channel)
def _on_channel_changed(sender, **kwargs):
    # 默认渠道切换 / 渠道增删会改变 channel=None 的锚点解析
    invalidate()


# key -> (type, default, description, group)
# 分组用于后台设置页分区展示，避免平铺一团：
#   request=请求与重试  timeout=超时控制  stream=流式保活与掐线
#   health=健康检查与冷却  thinking=思考参数  logs=日志
RUNTIME_PARAMS: dict[str, tuple[str, object, str, str]] = {
    "default_upstream_rpm": ("int", lambda: settings.DEFAULT_NVIDIA_RPM,
                             "渠道 Key 默认每分钟请求数（0=不限流）", "request"),
    "max_concurrent_requests": ("int", lambda: settings.MAX_CONCURRENT_REQUESTS,
                                "平台同时处理的请求数上限；0=不限制", "request"),
    "max_concurrent_upstream": ("int", lambda: settings.MAX_CONCURRENT_UPSTREAM,
                                "竞速期间同时建立的上游连接上限；0=不限制"
                                "（Windows SelectorEventLoop 受限环境才需调小）", "request"),
    "max_routes_per_request": ("int", lambda: settings.MAX_ROUTES_PER_REQUEST,
                               "单次请求最大并行线路数", "request"),
    "retry_count": ("int", 0,
                    "全部线路失败后重试次数（0=不重试，上限 5）", "request"),
    "retry_backoff_seconds": ("float", 3,
                              "重试前等待秒数（0=立即重试）", "request"),
    "proxy_timeout": ("float", lambda: settings.PROXY_TIMEOUT,
                      "代理测速超时（秒）", "health"),
    "upstream_connect_timeout": ("float", lambda: settings.UPSTREAM_CONNECT_TIMEOUT,
                                 "上游连接超时（秒）", "timeout"),
    "upstream_read_timeout": ("float", lambda: settings.UPSTREAM_READ_TIMEOUT,
                              "非流式请求的上游读超时（秒）", "timeout"),
    "stream_first_byte_timeout": ("float", 180,
                                  "竞速等待首个 SSE 块超时（0=不限制）", "timeout"),
    "stream_heartbeat_interval": ("float", 20,
                                  "上游静默时发送心跳间隔（秒，0=关闭）", "stream"),
    "stream_idle_timeout": ("float", 300,
                            "胜出后无真实内容时的静默上限（0=不限制）", "stream"),
    "stream_content_idle_timeout": ("float", 0,
                                    "已产出内容后的静默上限（0=不限制）", "stream"),
    "stream_max_duration": ("float", 0,
                            "流式请求总时长上限（0=不限制）", "stream"),
    "proxy_failure_cooldown_seconds": ("int", 60,
                                       "代理失败后冷却秒数", "health"),
    "proxy_unhealthy_threshold": ("int", 3,
                                  "连续失败几次标记 unhealthy", "health"),
    "key_cooldown_seconds": ("int", 60,
                             "Key 失败后冷却秒数", "health"),
    "channel_cooldown_failures": ("int", 5,
                                  "连续失败几次触发渠道熔断", "health"),
    "channel_cooldown_seconds": ("int", 60,
                                 "渠道熔断冷却秒数", "health"),
    "log_retention_days": ("int", 30,
                           "日志保留天数（0=永不清理）", "logs"),
    "thinking_passthrough": ("bool", True,
                             "透传客户端思考参数", "thinking"),
    "thinking_strip_models": ("str", "",
                              "不支持思考的模型名子串，逗号分隔，命中则剥离", "thinking"),
    "default_thinking_effort": ("str", "high",
                                "未指定档位时的默认思考强度"
                                "（off/low/medium/high/max）", "thinking"),
}

# 兼容旧库里已经写入的 key
# first_content_timeout(旧) 语义是"胜出后等待首个正文/内容超时"——已并入
# stream_idle_timeout（胜出后未产出真实内容的静默判死，默认 120s）。
LEGACY_KEY_ALIASES = {
    "default_nvidia_rpm": "default_upstream_rpm",
    "first_content_timeout": "stream_idle_timeout",
}

# 平台级参数：语义上是全局的（进程内并发闸门等），不随渠道隔离。
# 读写都锚定平台默认渠道，否则后台设置页切到非默认渠道时，
# 修改会被写进该渠道而读取仍走默认渠道——改了不生效。
PLATFORM_SCOPED_KEYS = frozenset({
    "max_concurrent_requests",
    "max_concurrent_upstream",
})


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
    if channel is not None:
        return channel
    # 平台级参数应锚定在 is_default 渠道，而不是 default_channel()——
    # 后者在默认渠道熔断时会动态回落到别的渠道，导致「写入渠道 A、读取渠道 B」，
    # 平台级配置（thinking_passthrough / proxy_timeout 等）被静默忽略。
    # 锚点随缓存失效（Channel 信号），默认渠道切换后最多 3s 生效。
    ok, anchor = _cache_get(_ANCHOR_KEY)
    if ok:
        return anchor
    anchor = Channel.objects.filter(is_default=True).first()
    if anchor is None:
        from services import channel_service
        anchor = channel_service.default_channel()
    _cache_set(_ANCHOR_KEY, anchor)
    return anchor


def get(key: str, channel=None):
    """Current effective value for a runtime param.

    带短 TTL 缓存：热路径（竞速每线路、闸门每请求）避免每次都打 DB。
    """
    key = _normalize_key(key)
    type_name, default, _desc, _group = RUNTIME_PARAMS[key]
    ch = _resolve_channel(channel)
    cache_key = (key, getattr(ch, "pk", None))
    ok, value = _cache_get(cache_key)
    if ok:
        return value
    rec = SystemSetting.objects.filter(channel=ch, key=key).first()
    if rec is None or rec.value == "":
        value = default() if callable(default) else default
    else:
        try:
            value = _cast(rec.value, type_name)
        except (TypeError, ValueError):
            value = default() if callable(default) else default
    _cache_set(cache_key, value)
    return value


def all_params(channel=None) -> list[dict]:
    ch = _resolve_channel(channel)
    stored = {s.key: s.value for s in SystemSetting.objects.filter(channel=ch)}
    out = []
    for key, meta in RUNTIME_PARAMS.items():
        # 平台级参数读写都锚定平台默认渠道，与 set_params 保持一致
        efkey_ch = _resolve_channel(None) if key in PLATFORM_SCOPED_KEYS else ch
        raw = stored.get(key) if efkey_ch == ch else None
        if efkey_ch != ch:
            # 读平台渠道里该参数的存储值用于展示
            raw = SystemSetting.objects.filter(
                channel=efkey_ch, key=key).values_list("value", flat=True).first()
        value = get(key, efkey_ch)
        out.append({
            "key": key,
            "type": meta[0],
            "value": value,
            "default": meta[1]() if callable(meta[1]) else meta[1],
            "description": meta[2],
            "group": meta[3],
            # 空串表示「回落默认值」，前端据此显示未覆盖状态
            "overridden": bool(raw not in (None, "")),
        })
    return out


# 平台级参数定义见文件顶部（PLATFORM_SCOPED_KEYS，与 all_params 共用）


def set_params(updates: dict, channel=None) -> None:
    ch = _resolve_channel(channel)
    for key, value in updates.items():
        key = _normalize_key(key)
        if key not in RUNTIME_PARAMS:
            continue
        # 平台级参数强制锚定平台渠道（忽略传入 channel），保证读写同源
        target = _resolve_channel(None) if key in PLATFORM_SCOPED_KEYS else ch
        rec, _ = SystemSetting.objects.get_or_create(
            channel=target, key=key,
            defaults={"description": RUNTIME_PARAMS[key][2]},
        )
        rec.value = "" if value is None else str(value)
        rec.save(update_fields=["value", "updated_at"])
    # save() 信号已失效缓存；这里再显式失效一次，兜底 queryset 级旁路写入
    invalidate()


def reset_params(keys: list[str] | None = None, channel=None) -> None:
    """清空覆盖值，回落到默认。keys 为空表示全部重置。"""
    ch = _resolve_channel(channel)
    qs = SystemSetting.objects.filter(channel=ch)
    if keys:
        qs = qs.filter(key__in=[_normalize_key(k) for k in keys])
    qs.delete()
    # queryset.delete() 的信号不保证逐条送达，显式失效
    invalidate()
