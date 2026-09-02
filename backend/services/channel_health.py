"""渠道级自动熔断（Circuit Breaker）。

背景：单条 Key 已有冷却（429/401/403），但渠道整体故障（如上游端点失效、
IP 被墙、代理组全挂）时，每次请求都会触发 N 条线路竞速后全部失败——
浪费大量并发与上游配额。本模块在"系统级失败"（竞速全挂、5xx、线路不可用）
时累计渠道的连续失败数，达到阈值后把渠道拉进冷却窗口；冷却期间该渠道的
Key 不再参与线路构建，请求自动转向其他渠道 / 默认渠道。

恢复策略：
- 任意一次成功请求立即清零连续失败数（快速恢复）；
- 冷却结束后由调度侧自然探测（available_keys 重新纳入），不主动打上游。

阈值与冷却时长走 sysconfig（按渠道可覆盖）：
- `channel_cooldown_failures`  默认 5 次
- `channel_cooldown_seconds`   默认 120 秒
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Channel

logger = logging.getLogger("nvidia2api.channel_health")


def is_open(channel: Channel) -> bool:
    """该渠道是否处于熔断冷却（True = 不参与调度）。"""
    if channel is None:
        return False
    return bool(channel.cooldown_until and channel.cooldown_until > timezone.now())


def _key_alive_evidence(routes: list) -> bool:
    """线路明细中是否存在"账号/渠道仍活着"的证据（rate_limited / 401 / 403 / 429）。

    竞速失败但只要出现过这类 Key 级应答，就说明上游账户与渠道端点本身是通的，
    失败原因是号池容量或代理质量，而非渠道宕机。用于把"容量问题"与
    "渠道级故障"区分开，避免前者误触发熔断。
    """
    for item in routes:
        if not isinstance(item, dict):
            continue
        if item.get("error") == "rate_limited":
            return True
        if item.get("http_status") in (401, 403, 429):
            return True
    return False


def record(channel: Channel | None, success: bool, http_status: int = 0,
           error_type: str = "", routes: list | None = None) -> None:
    """按一次请求的结果更新渠道健康状态。

    只统计"系统级"失败：http >= 500 或错误类型为竞速全挂 / 线路不可用 /
    流错误。单 Key 的 401/403/429 属于 Key 级问题，不触发渠道熔断。
    注意：`no_available_route` **不计入**熔断计数——它是"渠道已熔断 /
    Key 全部不可用"的结果而非上游故障；一旦计入，熔断期间的每个 503 都会
    继续累计失败，冷却结束后瞬间再次熔断，形成自我强化的死循环。

    `routes`：本轮竞速的线路明细。若其中存在 `rate_limited` / 401 / 403 / 429
    的证据，说明**账号与渠道仍然活着**，失败源于号池容量或代理质量（429 风暴
    里夹带的 502 就属此类），不是渠道宕机——这类失败同样不计入熔断，否则
    "容量问题"会被升级成"渠道死透"，熔断期间对全池可用 Key 无差别 503。
    """
    if channel is None or not channel.pk:
        return
    if success:
        # 任意成功立即清零连续失败（传入对象可能已过期，直接按 pk 重置）
        Channel.objects.filter(pk=channel.pk).update(consecutive_failures=0)
        return
    if error_type == "no_available_route":
        # 不计入熔断：它是"渠道已熔断 / Key 全部不可用"的结果而非上游故障，
        # 计入会让熔断期间的每个 503 继续累计失败（503 也满足 http>=500），
        # 冷却结束后瞬间再次熔断，形成自我强化的死循环。
        return
    if error_type in ("stream_idle_timeout", "first_content_timeout"):
        # 流式判死超时（模型长思考静默过久 / 首字超时）是"模型延迟"，不是"渠道宕机"：
        # 上游端点、账号、网络都正常，只是这条模型推理太久。把它计入熔断，几次 kimi/R1
        # 的长思考超时就会把整个渠道熔断，熔断期间所有可用 Key 无差别 503。
        # 不计数、也不清零（它既不证明渠道健康，也不证明渠道故障）。
        return
    if routes and _key_alive_evidence(routes):
        # 竞速失败但本轮有 Key 曾应答 401/403/429（或线路明细标注 rate_limited）：
        # 账号活着、渠道端点活着，只是号池限流/代理拖垮——等价于"渠道活着"，
        # 清零连续失败（与成功同义），否则 429 风暴里夹带的 502 会把渠道熔断。
        Channel.objects.filter(pk=channel.pk).update(consecutive_failures=0)
        return
    # 只有"竞速全挂 / 上游服务错误"这类确凿的渠道级失败才累计熔断计数。
    # 不再用 `http_status >= 500` 兜底：它会把 504(流式超时)、503(无线路) 等
    # 非渠道故障误判为渠道级失败，导致"有号池却整渠道熔断"。
    systematic = error_type in ("all_routes_failed", "stream_error", "upstream_error")
    if not systematic:
        return

    from django.db.models import F

    from services import sysconfig

    threshold = int(sysconfig.get("channel_cooldown_failures", channel) or 5)
    cooldown = int(sysconfig.get("channel_cooldown_seconds", channel) or 120)
    now = timezone.now()
    with transaction.atomic():
        # SQLite 下 select_for_update 是空操作，read-modify-write 在并发下会
        # 丢计数（所有请求同时失败时最严重），改用原子 F() 递增再判定阈值。
        Channel.objects.filter(pk=channel.pk).update(
            consecutive_failures=F("consecutive_failures") + 1)
        ch = Channel.objects.get(pk=channel.pk)
        if ch.consecutive_failures >= threshold:
            # 幂等设置冷却（已冷却不重复刷新，避免持续失败延长冷却窗口）。
            # 关键：条件必须包含"冷却已过期"——只判 isnull 会让 cooldown_until
            # 首次写入后永久非空，冷却过期后即便持续失败也无法再次熔断。
            updated = Channel.objects.filter(
                Q(pk=channel.pk) & (
                    Q(cooldown_until__isnull=True) | Q(cooldown_until__lte=now))
            ).update(cooldown_until=now + timedelta(seconds=cooldown))
            if updated:
                logger.warning("channel %s tripped circuit breaker (%d failures), "
                               "cooldown %ds", ch.slug, ch.consecutive_failures,
                               cooldown)
                ch.refresh_from_db()  # 重新读取同步给调用方
            elif ch.consecutive_failures % threshold == 0:
                # 已在冷却窗口内且又累计了一轮失败：只提示，不延长冷却
                logger.info("channel %s still failing during cooldown "
                            "(%d consecutive failures), cooldown not extended",
                            ch.slug, ch.consecutive_failures)
        # 同步传入对象，避免同进程内后续调度读到过期状态
        channel.consecutive_failures = ch.consecutive_failures
        channel.cooldown_until = ch.cooldown_until
