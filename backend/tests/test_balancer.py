from django.test import TestCase

from apps.core.models import Channel, ChannelKey, Proxy
from services import channel_service, proxy_service
from services.load_balancer import build_routes


class BalancerTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://example.test/v1")

    def _setup(self, n_keys, n_proxies):
        keys = [ChannelKey.objects.create(channel=self.channel, name=f"k{i}",
                                          api_key=f"nvapi-{i}") for i in range(n_keys)]
        proxies = []
        for i in range(n_proxies):
            p = Proxy.objects.create(channel=self.channel, name=f"px{i}", protocol="socks5",
                                     host=f"10.0.0.{i}", port=1000 + i)
            proxies.append(p)
        return keys, proxies

    def test_5_keys_4_proxies_5_routes(self):
        keys, proxies = self._setup(5, 20)
        enabled = 0
        for p in proxies:
            ok, _ = proxy_service.set_enabled(p, True)
            enabled += ok
        self.assertEqual(enabled, 4)

        routes = build_routes(self.channel)
        self.assertEqual(len(routes), 5)
        proxy_routes = [r for r in routes if r.proxy]
        direct = [r for r in routes if r.proxy is None]
        self.assertEqual(len(proxy_routes), 4)
        self.assertEqual(len(direct), 1)
        # one key per route
        key_ids = [r.key.id for r in routes]
        self.assertEqual(len(set(key_ids)), 5)

    def test_10_keys_20_proxies_max_9(self):
        keys, proxies = self._setup(10, 20)
        enabled = 0
        for p in proxies:
            ok, _ = proxy_service.set_enabled(p, True)
            enabled += ok
        self.assertEqual(enabled, 9)
        routes = build_routes(self.channel)
        self.assertEqual(len(routes), 10)
        self.assertEqual(len([r for r in routes if r.proxy is None]), 1)

    def test_rpm_slots_claimed_by_balancer(self):
        # 2 keys, 0 enabled proxies -> 1 direct route only; exactly one key claimed
        keys, _ = self._setup(2, 0)
        routes1 = build_routes(self.channel)
        self.assertEqual(len(routes1), 1)
        claimed = sum(1 for k in [ChannelKey.objects.get(pk=x.pk) for x in keys]
                      if k.minute_request_count == 1)
        self.assertEqual(claimed, 1)

    def test_exclude_skips_dead_key_proxy_combination(self):
        """重试排除：被判定静止的 (key_id, proxy_id) 组合不再参与下一轮竞速。"""
        # 3 keys -> 最多启用 2 个代理（启用数 <= Key 数 - 1）
        keys, proxies = self._setup(3, 3)
        for p in proxies[:2]:
            ok, _ = proxy_service.set_enabled(p, True)
            self.assertTrue(ok)

        routes = build_routes(self.channel)
        self.assertEqual(len(routes), 3)  # 2 proxies + 1 direct

        # 排除第一条代线路（key0+proxy0）后，重新构建应少一条线路，且不包含该组合
        first = routes[0]
        first_comb = (first.key.id, first.proxy.id)
        excluded = {first_comb}
        routes2 = build_routes(self.channel, exclude=excluded)
        self.assertEqual(len(routes2), len(routes) - 1)
        for r in routes2:
            self.assertNotEqual((r.key.id, r.proxy.id if r.proxy else None), first_comb)

    def test_exclude_can_skip_direct_route(self):
        """直连线路（proxy=None）同样可被排除。"""
        # 2 keys -> 最多启用 1 个代理
        keys, proxies = self._setup(2, 2)
        ok, _ = proxy_service.set_enabled(proxies[0], True)
        self.assertTrue(ok)
        routes = build_routes(self.channel)
        self.assertEqual(len(routes), 2)  # 1 proxy + 1 direct
        direct = next(r for r in routes if r.proxy is None)
        excluded = {(direct.key.id, None)}
        routes2 = build_routes(self.channel, exclude=excluded)
        self.assertEqual(len(routes2), len(routes) - 1)
        self.assertFalse(any(r.proxy is None for r in routes2))

    def test_routes_are_channel_scoped(self):
        other = Channel.objects.create(name="Zen", slug="zen",
                                       base_url="https://opencode.ai/zen/v1")
        ChannelKey.objects.create(channel=other, name="other", api_key="zen-key")
        self._setup(2, 0)
        routes = build_routes(self.channel)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].key.channel_id, self.channel.id)


class RouteCountSemanticsTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="NVIDIA", slug="nvidia", base_url="https://example.test/v1")

    def test_routes_equal_proxies_plus_one(self):
        # 30 keys, only 6 proxies enabled -> exactly 6 proxy routes + 1 direct = 7
        for i in range(30):
            ChannelKey.objects.create(channel=self.channel, name=f"k{i}",
                                      api_key=f"nvapi-x{i}")
        proxies = [
            Proxy.objects.create(channel=self.channel, name=f"p{i}", protocol="socks5",
                                 host=f"10.1.0.{i}", port=1000)
            for i in range(10)
        ]
        for p in proxies[:6]:
            ok, _ = proxy_service.set_enabled(p, True)
            assert ok
        routes = build_routes(self.channel)
        self.assertEqual(len(routes), 7)
        self.assertEqual(len([r for r in routes if r.proxy]), 6)
        self.assertEqual(len([r for r in routes if r.proxy is None]), 1)
        key_ids = [r.key.id for r in routes]
        self.assertEqual(len(set(key_ids)), 7)

    def test_no_proxies_single_direct_route(self):
        for i in range(5):
            ChannelKey.objects.create(channel=self.channel, name=f"k{i}",
                                      api_key=f"nvapi-z{i}")
        routes = build_routes(self.channel)
        self.assertEqual(len(routes), 1)
        self.assertIsNone(routes[0].proxy)


class DefaultChannelTests(TestCase):
    def test_build_routes_without_channel_uses_default(self):
        channel = channel_service.ensure_default_channel()
        ChannelKey.objects.create(channel=channel, name="k", api_key="nvapi-1")
        routes = build_routes()
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].key.channel_id, channel.id)
