"""Async proxy speed test and public-IP + geo lookup."""
from __future__ import annotations

import asyncio
import time

import httpx
from django.conf import settings
from django.utils import timezone

from apps.core.models import Proxy
from services.proxy_service import report_proxy_result

IP_INFO_URL = "https://ipinfo.io/json"


async def check_proxy(proxy: Proxy, timeout: float | None = None) -> dict:
    timeout = timeout or settings.PROXY_TIMEOUT
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(proxy=proxy.url, timeout=timeout) as client:
            resp = await client.get(IP_INFO_URL)
        latency_ms = (time.monotonic() - start) * 1000
        if resp.status_code != 200:
            report_proxy_result(proxy.id, False)
            return {"ok": False, "http_status": resp.status_code}
        data = {}
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            pass
        report_proxy_result(proxy.id, True, latency_ms=round(latency_ms, 1))
        update = {
            "last_check_at": timezone.now(),
            "public_ip": data.get("ip", "") or "",
        }
        if data.get("country"):
            update["country"] = data["country"]
        if data.get("region"):
            update["region"] = data["region"]
        if data.get("city"):
            update["city"] = data["city"]
        if data.get("org"):
            update["isp"] = data["org"]
        Proxy.objects.filter(pk=proxy.id).update(**update)
        return {
            "ok": True,
            "latency_ms": round(latency_ms, 1),
            "ip": data.get("ip"),
            "country": data.get("country"),
            "region": data.get("region"),
            "city": data.get("city"),
        }
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.monotonic() - start) * 1000
        report_proxy_result(proxy.id, False)
        Proxy.objects.filter(pk=proxy.id).update(last_check_at=timezone.now())
        return {"ok": False, "error": type(exc).__name__, "latency_ms": round(latency_ms, 1)}


async def check_all(timeout: float | None = None) -> dict:
    proxies = list(Proxy.objects.all())
    sem = asyncio.Semaphore(20)

    async def one(p):
        async with sem:
            return p.id, await check_proxy(p, timeout)

    results = await asyncio.gather(*(one(p) for p in proxies))
    ok = sum(1 for _, r in results if r.get("ok"))
    return {"total": len(proxies), "ok": ok, "failed": len(proxies) - ok}
