"""渠道 Key 管理：批量导入、RPM 限流、冷却与状态机。"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.core.models import AuthScheme, Channel, ChannelKey, ChannelKeyStatus
from services import sysconfig
from services.crypto import decrypt_secret

logger = logging.getLogger("nvidia2api.keys")

MINUTE_SECONDS = 60

# 无鉴权渠道（如 LLM7 / Zen）的“无需 Key”标记：导入时该条目成为匿名线路槽位，
# api_key 存空字符串，上游请求不携带鉴权头，但仍占用一个可调度的 Key 名额。
NO_KEY_MARKER = "@nokey"


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:4] + "****"
    return key[:10] + "*" * 8 + key[-4:]


def _key_stored_in_channel(channel: Channel, plain_key: str) -> bool:
    """按明文判断渠道里是否已存在该 Key（存储为加密值，需解密后比较）。"""
    if not plain_key:
        return False
    for stored in channel.keys.values_list("api_key", flat=True):
        if decrypt_secret(stored or "") == plain_key:
            return True
    return False


def parse_import_text(text: str) -> list[tuple[str, str, str | None]]:
    """Parse bulk import lines. Returns list of (name, key, auto_name_or_None)."""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    parsed: list[tuple[str, str, str | None]] = []
    for ln in lines:
        if "---" in ln:
            name, key = ln.split("---", 1)
            name, key = name.strip(), key.strip()
            parsed.append((name, key, None))
        else:
            parsed.append(("", ln, None))
    return parsed


def bulk_import_keys(text: str, channel: Channel) -> dict:
    """Import keys from `name---key` or bare `key` lines into a channel.

    无鉴权渠道（auth_scheme=none，如 LLM7）可导入“无需 Key”的匿名线路：
      - `名称---` / `名称---@nokey`：显式匿名，名称保留；
      - 裸行（无 `---`）：直接作为匿名线路的名称（无需再写 `---`）。
    每条匿名线路仅作为可调度的线路槽位（api_key 存空字符串，请求不上送鉴权头）。
    """
    no_auth = channel.auth_scheme == AuthScheme.NONE
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    seen_in_batch: set[str] = set()
    result = {"success": 0, "duplicate": 0, "invalid": 0, "failed": 0, "errors": []}
    label = channel.name
    auto_idx = channel.keys.count() + 1
    default_rpm = sysconfig.get("default_upstream_rpm", channel)
    for ln in lines:
        auto_named = False
        anonymous = False
        if "---" in ln:
            name, key = (p.strip() for p in ln.split("---", 1))
            if not name:
                name = f"{label} Key {auto_idx:03d}"
                auto_named = True
        elif no_auth:
            # 无鉴权渠道：裸行就是一条匿名线路的名称，不用再写 `---`；
            # 裸 `@nokey` 则自动命名。
            if ln == NO_KEY_MARKER:
                name = f"{label} Key {auto_idx:03d}"
                key, anonymous = "", True
                auto_named = True
            else:
                name, key, anonymous = ln, "", True
        else:
            key = ln
            name = f"{label} Key {auto_idx:03d}"
            auto_named = True
        # 空 key 或显式 `@nokey` 标记 → 匿名线路槽位
        if key in ("", NO_KEY_MARKER):
            key = ""
            anonymous = True
        if not anonymous and (not key or " " in key):
            result["invalid"] += 1
            result["errors"].append({"line": ln, "reason": "invalid_format"})
            continue
        # 匿名线路每个都是独立槽位，跳过重复检查（允许多条并存）
        allow_dup = bool(getattr(channel, "allow_duplicate_keys", False))
        if not anonymous and not allow_dup and (
            key in seen_in_batch or _key_stored_in_channel(channel, key)
        ):
            result["duplicate"] += 1
            continue
        if allow_dup:
            seen_in_batch.add(key)
        try:
            ChannelKey.objects.create(
                channel=channel, name=name, api_key=key,
                rpm_limit=channel.default_rpm or default_rpm,
            )
            seen_in_batch.add(key)
            if auto_named:
                auto_idx += 1
            result["success"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("import key failed")
            result["failed"] += 1
            result["errors"].append({"line": ln, "reason": str(exc)})
    return result


def available_keys(channel: Channel) -> list[ChannelKey]:
    """Keys eligible for scheduling: enabled status + not in cooldown + under RPM.

    渠道处于熔断冷却时直接返回空列表（该渠道不参与线路构建），
    请求会流向其他渠道 / 默认渠道。
    """
    from services.channel_health import is_open

    if is_open(channel):
        logger.warning("channel %s in circuit-breaker cooldown, skipping keys",
                       channel.slug)
        return []
    now = timezone.now()
    out = []
    for k in channel.keys.all():
        if k.status in (ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID):
            continue
        if k.cooldown_until and k.cooldown_until > now:
            continue
        if _key_under_rpm(k, now):
            out.append(k)
    out.sort(key=_score)
    return out


def _key_under_rpm(k: ChannelKey, now) -> bool:
    if k.minute_window_start is None:
        return True
    elapsed = (now - k.minute_window_start).total_seconds()
    if elapsed >= MINUTE_SECONDS:
        return True
    return k.minute_request_count < k.rpm_limit


def _score(k: ChannelKey):
    """Lower is better: LRU + fewer failures."""
    lru = k.last_used_at.timestamp() if k.last_used_at else 0
    return (k.failure_count, lru)


def claim_rpm_slot(key_id: int) -> bool:
    """Atomically claim one RPM slot. Uses conditional UPDATEs (no SELECT ... FOR UPDATE)
    so it is safe under SQLite's serialized write locking across threads."""
    limit = ChannelKey.objects.filter(pk=key_id).values_list(
        "rpm_limit", flat=True).first()
    if limit is None:
        return False
    if limit <= 0:
        # rpm_limit <= 0 视为不限流：直接成功且不计数。
        return True
    now = timezone.now()
    window_cutoff = now - timedelta(seconds=MINUTE_SECONDS)
    ok_states = [ChannelKeyStatus.AVAILABLE, ChannelKeyStatus.RATE_LIMITED,
                 ChannelKeyStatus.ERROR]
    base = ChannelKey.objects.filter(pk=key_id, status__in=ok_states).filter(
        Q(cooldown_until__isnull=True) | Q(cooldown_until__lte=now)
    )
    # Case 1: window stale -> reset window and claim first slot (recovers rate_limited too).
    reset = base.filter(
        Q(minute_window_start__isnull=True) | Q(minute_window_start__lte=window_cutoff)
    ).update(
        minute_window_start=now, minute_request_count=1, last_used_at=now,
        status=ChannelKeyStatus.AVAILABLE,
    )
    if reset:
        return True
    # Case 2: window active -> claim only if under rpm_limit.
    claimed = base.filter(
        minute_window_start__gt=window_cutoff,
        minute_request_count__lt=F("rpm_limit"),
    ).update(minute_request_count=F("minute_request_count") + 1, last_used_at=now)
    if claimed:
        return True
    # Over limit (or disabled/cooling): mark rate_limited if the limit was actually hit.
    ChannelKey.objects.filter(
        pk=key_id, status=ChannelKeyStatus.AVAILABLE,
        minute_window_start__gt=window_cutoff,
        minute_request_count__gte=F("rpm_limit"),
        rpm_limit__gt=0,
    ).update(status=ChannelKeyStatus.RATE_LIMITED)
    return False


