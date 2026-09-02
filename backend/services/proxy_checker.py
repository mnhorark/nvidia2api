"""Async proxy speed test and public-IP + geo lookup."""
from __future__ import annotations

import asyncio
import time

import httpx
from django.conf import settings
from django.utils import timezone

from apps.core.models import Proxy
from services.loop_offload import run_db
from services.proxy_service import report_proxy_result

# 多拨测源，互为兜底：**任何**一个源返回 HTTP 响应（哪怕 429）都说明
# 代理本身是通的——过去单点依赖 ipinfo.io，其匿名限流/故障会把整个
# 代理池误判为不可用并污染健康计数、踢出调度。
IP_PROBE_URLS = (
    "https://ipinfo.io/json",
    "https://ipapi.co/json/",
    "https://api.ipify.org?format=json",
)


def _extract_geo(data: dict) -> dict:
    """从不同拨测源的响应里提取统一字段（各源字段名不一致）。"""
    out = {
        "ip": data.get("ip") or data.get("query") or "",
        "country": data.get("country") or data.get("country_code") or "",
        "region": data.get("region") or data.get("regionName") or "",
        "city": data.get("city") or "",
    }
    isp = data.get("org") or data.get("isp") or ""
    if isp:
        out["isp"] = isp
    return {k: v for k, v in out.items() if v}


async def check_proxy(proxy: Proxy, timeout: float | None = None) -> dict:
    from services import sysconfig
    timeout = timeout or sysconfig.get("proxy_timeout")
    start = time.monotonic()
    geo: dict = {}
    got_http = False
    last_status = 0
    last_exc_name = ""
    try:
        async with httpx.AsyncClient(proxy=proxy.url, timeout=timeout) as client:
            for url in IP_PROBE_URLS:
                try:
                    resp = await client.get(url)
                except Exception as exc:  # noqa: BLE001
                    # 连接级失败（代理本身死/握手失败/超时）——换一个源再试，
                    # 全部失败才判代理不可用。
                    last_exc_name = type(exc).__name__
                    continue
                # 收到任何 HTTP 响应 = 代理链路通畅（即使目标源限流 429）
                got_http = True
                last_status = resp.status_code
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:  # noqa: BLE001
                        data = {}
                    if isinstance(data, dict):
                        geo = _extract_geo(data)
                    break
        latency_ms = (time.monotonic() - start) * 1000

        if got_http:
            await run_db(report_proxy_result, proxy.id, True,
                         latency_ms=round(latency_ms, 1))
            update: dict = {"last_check_at": timezone.now()}
            if geo.get("ip"):
                update["public_ip"] = geo["ip"]
            for f in ("country", "region", "city", "isp"):
                if geo.get(f):
                    update[f] = geo[f]
            await run_db(lambda: Proxy.objects.filter(pk=proxy.id).update(**update))
            return {
                "ok": True,
                "latency_ms": round(latency_ms, 1),
                "ip": geo.get("ip"),
                "country": geo.get("country"),
                "region": geo.get("region"),
                "city": geo.get("city"),
            }
        # 全部源连接级失败：才是真正意义的代理不可用
        await run_db(report_proxy_result, proxy.id, False)
        await run_db(lambda: Proxy.objects.filter(pk=proxy.id).update(
            last_check_at=timezone.now()))
        if last_exc_name:
            return {"ok": False, "error": last_exc_name,
                    "latency_ms": round(latency_ms, 1)}
        return {"ok": False, "http_status": last_status,
                "latency_ms": round(latency_ms, 1)}
    except Exception as exc:  # noqa: BLE001
        # 防御：client 构建等外层异常（如非法代理 URL）
        latency_ms = (time.monotonic() - start) * 1000
        await run_db(report_proxy_result, proxy.id, False)
        await run_db(lambda: Proxy.objects.filter(pk=proxy.id).update(
            last_check_at=timezone.now()))
        return {"ok": False, "error": type(exc).__name__,
                "latency_ms": round(latency_ms, 1)}


async def check_all(channel=None, timeout: float | None = None,
                    ids: list[int] | None = None) -> dict:
    qs = Proxy.objects.all() if channel is None else channel.proxies.all()
    if ids:
        qs = qs.filter(id__in=ids)
    proxies = list(qs)
    sem = asyncio.Semaphore(20)

    async def one(p):
        async with sem:
            return p.id, await check_proxy(p, timeout)

    results = await asyncio.gather(*(one(p) for p in proxies))
    ok = sum(1 for _, r in results if r.get("ok"))
    return {"total": len(proxies), "ok": ok, "failed": len(proxies) - ok}
