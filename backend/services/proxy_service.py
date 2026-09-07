"""代理管理：导入、解析、启用上限、分组。全部按渠道隔离。"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from apps.core.models import Channel, ChannelKey, ChannelKeyStatus, Proxy, ProxyGroup, ProxyStatus
from services import sysconfig

logger = logging.getLogger("nvidia2api.proxy")

SUPPORTED_PROTOCOLS = {"socks5", "socks5h", "http", "https"}


def key_counts(channel: Channel) -> tuple[int, int]:
    """(该渠道 Key 总数, 可调度 Key 数) —— 一条条件聚合同时拿到。

    可调度口径 = 排除 DISABLED / INVALID，与 `count_schedulable_keys` 完全一致
    （单一事实来源）。管理端点需要同时展示"共 N 个 Key"与"启用上限"，过去前端
    为此整拉 `/api/admin/keys`（千级 Key ≈ 128KB）只为算一个 length。
    """
    agg = channel.keys.aggregate(
        total=Count('id'),
        schedulable=Count('id', filter=~Q(
            status__in=[ChannelKeyStatus.DISABLED, ChannelKeyStatus.INVALID])),
    )
    return int(agg['total'] or 0), int(agg['schedulable'] or 0)


def count_schedulable_keys(channel: Channel) -> int:
    """可用于调度竞速的 Key 数：DISABLED / INVALID 不算数。

    旧口径把 401 已判死的 Key 也计入代理启用上限的分母，导致'理论可启用代理数'
    高于 build_routes 实际可用 Key 数，运维侧数字失真。调度侧以 available_keys
    （排除 DISABLED/INVALID/冷却）为准，这里同步排除 INVALID。
    """
    return key_counts(channel)[1]


def max_proxies_for_channel(channel: Channel) -> int:
    return max(count_schedulable_keys(channel) - 1, 0)


def enabled_proxy_count(channel: Channel) -> int:
    return channel.proxies.filter(enabled=True).count()


def parse_proxy_url(url: str) -> dict | None:
    """Parse socks5://user:pass@host:port etc.

    任何解析/取值异常都归一为「返回 None = 无效格式」——端口非数字或超出
    1..65535 时 `urlparse` 的 `.port` 会抛 ValueError，过去会让整个批量导入
    以 500 中断（且已导入的行不回滚）。
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "socks5://" + url
    try:
        p = urlparse(url)
        proto = p.scheme.lower()
        if proto not in SUPPORTED_PROTOCOLS:
            return None
        if not p.hostname:
            return None
        port = p.port
        if not port or not (0 < port < 65536):
            return None
    except ValueError:
        # port 非数字 / 超范围：urlparse 在取值时抛出
        return None
    except Exception:  # noqa: BLE001
        # 其它畸形输入（非法 IPv6 字面量等）同样按无效格式处理
        return None
    return {
        "protocol": proto,
        "host": p.hostname,
        "port": port,
        "username": (p.username or ""),
        "password": (p.password or ""),
    }


def bulk_import_proxies(text: str, channel: Channel) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result = {"success": 0, "duplicate": 0, "invalid": 0, "failed": 0, "errors": []}
    auto_idx = channel.proxies.count() + 1
    seen: set[tuple] = set()
    # 整批导入放在一个事务里：任何一行写库异常都不会留下"导入了一半"的脏数据。
    try:
        with transaction.atomic():
            return _bulk_import_proxies_locked(
                lines, channel, result, seen, auto_idx)
    except Exception as exc:  # noqa: BLE001
        logger.exception("bulk import proxies aborted, rolled back")
        return {"success": 0, "duplicate": 0, "invalid": result["invalid"],
                "failed": len(lines), "errors": [{"reason": str(exc)}]}


