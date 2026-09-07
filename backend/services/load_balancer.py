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
                 endpoint: str | None = None,
                 exclude: set | None = None,
                 exclude_proxies: set | None = None) -> list[Route]:
    """Build race routes for a channel.

    Route count = min(启用代理数 + 1 直连, 可用 Key 数, max_routes_per_request)。
    每个代理占一条线路，再加上恰好 1 条直连；每条线路分配不同的 Key。
    `proxy_group` 非空时，仅使用该分组内的代理。
    `endpoint` 非空时，作为模型级端点覆盖写入每条线路（完整 URL 或相对路径）。
    `exclude`：{(key_id, proxy_id|None)} 集合，跳过这些"Key+代理"组合——
    用于重试时排除上一轮被判定静止的线路，避免立刻又抽到同一死线路。
    `exclude_proxies`：{proxy_id} 集合，**整个代理**本轮不使用。组合级排除会被
    "同一坏代理换一把 Key"绕过，代理才是坏源大头（代理池质量差场景尤为关键），
    因此被静默掐断/竞速失败的线路，其代理也一并即时排除。
    """
    if channel is None:
        channel = channel_service.default_channel()
    cfg_max = sysconfig.get("max_routes_per_request", channel)
    max_routes = min(max_routes or cfg_max, cfg_max)

    proxies = proxy_service.schedulable_proxies(channel, group=proxy_group)
    # 整轮停用的坏代理先过滤：exclude_proxies 命中即不参与本轮任何配对，
    # 且不计入线路配额（否则"1 直连 + N 代理"会膨胀出多余的直连线路）。
    if exclude_proxies:
        proxies = [p for p in proxies if p.id not in exclude_proxies]
    keys = key_service.available_keys(channel)

    route_count = min(len(proxies) + 1, len(keys), max_routes)
    if route_count <= 0:
        return []
    # 恰好保留 1 条直连：代理最多占 route_count-1 条线路，
    # 缺代理时最后一条回落直连（保证"1 直连 + N 代理"的既有拓扑）。
    proxies = proxies[: route_count - 1]
    n_proxies = len(proxies)

    routes: list[Route] = []
    # 代理按"下一个可用"的顺序发放，而不是按线路下标取。
    # 过去若第 i 把 Key 占位（RPM claim）失败，proxies[i] 会被整轮跳过——
    # 排头的启用代理因此永不中标，实际线路数也少于预期。
    #
    # 关键顺序：**先排除后占位**。组合级 exclude 命中时既不能消耗代理，
    # 也不能 claim RPM 计数——否则重试风暴里每轮重试都把好 Key 的 RPM 额度
    # 白占（未进线路却计数），最终误判 rate_limited。claim 失败同样不消耗
    # 代理（保持 M9 语义）。
    proxy_ptr = 0
    # 线路回填：claim 竞争失败 / 组合级排除不消耗线路配额，继续用后续 Key
    # 补位，直到铺满 route_count 或 Key 池耗尽。旧实现只看 keys[:route_count]，
    # 高并发重试轮次下"5 Key 4 代理"可能实际只发出 1-2 条线路，竞速冗余
    # 名存实亡。
    ki = 0
    while len(routes) < route_count and ki < len(keys):
        key = keys[ki]
        ki += 1
        proxy = proxies[proxy_ptr] if proxy_ptr < n_proxies else None
        # 上一轮被判定死亡（静默掐断）的 Key+代理组合：本轮不参与竞速。
        # 直连（proxy=None）也可被排除：被掐线路就是 winner，其 Key 被盗用
        # 概率低，但组合级排除能同时换掉"Key 或代理"任一嫌疑。
        if exclude and (key.id, proxy.id if proxy else None) in exclude:
            # 被排除组合：跳过该 Key，不 claim（组合排除换的是 Key，代理保留
            # 给后续可用 Key，避免被排除的代理被白白消耗）
            continue
        try:
            claimed_ok = key_service.claim_rpm_slot(key.id)
        except key_service.RpmClaimUnavailable:
            # 数据库判不出来（锁 / 连接故障）。继续往后试每一把 Key 只会得到
            # 同样的结果，而且会把"DB 不可用"伪装成"整池配额耗尽"。
            # 立刻停止本轮 claim，把已经拿到的线路交出去；一条都没有时
            # 调用方会得到 no_available_route，但日志里有一条明确写着
            # DB 争用，不会被引向"配额配错了"。
            logger.warning(
                "build_routes: RPM 领取无法判定（数据库争用），本轮提前结束，"
                "已构建 %d 条线路（channel=%s）", len(routes), channel.slug)
            break
        if not claimed_ok:
            continue
        if proxy is not None:
            proxy_ptr += 1
        routes.append(Route(kind="proxy" if proxy else "direct", key=key,
                            proxy=proxy, claimed=True,
                            url_override=endpoint or None))

    return routes
