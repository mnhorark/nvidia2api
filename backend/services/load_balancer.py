"""Route construction: NVIDIA keys paired with proxies + one direct route.

Rules:
- One request uses at most N routes, N = number of schedulable keys.
- Enabled proxies <= N - 1 (enforced on enable), so routes = proxies + 1 direct.
- One NVIDIA key is never used by two routes within the same request.
- RPM slots are claimed atomically so concurrent requests share the budget safely.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings

from apps.core.models import NvidiaApiKey, Proxy
from services import key_service, proxy_service, sysconfig

logger = logging.getLogger("nvidia2api.balancer")


@dataclass
class Route:
    kind: str                          # "proxy" | "direct"
    key: NvidiaApiKey
    proxy: Proxy | None = None
    claimed: bool = False

    @property
    def name(self) -> str:
        if self.kind == "direct":
            return f"direct:{self.key.name}"
        return f"{self.proxy.name}+{self.key.name}"


def build_routes(max_routes: int | None = None) -> list[Route]:
    """Build race routes.

    Route count = min(启用代理数 + 1 直连, 可用 Key 数, max_routes_per_request)。
    每个代理占一条线路，再加上恰好 1 条直连；每条线路分配不同的 Key。
    多出来的 Key 由调度层（LRU/成功率/RPM）留给后续请求轮换，不产生冗余直连。
    """
    cfg_max = sysconfig.get("max_routes_per_request")
    max_routes = min(max_routes or cfg_max, cfg_max)

    proxies = proxy_service.schedulable_proxies()
    keys = key_service.available_keys()

    route_count = min(len(proxies) + 1, len(keys), max_routes)
    if route_count <= 0:
        return []
    proxies = proxies[: route_count - 1]

    routes: list[Route] = []
    for i in range(route_count):
        key = keys[i]
        if not key_service.claim_rpm_slot(key.id):
            continue
        proxy = proxies[i] if i < len(proxies) else None
        routes.append(Route(kind="proxy" if proxy else "direct", key=key, proxy=proxy))

    # 保证恰好 1 条直连：若最后一条恰好也是直连（即 proxies 已全部用完），无需额外处理
    # 由于 proxies 被裁剪到 route_count-1，最后一条必然是 direct。
    return routes
