from django.test import TestCase

from apps.core.models import NvidiaApiKey, Proxy
from services import proxy_service
from services.load_balancer import build_routes


class BalancerTests(TestCase):
    def _setup(self, n_keys, n_proxies):
        keys = [NvidiaApiKey.objects.create(name=f"k{i}", api_key=f"nvapi-{i}") for i in range(n_keys)]
        proxies = []
        for i in range(n_proxies):
            p = Proxy.objects.create(name=f"px{i}", protocol="socks5",
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

        routes = build_routes()
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
        routes = build_routes()
        self.assertEqual(len(routes), 10)
        self.assertEqual(len([r for r in routes if r.proxy is None]), 1)

    def test_rpm_slots_claimed_by_balancer(self):
        # 2 keys, 0 enabled proxies -> 1 direct route only; exactly one key claimed
        keys, _ = self._setup(2, 0)
        routes1 = build_routes()
        self.assertEqual(len(routes1), 1)
        claimed = sum(1 for k in [NvidiaApiKey.objects.get(pk=x.pk) for x in keys]
                      if k.minute_request_count == 1)
        self.assertEqual(claimed, 1)


class RouteCountSemanticsTests(TestCase):
    def test_routes_equal_proxies_plus_one(self):
        # 30 keys, only 6 proxies enabled -> exactly 6 proxy routes + 1 direct = 7
        for i in range(30):
            NvidiaApiKey.objects.create(name=f"k{i}", api_key=f"nvapi-x{i}")
        proxies = [
            Proxy.objects.create(name=f"p{i}", protocol="socks5",
                                 host=f"10.1.0.{i}", port=1000)
            for i in range(10)
        ]
        for p in proxies[:6]:
            ok, _ = __import__("services.proxy_service", fromlist=["set_enabled"]).set_enabled(p, True)
            assert ok
        routes = build_routes()
        self.assertEqual(len(routes), 7)
        self.assertEqual(len([r for r in routes if r.proxy]), 6)
        self.assertEqual(len([r for r in routes if r.proxy is None]), 1)
        key_ids = [r.key.id for r in routes]
        self.assertEqual(len(set(key_ids)), 7)

    def test_no_proxies_single_direct_route(self):
        for i in range(5):
            NvidiaApiKey.objects.create(name=f"k{i}", api_key=f"nvapi-z{i}")
        routes = build_routes()
        self.assertEqual(len(routes), 1)
        self.assertIsNone(routes[0].proxy)
