"""代理管理：导入、解析、启用上限、分组。全部按渠道隔离。"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from apps.core.models import Channel, ChannelKey, ChannelKeyStatus, Proxy, ProxyGroup, ProxyStatus
from services import sysconfig

logger = logging.getLogger("nvidia2api.proxy")

SUPPORTED_PROTOCOLS = {"socks5", "socks5h", "http", "https"}


def max_proxies_for_channel(channel: Channel) -> int:
    n = channel.keys.exclude(status=ChannelKeyStatus.DISABLED).count()
    return max(n - 1, 0)


def enabled_proxy_count(channel: Channel) -> int:
    return channel.proxies.filter(enabled=True).count()


def parse_proxy_url(url: str) -> dict | None:
    """Parse socks5://user:pass@host:port etc."""
    url = url.strip()
    if "://" not in url:
        url = "socks5://" + url
    p = urlparse(url)
    proto = p.scheme.lower()
    if proto not in SUPPORTED_PROTOCOLS:
        return None
    if not p.hostname or not p.port:
        return None
    return {
        "protocol": proto,
        "host": p.hostname,
        "port": p.port,
        "username": (p.username or ""),
        "password": (p.password or ""),
    }


def bulk_import_proxies(text: str, channel: Channel) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result = {"success": 0, "duplicate": 0, "invalid": 0, "failed": 0, "errors": []}
    auto_idx = channel.proxies.count() + 1
    seen: set[tuple] = set()
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
        channel = proxy.channel
        n_keys = channel.keys.exclude(status=ChannelKeyStatus.DISABLED).count()
        max_allowed = max(n_keys - 1, 0)
        current = channel.proxies.filter(enabled=True).count()
        if current >= max_allowed:
            msg = (
                f"当前渠道 {channel.name} 的 Key 数量为 {n_keys}，"
                f"最多允许启用 {max_allowed} 个代理。"
            )
            return False, msg
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
    with transaction.atomic():
        p = Proxy.objects.select_for_update().get(pk=proxy_id)
        if success:
            p.success_count += 1
            p.consecutive_failures = 0
            p.status = ProxyStatus.HEALTHY
            if latency_ms is not None:
                p.latency_ms = latency_ms
            p.cooldown_until = None
            p.save(update_fields=[
                "success_count", "consecutive_failures", "status",
                "latency_ms", "cooldown_until", "updated_at",
            ])
        else:
            p.failure_count += 1
            p.consecutive_failures += 1
            if p.consecutive_failures >= unhealthy_threshold:
                p.status = ProxyStatus.UNHEALTHY
                p.cooldown_until = now + timedelta(seconds=cooldown_seconds)
            elif p.consecutive_failures >= 1:
                p.status = ProxyStatus.DEGRADED
            p.save(update_fields=[
                "failure_count", "consecutive_failures", "status",
                "cooldown_until", "updated_at",
            ])


def schedulable_proxies(channel: Channel) -> list[Proxy]:
    """Enabled, not in cooldown, healthy-ish proxies, best first."""
    now = timezone.now()
    out = []
    for p in channel.proxies.filter(enabled=True).select_related("group"):
        if p.cooldown_until and p.cooldown_until > now:
            continue
        if p.status == ProxyStatus.UNHEALTHY:
            continue
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
