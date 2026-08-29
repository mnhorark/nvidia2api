"""Route construction: 渠道 Keys 与代理配对 + 一条直连线路。

Rules:
- 所有线路属于同一渠道。
- One request uses at most N routes, N = number of schedulable keys in the channel.
- Enabled proxies <= N - 1 (enforced on enable), so routes = proxies + 1 direct.
- One key is never used by two routes within the same request.
- RPM slots are claimed atomically so concurrent requests share the budget safely.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from apps.core.models import Channel, ChannelKey, Proxy
from services import channel_service, key_service, proxy_service, sysconfig

logger = logging.getLogger("nvidia2api.balancer")


@dataclass
class Route:
    kind: str                          # "proxy" | "direct"
    key: ChannelKey
    proxy: Proxy | None = None
    claimed: bool = False
    # 模型级端点覆盖（完整 URL 或相对路径）；None 时用渠道 chat 端点
    url_override: str | None = None

    @property
    def name(self) -> str:
        if self.kind == "direct":
            return f"direct:{self.key.name}"
        return f"{self.proxy.name}+{self.key.name}"

    @property
    def channel(self) -> Channel | None:
        return self.key.channel


def build_routes(channel: Channel | None = None,
                 max_routes: int | None = None,
                 proxy_group: int | None = None,
                 endpoint: str | None = None) -> list[Route]:
    """Build race routes for a channel.

    Route count = min(启用代理数 + 1 直连, 可用 Key 数, max_routes_per_request)。
    每个代理占一条线路，再加上恰好 1 条直连；每条线路分配不同的 Key。
    `proxy_group` 非空时，仅使用该分组内的代理。
    `endpoint` 非空时，作为模型级端点覆盖写入每条线路（完整 URL 或相对路径）。
    """
    if channel is None:
        channel = channel_service.default_channel()
    cfg_max = sysconfig.get("max_routes_per_request", channel)
    max_routes = min(max_routes or cfg_max, cfg_max)

    proxies = proxy_service.schedulable_proxies(channel, group=proxy_group)
    keys = key_service.available_keys(channel)

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
        routes.append(Route(kind="proxy" if proxy else "direct", key=key,
                            proxy=proxy, url_override=endpoint or None))

    return routes
