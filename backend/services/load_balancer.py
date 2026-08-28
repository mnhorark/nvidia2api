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
    """Pick keys + proxies, claim RPM slots, return routes (0..N)."""
    cfg_max = sysconfig.get("max_routes_per_request")
    max_routes = min(max_routes or cfg_max, cfg_max)
    keys = key_service.available_keys()[:max_routes]
    proxies = proxy_service.schedulable_proxies()[: max(len(keys) - 1, 0)]

    routes: list[Route] = []
    for i, key in enumerate(keys):
        if not key_service.claim_rpm_slot(key.id):
            continue
        proxy = proxies[i] if i < len(proxies) else None
        routes.append(Route(kind="proxy" if proxy else "direct", key=key, proxy=proxy, claimed=True))

    # Guarantee at least one direct route if there are keys and no route ended direct.
    if routes and all(r.proxy is not None for r in routes) and len(routes) > 1:
        last = routes.pop()
        routes.append(Route(kind="direct", key=last.key, claimed=True))
    return routes