def _bulk_import_proxies_locked(lines, channel, result, seen, auto_idx) -> dict:
    for ln in lines:
        auto_named = False
        if "---" in ln:
            name, url = (p.strip() for p in ln.split("---", 1))
            if not name:
                name = f"代理 {auto_idx:03d}"
                auto_named = True
        else:
            url = ln
            name = f"代理 {auto_idx:03d}"
            auto_named = True
        parsed = parse_proxy_url(url)
        if not parsed:
            result["invalid"] += 1
            result["errors"].append({"line": ln, "reason": "invalid_format"})
            continue
        ident = (parsed["protocol"], parsed["host"], parsed["port"], parsed["username"])
        if ident in seen or channel.proxies.filter(
            protocol=ident[0], host=ident[1], port=ident[2], username=ident[3]
        ).exists():
            result["duplicate"] += 1
            continue
        try:
            Proxy.objects.create(channel=channel, name=name, **parsed)
            seen.add(ident)
            if auto_named:
                auto_idx += 1
            result["success"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("import proxy failed")
            result["failed"] += 1
            result["errors"].append({"line": ln, "reason": str(exc)})
    return result


def set_enabled(proxy: Proxy, enabled: bool) -> tuple[bool, str]:
    """Enforce: enabled proxies <= number of channel keys - 1."""
    if enabled and not proxy.enabled:
        # 消除 check-then-act：整个"读上限 + 读已启用数 + 置位"放进一个事务，
        # 先对渠道行做一次写操作拿到 SQLite 写锁（串行化并发的 set_enabled），
        # 再读计数与置位——两个并发启用请求不再能同时穿过上限检查。
        try:
            with transaction.atomic():
                Channel.objects.filter(pk=proxy.channel_id).update(
                    updated_at=timezone.now())
                channel = Channel.objects.get(pk=proxy.channel_id)
                n_keys = count_schedulable_keys(channel)
                max_allowed = max(n_keys - 1, 0)
                current = channel.proxies.filter(enabled=True).count()
                if current >= max_allowed:
                    msg = (
                        f"当前渠道 {channel.name} 的可调度 Key 数量为 {n_keys}，"
                        f"最多允许启用 {max_allowed} 个代理。"
                    )
                    return False, msg
                proxy.enabled = True
                proxy.status = ProxyStatus.UNKNOWN
                proxy.save(update_fields=["enabled", "status", "updated_at"])
                return True, ""
        except Exception as exc:  # 写锁竞争（database is locked）等——宁可拒绝也不可超限
            logger.warning("set_enabled aborted for proxy %s: %s", proxy.pk, exc)
            return False, "并发启用冲突，请重试"
    proxy.enabled = enabled
    proxy.status = ProxyStatus.UNKNOWN if enabled else ProxyStatus.DISABLED
    proxy.save(update_fields=["enabled", "status", "updated_at"])
    return True, ""


def report_proxy_result(proxy_id: int, success: bool, latency_ms: float | None = None):
    now = timezone.now()
    proxy = Proxy.objects.filter(pk=proxy_id).first()
    channel = proxy.channel if proxy else None
    unhealthy_threshold = sysconfig.get("proxy_unhealthy_threshold", channel)
    cooldown_seconds = sysconfig.get("proxy_failure_cooldown_seconds", channel)
    cancel_unhealthy = bool(channel and channel.disable_proxy_unhealthy)
    try:
        with transaction.atomic():
            if success:
                # 原子更新：避免并发下 read-modify-write 丢失计数/状态
                Proxy.objects.filter(pk=proxy_id).update(
                    success_count=F("success_count") + 1,
                    consecutive_failures=0,
                    status=ProxyStatus.HEALTHY,
                    cooldown_until=None,
                    **(dict(latency_ms=latency_ms) if latency_ms is not None else {}),
                )
                return
            # 原子递增计数后判定状态，保证并发失败也能准确进冷却
            Proxy.objects.filter(pk=proxy_id).update(
                failure_count=F("failure_count") + 1,
                consecutive_failures=F("consecutive_failures") + 1,
            )
            p = Proxy.objects.get(pk=proxy_id)
            if cancel_unhealthy:
                # 关闭"异常"标记：公共/不稳定代理渠道不因间歇性失败标 unhealthy / 进冷却，
                # 失败计数仍保留用于统计与降级展示，代理保持可调度。
                if p.consecutive_failures >= 1:
                    Proxy.objects.filter(pk=proxy_id).update(status=ProxyStatus.DEGRADED)
            elif p.consecutive_failures >= unhealthy_threshold:
                Proxy.objects.filter(pk=proxy_id).update(
                    status=ProxyStatus.UNHEALTHY,
                    cooldown_until=now + timedelta(seconds=cooldown_seconds),
                )
            elif p.consecutive_failures >= 1:
                Proxy.objects.filter(pk=proxy_id).update(status=ProxyStatus.DEGRADED)
    except Exception as exc:  # noqa: BLE001
        # 统计写入失败（如 SQLite 锁）只记日志，绝不连带请求/线路判定失败。
        logger.warning("report_proxy_result %s failed (swallowed): %s", proxy_id, exc)


def schedulable_proxies(channel: Channel, group: int | None = None) -> list[Proxy]:
    """Enabled, not in cooldown, healthy-ish proxies, best first.

    `group` 非空时仅返回该分组内的代理；分组内无代理时返回空列表。
    """
    now = timezone.now()
    out = []
    qs = channel.proxies.filter(enabled=True).select_related("group")
    if group is not None:
        qs = qs.filter(group_id=group)
    cancel_unhealthy = bool(channel.disable_proxy_unhealthy)
    for p in qs:
        if not cancel_unhealthy:
            if p.cooldown_until and p.cooldown_until > now:
                continue
            # B7：unhealthy 曾经是**永久**判决——上面那行冷却判断只看
            # `cooldown_until`，而这一行无条件跳过 UNHEALTHY，于是冷却到期后
            # 代理依然回不到调度池，唯一出路是有人在控制台手点测速
            # （只有 `report_proxy_result(success=True)` 会恢复 HEALTHY）。
            # 一条代理被偶发网络抖动连续打挂三次，就得等人工复检——
            # 在 1200+ 代理的池子里等于永久损失一条线路。
            #
            # 现在退化成标准熔断器的 half-open：`cooldown_until` 过期即重新
            #  eligible。真死的代理会快速失败（connect 超时 10s）并被
            # `report_proxy_result` 重新置 UNHEALTHY + 新冷却，自动回到 open。
            # 状态字段本身不改——读路径不做写操作。
        out.append(p)
    out.sort(key=lambda p: (
        p.latency_ms if p.latency_ms is not None else float("inf"),
        p.failure_count,
    ))
    return out


def add_group(channel: Channel, name: str, **kwargs) -> ProxyGroup:
    return ProxyGroup.objects.create(channel=channel, name=name, **kwargs)


def run_async(coro):
    """Run an async coroutine from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
