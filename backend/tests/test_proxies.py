from django.test import TestCase

from apps.core.models import Channel, ChannelKey, Proxy, ProxyStatus
from services import channel_service, proxy_service


def make_channel(slug="nvidia"):
    return Channel.objects.create(name=slug.upper(), slug=slug,
                                  base_url="https://example.test/v1")


def make_key(channel, i):
    return ChannelKey.objects.create(channel=channel, name=f"k{i}", api_key=f"nvapi-{i}")


class ProxyParseTests(TestCase):
    def setUp(self):
        self.channel = make_channel()

    def test_parse_variants(self):
        p = proxy_service.parse_proxy_url("socks5://user:pass@1.2.3.4:1080")
        self.assertEqual((p["protocol"], p["host"], p["port"], p["username"], p["password"]),
                         ("socks5", "1.2.3.4", 1080, "user", "pass"))
        self.assertEqual(proxy_service.parse_proxy_url("http://1.2.3.4:8080")["protocol"], "http")
        self.assertIsNone(proxy_service.parse_proxy_url("ftp://1.2.3.4:21"))
        self.assertIsNone(proxy_service.parse_proxy_url("socks5://nohost"))

    def test_import(self):
        text = "美国01---socks5://1.1.1.1:1001\n日本01---socks5://2.2.2.2:1002\nhttp://3.3.3.3:8080\nbad://x\n"
        res = proxy_service.bulk_import_proxies(text, self.channel)
        self.assertEqual(res["success"], 3)
        self.assertEqual(res["invalid"], 1)
        auto = Proxy.objects.get(host="3.3.3.3")
        self.assertTrue(auto.name.startswith("代理"))
        # duplicate within the same channel
        res2 = proxy_service.bulk_import_proxies("socks5://1.1.1.1:1001", self.channel)
        self.assertEqual(res2["duplicate"], 1)

    def test_same_proxy_allowed_in_another_channel(self):
        proxy_service.bulk_import_proxies("socks5://1.1.1.1:1001", self.channel)
        other = make_channel("zen")
        res = proxy_service.bulk_import_proxies("socks5://1.1.1.1:1001", other)
        self.assertEqual(res["success"], 1)
        self.assertEqual(Proxy.objects.filter(host="1.1.1.1").count(), 2)


class EnableLimitTests(TestCase):
    def setUp(self):
        self.channel = make_channel()

    def test_limit_is_n_minus_1(self):
        for i in range(5):
            make_key(self.channel, i)
        proxies = [
            Proxy.objects.create(channel=self.channel, name=f"p{i}", protocol="socks5",
                                 host=f"10.0.0.{i}", port=1000)
            for i in range(6)
        ]
        ok_count = 0
        last_msg = ""
        for p in proxies:
            ok, msg = proxy_service.set_enabled(p, True)
            ok_count += 1 if ok else 0
            last_msg = msg
        self.assertEqual(ok_count, 4)
        self.assertIn("最多允许启用 4 个代理", last_msg)

    def test_disable_always_allowed(self):
        make_key(self.channel, 1)
        p = Proxy.objects.create(channel=self.channel, name="p", protocol="socks5",
                                 host="1.1.1.1", port=1)
        ok, _ = proxy_service.set_enabled(p, True)
        self.assertFalse(ok)  # 1 key -> 0 proxies allowed
        ok, _ = proxy_service.set_enabled(p, False)
        self.assertTrue(ok)

    def test_limit_is_per_channel(self):
        # 当前渠道 2 个 Key 只能启用 1 个代理；另一渠道的 Key 不能给本渠道凑数
        for i in range(2):
            make_key(self.channel, i)
        other = make_channel("zen")
        for i in range(10):
            make_key(other, i)
        p1 = Proxy.objects.create(channel=self.channel, name="p1", protocol="socks5",
                                  host="1.1.1.1", port=1)
        p2 = Proxy.objects.create(channel=self.channel, name="p2", protocol="socks5",
                                  host="2.2.2.2", port=1)
        self.assertTrue(proxy_service.set_enabled(p1, True)[0])
        self.assertFalse(proxy_service.set_enabled(p2, True)[0])

    def test_failure_cooldown(self):
        p = Proxy.objects.create(channel=self.channel, name="p", protocol="socks5",
                                 host="1.1.1.1", port=1)
        for _ in range(3):
            proxy_service.report_proxy_result(p.id, False)
        p.refresh_from_db()
        self.assertEqual(p.status, ProxyStatus.UNHEALTHY)
        self.assertIsNotNone(p.cooldown_until)
        proxy_service.report_proxy_result(p.id, True, latency_ms=120)
        p.refresh_from_db()
        self.assertEqual(p.status, ProxyStatus.HEALTHY)
        self.assertEqual(p.latency_ms, 120)


class ChannelServiceTests(TestCase):
    def test_ensure_default_channel_is_idempotent(self):
        a = channel_service.ensure_default_channel()
        b = channel_service.ensure_default_channel()
        self.assertEqual(a.pk, b.pk)
        self.assertTrue(a.is_default)

    def test_resolve_unknown_slug_falls_back(self):
        default = channel_service.ensure_default_channel()
        self.assertEqual(channel_service.resolve("nope").pk, default.pk)

    def test_resolve_by_slug_and_id(self):
        default = channel_service.ensure_default_channel()
        zen = make_channel("zen")
        self.assertEqual(channel_service.resolve("zen").pk, zen.pk)
        self.assertEqual(channel_service.resolve(str(zen.id)).pk, zen.pk)
        self.assertEqual(channel_service.resolve(None).pk, default.pk)
