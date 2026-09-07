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
from services.crypto import decrypt_secret, mask_secret

logger = logging.getLogger("nvidia2api.keys")

MINUTE_SECONDS = 60

# 无鉴权渠道（如 LLM7 / Zen）的“无需 Key”标记：导入时该条目成为匿名线路槽位，
# api_key 存空字符串，上游请求不携带鉴权头，但仍占用一个可调度的 Key 名额。
NO_KEY_MARKER = "@nokey"


def mask_key(key: str) -> str:
    """脱敏展示位。**委托 crypto.mask_secret，单一事实来源**。

    历史上这里是逐字复制的第二份实现（两份连 docstring 都互相引用），
    任何一份改动口径（比如前 10 后 4 的长度阈值）都会让"列表页 hint"
    与"接口即时脱敏"两种展示形态不一致——而它们必须一致，用户会拿
    脱敏串去比对到底是哪把 Key。
    """
    return mask_secret(key)


def _stored_plain_keys(channel: Channel) -> set[str]:
    """渠道内已存 Key 的明文集合（存储为加密值，需逐条解密）。

    批量导入时**只调用一次**再复用：原来每行都全量解密比对，导入 n 行、
    渠道已有 m 把 Key 就是 O(n×m) 次 Fernet 解密，几百行时明显卡顿。
    """
    return {decrypt_secret(stored or "")
            for stored in channel.keys.values_list("api_key", flat=True)}


def _key_stored_in_channel(channel: Channel, plain_key: str) -> bool:
    """该明文 Key 是否已存在于渠道内（去重检查）。

    存储为密文，必须逐条解密比对；重复导入防护使用，高频路径（批量导入）
    请改用 `_stored_plain_keys` 一次性构建集合后自行 in 判断。
    """
    if not plain_key:
        return False
    return plain_key in _stored_plain_keys(channel)


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
    # 已有明文集合只解密构建一次（惰性：确实要做去重时才建）
    existing: set[str] | None = None
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
        if not anonymous and not allow_dup:
            if existing is None:
                existing = _stored_plain_keys(channel)
            if key in seen_in_batch or key in existing:
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
            if existing is not None:
                existing.add(key)
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


class RpmClaimUnavailable(RuntimeError):
    """RPM 领取**无法判定**（数据库锁 / 连接故障等瞬时错误）。

    必须与"返回 False = 这个 Key 配额耗尽"区分开。旧实现把两者坍缩成同一个
    `False`，于是 SQLite 写争用期间 `build_routes` 看每个 Key 都像被限流，
    整批 `continue` 后返回空线路 → `no_available_route` → 503，
    而日志里与真实限流长得一模一样，排查时会被引向"配额配置错了"。
    """


def claim_rpm_slot(key_id: int) -> bool:
    """Atomically claim one RPM slot. Uses conditional UPDATEs (no SELECT ... FOR UPDATE)
    so it is safe under SQLite's serialized write locking across threads.

    返回 True = 领到名额；False = 该 Key 确实不可用（耗尽 / 禁用 / 冷却中）。
    数据库层面判不出来时抛 `RpmClaimUnavailable`，**不伪装成 False**。
    """
    try:
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
    except RpmClaimUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # DB 锁等瞬时错误**不能**伪装成"这个 Key 被限流了"：那会让 SQLite
        # 写争用表现为整池配额耗尽 → no_available_route → 503，且与真实限流
        # 在日志里无法区分。抛出去让调用方（build_routes）自己决定降级方式。
        logger.warning("claim_rpm_slot %s unavailable (DB error): %s", key_id, exc)
        raise RpmClaimUnavailable(f"claim_rpm_slot({key_id}) unavailable") from exc


def release_rpm_slot(key_id: int) -> None:
    """退还一次已申领但最终未被线路使用的 RPM 槽位。

    场景：build_routes 已为每条线路 claim 名额后，全局上游并发闸门
    （_reserve_upstream）不足的线路被整条丢弃——若不回滚，全局拥塞期
    （恰是最需要保护 RPM 配额的时刻）会按比率虚耗各 Key 的分钟配额，
    加速集体 429/误判 rate_limited。条件 UPDATE + F()-1，不会减成负数。
    """
    try:
        ChannelKey.objects.filter(
            pk=key_id, minute_request_count__gt=0,
        ).update(minute_request_count=F("minute_request_count") - 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("release_rpm_slot %s failed (swallowed): %s", key_id, exc)


def report_success(key_id: int):
    """记录一次成功。统计写入失败（如 SQLite 锁）只记日志，绝不连带请求失败。"""
    try:
        with transaction.atomic():
            ChannelKey.objects.filter(pk=key_id).update(
                success_count=F("success_count") + 1,
                cooldown_until=None,
                last_error="",
            )
            # 限流/异常态随成功自动恢复为可用
            ChannelKey.objects.filter(
                pk=key_id,
                status__in=[ChannelKeyStatus.RATE_LIMITED, ChannelKeyStatus.ERROR],
            ).update(status=ChannelKeyStatus.AVAILABLE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("report_success %s failed (swallowed): %s", key_id, exc)


def report_failure(key_id: int, error_type: str, http_status: int = 0):
    """记录一次失败（计数/状态/冷却）。统计写入失败只记日志，绝不连带请求失败。"""
    try:
        now = timezone.now()
        cooldown_seconds = _cooldown_for(error_type, http_status, key_id)
        new_status = None
        if http_status in (401, 403):
            new_status = ChannelKeyStatus.INVALID
        elif http_status == 429:
            new_status = ChannelKeyStatus.RATE_LIMITED
        with transaction.atomic():
            ChannelKey.objects.filter(pk=key_id).update(
                failure_count=F("failure_count") + 1,
                last_error=f"{error_type}:{http_status}" if http_status else error_type,
            )
            key = ChannelKey.objects.select_related("channel").get(pk=key_id)
            breaker_off = bool(key.channel and key.channel.disable_key_invalid)
            # disable_key_invalid 打开（公共/匿名 Key 渠道）时，401/403 一律不标无效，
            # 匿名空 Key 同样适用：公共上游的 401/403 常是间歇性的（限流伪装/公共端
            # 波动），永久踢出会掏空号池。改为保留"限流与冷却"——cooldown_until 照常
            # 设置、failure_count 照常累计，因此不会无限 401：冷却期间不调度、失败率
            # 升高后排到线路末尾，冷却结束自动回归调度池。
            if breaker_off and new_status == ChannelKeyStatus.INVALID:
                new_status = None
            fields: dict = {}
            if new_status:
                fields["status"] = new_status
            if cooldown_seconds:
                fields["cooldown_until"] = now + timedelta(seconds=cooldown_seconds)
            if fields:
                ChannelKey.objects.filter(pk=key_id).update(**fields)
        logger.info("key %s marked failure type=%s http=%s", key_id, error_type, http_status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("report_failure %s failed (swallowed): %s", key_id, exc)


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
