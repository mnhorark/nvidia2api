"""渠道解析：把一次请求绑定到某个上游渠道。

解析优先级：`X-Channel` 头 / `?channel=` 查询参数 -> 平台默认渠道。
没有渠道时懒创建一个 NVIDIA 默认渠道，保证裸库也能直接跑起来。
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.core.models import Channel, split_endpoint

logger = logging.getLogger("nvidia2api.channel")

DEFAULT_SLUG = "nvidia"
DEFAULT_NAME = "NVIDIA"


def ensure_default_channel() -> Channel:
    """确保默认渠道存在；返回默认渠道。"""
    channel = Channel.objects.filter(is_default=True).first()
    if channel is not None:
        return channel
    channel = Channel.objects.filter(slug=DEFAULT_SLUG).first()
    if channel is not None:
        if not channel.is_default:
            channel.is_default = True
            channel.save(update_fields=["is_default", "updated_at"])
        return channel

    base_url, chat_path = split_endpoint(settings.NVIDIA_BASE_URL)
    try:
        with transaction.atomic():
            channel = Channel.objects.create(
                name=DEFAULT_NAME, slug=DEFAULT_SLUG, base_url=base_url,
                chat_path=chat_path, models_path="/models", key_prefix="nvapi",
                auth_scheme="bearer", default_rpm=settings.DEFAULT_NVIDIA_RPM,
                enabled=True, is_default=True,
            )
    except IntegrityError:
        # 并发创建时退化为读取
        return Channel.objects.filter(slug=DEFAULT_SLUG).first() or default_channel()
    logger.info("created default channel %s -> %s", channel.slug, channel.chat_url)
    return channel


def default_channel() -> Channel:
    """默认渠道：优先非熔断渠道（is_default 优先），全部熔断时回落第一个。

    熔断冷却中的渠道不参与调度，请求自动转向其他可用渠道，避免打到
    已知故障上游。
    """
    from services.channel_health import is_open

    candidates = Channel.objects.filter(enabled=True).order_by("-is_default", "id")
    for channel in candidates:
        if not is_open(channel):
            return channel
    first = candidates.first()
    if first is None:
        return ensure_default_channel()
    return first


def lookup(slug: str | None) -> Channel | None:
    """按 slug 或主键查找渠道；找不到返回 None（不做任何回落）。"""
    if not slug:
        return None
    key = str(slug).strip()
    if not key:
        return None
    channel = None
    if key.isdigit():
        channel = Channel.objects.filter(pk=int(key)).first()
    if channel is None:
        channel = Channel.objects.filter(slug__iexact=key).first()
    return channel


def resolve(slug: str | None = None) -> Channel:
    """按 slug 或主键解析渠道；解析不到回落到默认渠道。

    注意：本函数用于「没有明确指定渠道」的通用场景（管理端作用域等）。
    客户端显式通过 `/c/<slug>/` 指定渠道时**必须**用 `lookup` —— 那里静默
    回落到默认渠道会把同名模型的请求路由到错误的上游，造成串渠道与错计费。
    """
    channel = lookup(slug)
    if channel is not None:
        return channel
    if slug and str(slug).strip():
        logger.warning("unknown channel %r, falling back to default", slug)
    return default_channel()


def resolve_from_request(request) -> Channel:
    """从请求解析渠道：`X-Channel` 头优先，其次 `?channel=`。"""
    getter = getattr(request, "headers", None)
    header = ""
    if getter is not None:
        header = (getter.get("X-Channel") or "").strip()
    query = ""
    query_params = getattr(request, "query_params", None) or getattr(request, "GET", None)
    if query_params is not None:
        query = (query_params.get("channel") or "").strip()
    return resolve(header or query or None)


def list_channels() -> list[Channel]:
    ensure_default_channel()
    return list(Channel.objects.order_by("-is_default", "id"))


def create_channel(**kwargs) -> Channel:
    """创建渠道；自动拆分用户粘贴的完整端点地址。"""
    channel = Channel(**kwargs)
    channel.save()
    return channel


def test_channel(channel: Channel) -> dict:
    """探测渠道连通性：用该渠道下第一个可用 Key 打一次 /models。"""
    from services import upstream_service

    from services.crypto import decrypt_secret

    key = channel.keys.exclude(status="invalid").exclude(status="disabled").first()
    if key is None:
        return {"ok": False, "error": "该渠道下没有可用的 Key"}
    return upstream_service.probe(channel, decrypt_secret(key.api_key))


def channel_summary(channel: Channel) -> dict:
    return {
        "id": channel.id,
        "name": channel.name,
        "slug": channel.slug,
        "base_url": channel.base_url,
        "chat_url": channel.chat_url,
        "models_url": channel.models_url,
        "chat_path": channel.chat_path,
        "models_path": channel.models_path,
        "key_prefix": channel.key_prefix,
        "auth_scheme": channel.auth_scheme,
        "default_rpm": channel.default_rpm,
        "enabled": channel.enabled,
        "is_default": channel.is_default,
        "notes": channel.notes,
        "key_count": channel.keys.count(),
        "enabled_key_count": channel.keys.exclude(status="disabled").count(),
        "proxy_count": channel.proxies.count(),
        "enabled_proxy_count": channel.proxies.filter(enabled=True).count(),
        "model_count": channel.models.count(),
        "enabled_model_count": channel.models.filter(enabled=True).count(),
        "created_at": channel.created_at,
        "updated_at": channel.updated_at,
    }
