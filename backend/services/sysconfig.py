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

# key -> (type, default, description, group)
# 分组用于后台设置页分区展示，避免平铺一团：
#   request=请求与重试  timeout=超时控制  stream=流式保活与掐线
#   health=健康检查与冷却  thinking=思考参数  logs=日志
RUNTIME_PARAMS: dict[str, tuple[str, object, str, str]] = {
    "default_upstream_rpm": ("int", lambda: settings.DEFAULT_NVIDIA_RPM,
                             "渠道 Key 默认每分钟请求数（RPM）", "request"),
    "max_routes_per_request": ("int", lambda: settings.MAX_ROUTES_PER_REQUEST,
                               "单次请求最大并行线路数（1 直连 + N 代理，受可用 Key 数约束）", "request"),
    "retry_count": ("int", 2,
                    "竞速全部线路失败、或胜出线路被静默掐断后的自动重试次数（上限 5，0=不重试）。"
                    "配合短静默阈值：假死线路快速掐断后多换几次线路；竞速已并行全部线路，"
                    "重试主要兜底瞬时故障与换掉假死线路", "request"),
    "proxy_timeout": ("float", lambda: settings.PROXY_TIMEOUT,
                      "代理测速超时（秒）", "health"),
    "upstream_connect_timeout": ("float", lambda: settings.UPSTREAM_CONNECT_TIMEOUT,
                                 "上游连接超时（秒），覆盖连接阶段（DNS/建连/代理握手）", "timeout"),
    "upstream_read_timeout": ("float", lambda: settings.UPSTREAM_READ_TIMEOUT,
                              "非流式请求的上游读超时（秒），也是该线路请求的总读预算", "timeout"),
    "stream_first_byte_timeout": ("float", 90,
                                  "流式竞速：连接成功后等待首个有效 SSE 块的最长超时（秒）。"
                                  "竞速是并行的，等待窗口稍大只会让慢速首块模型多一次机会，"
                                  "不浪费其它线路；超时仍视为该线路死线（可换线重试）；0=不限制", "timeout"),
    "stream_heartbeat_interval": ("float", 20,
                                  "流式请求：上游静默超过该时长时向客户端发送 SSE 心跳（: keep-alive），"
                                  "防止 NAT/负载均衡/客户端把连接误判为死；0=关闭", "stream"),
    "stream_probe_interval": ("float", 30,
                              "流式请求：判死的「心跳探测」周期（秒），等价 WebSocket 的 Pong 超时。"
                              "SSE 是 HTTP 单向流，无应用层 Pong 帧，因此以「单个周期内无任何字节」"
                              "视为一次心跳失败；配合 stream_max_idle_probes 连续失败才判死。"
                              "任何数据（含思考 token）到达即清零重计，生成慢不会误杀。"
                              "节点质量差/长静默思考场景建议 30s 起步，25×30~90s 总容忍", "stream"),
    "stream_max_idle_probes": ("int", 3,
                               "流式请求：连续多少次心跳探测失败（约 interval×count 秒无任何数据）"
                               "判定线路真死。默认 30s×3≈90s；想更快踢坏节点可 20s×2≈40s。"
                               "未交付正文则换线重试，已交付正文则干净收尾", "stream"),
    "stream_max_duration": ("float", 0,
                            "流式请求总时长上限（秒），兜底防僵尸流；0=不限制", "stream"),
    "proxy_failure_cooldown_seconds": ("int", 60,
                                       "代理连续失败后的冷却时间（秒）", "health"),
    "proxy_unhealthy_threshold": ("int", 5,
                                  "代理连续失败多少次后标记为 unhealthy（代理池质量差时放宽，"
                                  "避免间歇性失败过快耗尽可用代理）", "health"),
    "key_cooldown_seconds": ("int", 60,
                             "渠道 Key 失败后冷却时间（秒）", "health"),
    "channel_cooldown_failures": ("int", 5,
                                  "渠道连续系统级失败多少次后自动熔断", "health"),
    "channel_cooldown_seconds": ("int", 120,
                                 "渠道熔断冷却时间（秒）", "health"),
    "log_retention_days": ("int", 30,
                           "请求日志保留天数（0 = 永不清理，超过此期限的日志会被 cleanlogs 清理）", "logs"),
    "thinking_passthrough": ("bool", True,
                             "透传客户端的思考强度参数（reasoning_effort / chat_template_kwargs 等）", "thinking"),
    "thinking_strip_models": ("str", "",
                              "不支持思考参数的模型名子串，英文逗号分隔；命中时剥离思考参数", "thinking"),
    "default_thinking_effort": ("str", "high",
                                "客户端只开启思考但未指定档位时，自动映射的思考强度"
                                "（off/low/medium/high/max）。默认 high 与 NVIDIA/DeepSeek 官方默认一致；"
                                "Kimi-K3/DeepSeek-R1 等模型由能力表内建默认（max）优先，无需在此配置", "thinking"),
}

# 兼容旧库里已经写入的 key
# first_content_timeout(旧) -> stream_first_byte_timeout(新)：旧语义是"流式等待首个正文超时"，
# 新版拆分为 首字节超时(竞速阶段) + 心跳/停滞超时(胜出后阶段)，旧配置自动映射到首字节超时。
# 已下线的 key（max_concurrent_requests 运行时版 / stream_read_timeout）在旧库里可能残留
# SystemSetting 行，它们已不再被注册表引用，get()/all_params() 会自动忽略，无需手工清理。
LEGACY_KEY_ALIASES = {
    "default_nvidia_rpm": "default_upstream_rpm",
    "first_content_timeout": "stream_first_byte_timeout",
}


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
        # 平台级参数应锚定在 is_default 渠道，而不是 default_channel()——
        # 后者在默认渠道熔断时会动态回落到别的渠道，导致「写入渠道 A、读取渠道 B」，
        # 平台级配置（thinking_passthrough / proxy_timeout 等）被静默忽略。
        from apps.core.models import Channel
        anchor = Channel.objects.filter(is_default=True).first()
        if anchor is not None:
            return anchor
        from services import channel_service
        return channel_service.default_channel()
    return channel


def get(key: str, channel=None):
    """Current effective value for a runtime param."""
    key = _normalize_key(key)
    type_name, default, _desc, _group = RUNTIME_PARAMS[key]
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
            "group": meta[3],
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
