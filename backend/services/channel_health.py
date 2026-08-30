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
from django.utils import timezone

from apps.core.models import Channel

logger = logging.getLogger("nvidia2api.channel_health")


def is_open(channel: Channel) -> bool:
    """该渠道是否处于熔断冷却（True = 不参与调度）。"""
    if channel is None:
        return False
    return bool(channel.cooldown_until and channel.cooldown_until > timezone.now())


def record(channel: Channel | None, success: bool, http_status: int = 0,
           error_type: str = "") -> None:
    """按一次请求的结果更新渠道健康状态。

    只统计"系统级"失败：http >= 500 或错误类型为竞速全挂 / 线路不可用 /
    流错误。单 Key 的 401/403/429 属于 Key 级问题，不触发渠道熔断。
    """
    if channel is None or not channel.pk:
        return
    if success:
        # 任意成功立即清零连续失败（传入对象可能已过期，直接按 pk 重置）
        Channel.objects.filter(pk=channel.pk).update(consecutive_failures=0)
        return
    systematic = error_type in (
        "all_routes_failed", "no_available_route", "stream_error", "upstream_error",
    ) or (http_status and http_status >= 500)
    if not systematic:
        return

    from django.db.models import F

    from services import sysconfig

    threshold = int(sysconfig.get("channel_cooldown_failures", channel) or 5)
    cooldown = int(sysconfig.get("channel_cooldown_seconds", channel) or 120)
    with transaction.atomic():
        # SQLite 下 select_for_update 是空操作，read-modify-write 在并发下会
        # 丢计数（所有请求同时失败时最严重），改用原子 F() 递增再判定阈值。
        Channel.objects.filter(pk=channel.pk).update(
            consecutive_failures=F("consecutive_failures") + 1)
        ch = Channel.objects.get(pk=channel.pk)
        if ch.consecutive_failures >= threshold:
            # 幂等设置冷却（已冷却不重复刷新，避免持续失败延长冷却窗口）
            Channel.objects.filter(pk=channel.pk, cooldown_until__isnull=True).update(
                cooldown_until=timezone.now() + timedelta(seconds=cooldown))
            logger.warning("channel %s tripped circuit breaker (%d failures), "
                           "cooldown %ds", ch.slug, ch.consecutive_failures, cooldown)
            ch.refresh_from_db()  # cooldown 由上面 UPDATE 写入，重新读取同步给调用方
        # 同步传入对象，避免同进程内后续调度读到过期状态
        channel.consecutive_failures = ch.consecutive_failures
        channel.cooldown_until = ch.cooldown_until
