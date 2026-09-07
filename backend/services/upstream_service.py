"""上游渠道的 HTTP 调用（替代原 nvidia_service）。

所有请求都显式带上渠道：URL 与鉴权方式由 Channel 决定，
不再依赖全局的 NVIDIA_BASE_URL。
"""
from __future__ import annotations

import logging

import httpx

from apps.core.models import AuthScheme, Channel

logger = logging.getLogger("nvidia2api.upstream")


def auth_headers(channel: Channel, api_key: str) -> dict:
    """按渠道的鉴权方式生成请求头。api_key 为空（匿名线路）时不携带任何鉴权头。"""
    headers = {"Content-Type": "application/json"}
    if not api_key:
        return headers
    if channel.auth_scheme == AuthScheme.X_API_KEY:
        headers["X-API-Key"] = api_key
    elif channel.auth_scheme == AuthScheme.NONE:
        pass
    else:  # AuthScheme.BEARER
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def list_models_raw(channel: Channel, api_key: str, timeout: float = 30) -> tuple[int, dict]:
    headers = auth_headers(channel, api_key)
    headers.pop("Content-Type", None)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(channel.models_url, headers=headers)
            try:
                return resp.status_code, resp.json()
            except Exception:  # noqa: BLE001
                return resp.status_code, {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("list models via httpx failed channel=%s err=%s, trying curl_cffi",
                       channel.slug, exc)

    # 某些环境（Windows/Cloudflare/TLS）下 httpx 会卡在 TLS 握手；curl_cffi 走
    # 浏览器兼容 TLS 指纹，用它兜底。curl_cffi 不可用时保持原 0/{} 失败语义。
    try:
        from curl_cffi import requests as curl_requests

        resp = curl_requests.get(channel.models_url, headers=headers,
                                 timeout=timeout, impersonate="chrome")
        try:
            return resp.status_code, resp.json()
        except Exception:  # noqa: BLE001
            return resp.status_code, {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("list models failed channel=%s err=%s", channel.slug, exc)
        return 0, {}


def sync_models(channel: Channel, api_key: str | None = None,
                prune: bool = False, prune_only: bool = False) -> dict:
    """拉取渠道的模型列表并幂等 upsert 到 AIModel。

    `prune=True`：同步后清理"上游已不存在的同步来源模型"（provider==channel.slug）。
    `prune_only=True`：**只清理不重新同步**——拉取上游列表仅用于对比，
    不创建/更新任何本地模型，直接删除上游已不存在的同步来源模型。
    两者互斥，prune_only 优先。

    清理口径（修正自 2026-09-01 审查）：**不再要求 enabled=False**——
    此前"上游已下线但本地仍启用"的模型永远清不掉（用户反馈"同步并清理
    不可用"的根因）。同步来源（provider==channel.slug）且上游不存在的模型
    一律删除；手动添加但 provider 恰为该渠道的模型若上游真无同名，也会被清，
    个人场景可接受（如需保留请设置非 channel.slug 的 provider）。
    """
    from apps.core.models import ChannelKey, ChannelKeyStatus

    key = api_key
    if key is None:
        rec = (
            channel.keys.exclude(
                status__in=[ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID]
            ).order_by("failure_count", "last_used_at").first()
        )
        if not rec:
            raise ValueError("no_available_key")
        from services.crypto import decrypt_secret
        key = decrypt_secret(rec.api_key)

    status_code, body = list_models_raw(channel, key)
    if status_code != 200 or "data" not in body:
        raise ValueError(f"upstream_error:{status_code}")

    upstream_names = [item.get("id") for item in body.get("data", []) if item.get("id")]

    created = existing = 0
    if not prune_only:
        # 上游一次可以返回几百个模型（NVIDIA / OpenRouter 都是），原来是每个名字
        # 一次 get_or_create，同步一次就是几百条串行查询。改成一次取回已存在的
        # 名字 + 一条 bulk_create 补齐缺失的。
        # 上游名可能重复，先去重再算差集，否则 created 会虚高。
        wanted = list(dict.fromkeys(n for n in upstream_names if n))
        have = set(
            channel.models.filter(model_name__in=wanted)
            .values_list("model_name", flat=True)
        )
        missing = [n for n in wanted if n not in have]
        if missing:
            from apps.core.models import AIModel

            # ignore_conflicts：两个管理端同时同步时撞 unique_channel_model
            # 不该让整批失败。
            channel.models.bulk_create(
                [AIModel(channel=channel, model_name=n, provider=channel.slug)
                 for n in missing],
                ignore_conflicts=True,
            )
            # bulk_create 不发 post_save，而 model_registry 的缓存失效是靠信号
            # 做的——不显式清一次，新模型要等 3s TTL 才对外可见。
            from services import model_registry

            model_registry.invalidate()
        created = len(missing)
        existing = len(wanted) - created

    result = {"created": created, "existing": existing,
              "total": len(upstream_names), "channel": channel.slug,
              "pruned": 0, "prune_only": prune_only}

    # 裁剪失效模型：删除「同步来源（provider==channel.slug）且上游已不存在」
    # 的本地模型，与 enabled 状态无关（修正前仅删 enabled=False 导致清不掉）。
    if prune or prune_only:
        stale = channel.models.filter(provider=channel.slug).exclude(
            model_name__in=upstream_names)
        pruned, _ = stale.delete()
        result["pruned"] = pruned
    return result


def probe(channel: Channel, api_key: str, timeout: float = 15) -> dict:
    """轻量连通性探测：打一次 /models。"""
    status_code, body = list_models_raw(channel, api_key, timeout=timeout)
    if status_code == 200:
        count = len(body.get("data", [])) if isinstance(body, dict) else 0
        return {"ok": True, "http_status": status_code, "model_count": count}
    return {"ok": False, "http_status": status_code or 0}
