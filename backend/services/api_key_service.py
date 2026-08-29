"""User API keys: generate, hash, verify, per-key user rate limiting."""
from __future__ import annotations

import hashlib
import secrets

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.core.models import UserApiKey


def generate_key() -> str:
    return "sk-nvidia2api-" + secrets.token_hex(18)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_key(name: str, rate_limit: int = 0) -> tuple[UserApiKey, str]:
    raw = generate_key()
    rec = UserApiKey.objects.create(
        name=name, key_hash=hash_key(raw), key_prefix=raw[:22], rate_limit=rate_limit
    )
    return rec, raw


def authenticate(raw: str | None) -> UserApiKey | None:
    if not raw:
        return None
    try:
        rec = UserApiKey.objects.get(key_hash=hash_key(raw.strip()))
    except UserApiKey.DoesNotExist:
        return None
    return rec


def check_and_count(rec: UserApiKey) -> tuple[bool, str]:
    """Rate-limit + count a user request. Concurrency-safe."""
    now = timezone.now()
    with transaction.atomic():
        key = UserApiKey.objects.select_for_update().get(pk=rec.pk)
        if not key.enabled:
            return False, "disabled"
        if key.rate_limit and key.rate_limit > 0:
            if key.minute_window_start is None or (
                (now - key.minute_window_start).total_seconds() >= 60
            ):
                key.minute_window_start = now
                key.minute_request_count = 0
            if key.minute_request_count >= key.rate_limit:
                return False, "rate_limited"
        key.minute_request_count += 1
        key.total_requests += 1
        key.last_used_at = now
        key.save(update_fields=[
            "minute_window_start", "minute_request_count", "total_requests", "last_used_at"
        ])
    return True, ""


def record_result(rec: UserApiKey | None, success: bool):
    if rec is None:
        return
    field = "success_requests" if success else "failed_requests"
    UserApiKey.objects.filter(pk=rec.pk).update(
        total_requests=F("total_requests") + 1,
        **{field: F(field) + 1},
    )
