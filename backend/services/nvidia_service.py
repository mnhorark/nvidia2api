"""Thin wrapper around the NVIDIA upstream API (sync + async)."""
from __future__ import annotations

import httpx
from django.conf import settings

NVIDIA_BASE = settings.NVIDIA_BASE_URL


def list_models_raw(api_key: str, timeout: float = 30) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{NVIDIA_BASE}/models", headers=headers)
        try:
            return resp.status_code, resp.json()
        except Exception:  # noqa: BLE001
            return resp.status_code, {}


def sync_models(api_key: str | None = None) -> dict:
    """Pull model list from NVIDIA and upsert into AIModel."""
    from apps.core.models import AIModel

    key = api_key
    if key is None:
        from apps.core.models import NvidiaApiKey, NvidiaApiKeyStatus
        rec = (
            NvidiaApiKey.objects.exclude(
                status__in=[NvidiaApiKeyStatus.DISABLED, NvidiaApiKeyStatus.INVALID]
            ).order_by("failure_count", "last_used_at").first()
        )
        if not rec:
            raise ValueError("no_available_nvidia_key")
        key = rec.api_key
    status_code, body = list_models_raw(key)
    if status_code != 200 or "data" not in body:
        raise ValueError(f"upstream_error:{status_code}")
    created, existing = 0, 0
    for item in body.get("data", []):
        name = item.get("id")
        if not name:
            continue
        _, was_created = AIModel.objects.get_or_create(
            model_name=name, defaults={"provider": "nvidia"}
        )
        if was_created:
            created += 1
        else:
            existing += 1
    return {"created": created, "existing": existing, "total": len(body.get("data", []))}
