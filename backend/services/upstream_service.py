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
                prune: bool = False) -> dict:
    """拉取渠道的模型列表并幂等 upsert 到 AIModel。

    `prune=True` 时清理"上游已不存在的同步来源模型"（详见下方裁剪逻辑）。
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

    created = existing = 0
    upstream_names: list[str] = []
    for item in body.get("data", []):
        name = item.get("id")
        if not name:
            continue
        upstream_names.append(name)
        _, was_created = channel.models.get_or_create(
            model_name=name, defaults={"provider": channel.slug}
        )
        if was_created:
            created += 1
        else:
            existing += 1

    result = {"created": created, "existing": existing,
              "total": len(body.get("data", [])), "channel": channel.slug,
              "pruned": 0}

    # 裁剪失效模型：只删「同步来源是本站（provider==channel.slug）且已禁用」
    # 且上游已不存在的模型。手动添加/仍在启用的模型一律保留。
    if prune:
        stale = channel.models.filter(
            provider=channel.slug, enabled=False,
        ).exclude(model_name__in=upstream_names)
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