def report_success(key_id: int):
    with transaction.atomic():
        key = ChannelKey.objects.select_for_update().get(pk=key_id)
        key.success_count += 1
        key.cooldown_until = None
        key.last_error = ""
        if key.status == ChannelKeyStatus.RATE_LIMITED:
            key.status = ChannelKeyStatus.AVAILABLE
        key.save(update_fields=["success_count", "cooldown_until", "last_error", "status"])


def report_failure(key_id: int, error_type: str, http_status: int = 0):
    now = timezone.now()
    cooldown_seconds = _cooldown_for(error_type, http_status, key_id)
    new_status = None
    if http_status in (401, 403):
        new_status = ChannelKeyStatus.INVALID
    elif http_status == 429:
        new_status = ChannelKeyStatus.RATE_LIMITED
    with transaction.atomic():
        key = ChannelKey.objects.select_for_update().select_related("channel").get(pk=key_id)
        breaker_off = bool(key.channel and key.channel.disable_key_invalid)
        # 匿名线路（api_key 为空）的 401/403 是"上游必须鉴权"的确定性信号：
        # 空 key 永远无法通过鉴权。不受 disable_key_invalid 影响，一律标 invalid，
        # 否则会一直留在调度池里反复 401（"开了禁用无效还是没用"的根因）。
        is_anonymous = not key.api_key
        if breaker_off and new_status == ChannelKeyStatus.INVALID and not is_anonymous:
            new_status = None
        key.failure_count += 1
        key.last_error = f"{error_type}:{http_status}" if http_status else error_type
        fields = ["failure_count", "last_error"]
        if new_status:
            key.status = new_status
            fields.append("status")
        if cooldown_seconds:
            key.cooldown_until = now + timedelta(seconds=cooldown_seconds)
            fields.append("cooldown_until")
        key.save(update_fields=fields)
    logger.info("key %s marked failure type=%s http=%s", key_id, error_type, http_status)


def _cooldown_for(error_type: str, http_status: int, key_id: int) -> int:
    if http_status == 429:
        return 60
    if error_type == "invalid_response":
        return 30
    channel = None
    key = ChannelKey.objects.filter(pk=key_id).first()
    if key is not None:
        channel = key.channel
    return int(sysconfig.get("key_cooldown_seconds", channel))


def test_key(key: ChannelKey) -> dict:
    """Lightweight upstream check: GET the channel's models endpoint with this key."""
    from services import upstream_service

    channel = key.channel
    if channel is None:
        return {"ok": False, "error": "key 未绑定渠道"}
    try:
        result = upstream_service.probe(channel, decrypt_secret(key.api_key))
        if result.get("ok"):
            report_success(key.id)
            return {"ok": True, "http_status": result.get("http_status", 0),
                    "model_count": result.get("model_count", 0)}
        report_failure(key.id, "http_error", result.get("http_status", 0))
        return {"ok": False, "http_status": result.get("http_status", 0)}
    except Exception as exc:  # noqa: BLE001
        report_failure(key.id, "network_error")
        return {"ok": False, "error": str(exc)}


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:24]
