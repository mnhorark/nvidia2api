"""User API keys: generate, hash, verify, per-key user rate limiting."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from apps.core.models import UserApiKey


def generate_key() -> str:
    return "sk-nvidia2api-" + secrets.token_hex(18)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_key(name: str, rate_limit: int = 0, quota: int = 0) -> tuple[UserApiKey, str]:
    raw = generate_key()
    rec = UserApiKey.objects.create(
        name=name, key_hash=hash_key(raw), key_prefix=raw[:22], rate_limit=rate_limit,
        quota=quota,
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
    """Rate-limit + count a user request. Concurrency-safe.

    SQLite 下 select_for_update 是空操作、且先读后写的事务会触发锁升级
    （database is locked），因此完全复用 claim_rpm_slot 的「条件 UPDATE 原子占位」
    模式：每次 UPDATE 自身原子，分支判定用 SQL 条件兜底，读到的旧值仅作提示。
    """
    now = timezone.now()
    window_cutoff = now - timedelta(seconds=60)
    fresh = UserApiKey.objects.filter(pk=rec.pk).values(
        "enabled", "rate_limit", "minute_window_start", "minute_request_count",
    ).first()
    if fresh is None or not fresh["enabled"]:
        return False, "disabled"
    limit = fresh["rate_limit"] or 0
    if limit > 0:
        ws = fresh["minute_window_start"]
        stale = ws is None or (now - ws).total_seconds() >= 60
        claimed = False
        if stale:
            # 窗口过期：原子重置并占第 1 个槽（同时自动恢复被限流的 key）
            claimed = UserApiKey.objects.filter(
                Q(minute_window_start__isnull=True) |
                Q(minute_window_start__lte=window_cutoff),
                pk=rec.pk,
            ).update(minute_window_start=now, minute_request_count=1)
        if not claimed:
            # 窗口被并发请求刚重置过：回到「未达上限才 +1」的原子占位
            claimed = UserApiKey.objects.filter(
                pk=rec.pk, minute_request_count__lt=limit,
            ).update(minute_request_count=F("minute_request_count") + 1)
        if not claimed:
            return False, "rate_limited"
    # 请求计数与最近使用时间（不影响限流判定，合并到一次 UPDATE）
    UserApiKey.objects.filter(pk=rec.pk).update(
        total_requests=F("total_requests") + 1, last_used_at=now)
    return True, ""


def record_result(rec: UserApiKey | None, success: bool):
    if rec is None:
        return
    # total_requests 已在 check_and_count 计入（每个鉴权通过的尝试计数一次），
    # 这里只累加成功/失败结果，避免 total 被双倍计数。
    field = "success_requests" if success else "failed_requests"
    UserApiKey.objects.filter(pk=rec.pk).update(
        **{field: F(field) + 1},
    )


# ---------------------------------------------------------------------------
# Token 额度（quota）
# ---------------------------------------------------------------------------

def quota_enabled(rec: UserApiKey) -> bool:
    return rec.quota > 0


def check_quota(rec: UserApiKey) -> tuple[bool, str]:
    """请求前置检查：额度耗尽则拒绝。返回 (ok, reason)。

    注意：这是"读后判"快照，仅供 UI 展示等参考场景。入口拦截必须改用
    `claim_quota`（原子预占），否则 N 个并发在途请求可同时通过检查导致超扣。
    """
    if not quota_enabled(rec):
        return True, ""
    fresh = UserApiKey.objects.filter(pk=rec.pk).values("quota", "used_quota").first()
    if fresh and fresh["used_quota"] >= fresh["quota"]:
        return False, "quota_exceeded"
    return True, ""


def claim_quota(rec: UserApiKey) -> tuple[bool, str]:
    """原子预占 1 token 额度。返回 (ok, reason)。

    用条件 UPDATE（WHERE used_quota < quota）把"检查 + 占位"合并为一条
    语句，消除 check-then-act 竞态：并发请求再多也不会越过 quota。
    预占的 1 token 由 record_usage 结算时扣除（reservation 参数）。
    """
    if not quota_enabled(rec):
        return True, ""
    updated = (
        UserApiKey.objects
        .filter(pk=rec.pk, used_quota__lt=F("quota"))
        .update(used_quota=F("used_quota") + 1)
    )
    if updated:
        return True, ""
    return False, "quota_exceeded"


def record_usage(rec: UserApiKey | None, prompt_tokens: int = 0,
                 completion_tokens: int = 0, cached_tokens: int = 0,
                 reservation: int = 0) -> None:
    """请求结束后累计 token 消耗。

    缓存 token 不占用额度（视为免费/折扣），与主流中转平台计费口径一致。
    所有 token 计数先做负值钳制——恶意/异常上游返回负数 usage 不得抵扣
    （否则等于给客户端"充值"）。reservation 为 claim_quota 预占的额度，
    结算时从实际计费中扣除；delta 为负即退还预占（如下游失败请求）。
    """
    if rec is None or not quota_enabled(rec):
        return
    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    cached = max(int(cached_tokens or 0), 0)
    billed = max(prompt - cached, 0) + completion
    delta = billed - int(reservation or 0)
    if delta == 0:
        return
    UserApiKey.objects.filter(pk=rec.pk).update(
        used_quota=F("used_quota") + delta)
    # 防御性下限：并发结算/异常参数下不允许出现负额度
    UserApiKey.objects.filter(pk=rec.pk, used_quota__lt=0).update(used_quota=0)
