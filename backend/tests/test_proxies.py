from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.core.models import Channel, ChannelKey, Proxy, ProxyStatus
from services import channel_service, crypto, proxy_service


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
        # 连续失败达到默认阈值(proxy_unhealthy_threshold=3)才标 unhealthy 并进冷却
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

    def test_disable_proxy_unhealthy_no_unhealthy_or_cooldown(self):
        # 开启"关闭代理异常标记"后：连续失败只降级，不标 unhealthy、不进冷却
        self.channel.disable_proxy_unhealthy = True
        self.channel.save(update_fields=["disable_proxy_unhealthy"])
        p = Proxy.objects.create(channel=self.channel, name="p", protocol="socks5",
                                 host="1.1.1.1", port=1, enabled=True)
        for _ in range(5):
            proxy_service.report_proxy_result(p.id, False)
        p.refresh_from_db()
        self.assertEqual(p.status, ProxyStatus.DEGRADED)
        self.assertIsNone(p.cooldown_until)
        # 仍可调度（未进冷却 / 未标 unhealthy）
        self.assertIn(p.id, [x.id for x in proxy_service.schedulable_proxies(self.channel)])

    def test_disable_proxy_unhealthy_schedules_existing_unhealthy(self):
        # 存量"异常 + 冷却中"的代理：开关开启后无需等待冷却即可调度
        p = Proxy.objects.create(channel=self.channel, name="p", protocol="socks5",
                                 host="1.1.1.1", port=1, enabled=True,
                                 status=ProxyStatus.UNHEALTHY,
                                 cooldown_until=timezone.now() + timedelta(minutes=5))
        # 关闭时不调度
        self.assertNotIn(p.id, [x.id for x in proxy_service.schedulable_proxies(self.channel)])
        self.channel.disable_proxy_unhealthy = True
        self.channel.save(update_fields=["disable_proxy_unhealthy"])
        self.assertIn(p.id, [x.id for x in proxy_service.schedulable_proxies(self.channel)])


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


class SetEnabledConcurrencyTests(__import__("django.test", fromlist=["TransactionTestCase"]).TransactionTestCase):
    """M3 回归：并发启用不得突破 keys-1 上限（条件检查必须原子）。"""

    def test_concurrent_enables_respect_limit(self):
        import threading
        channel = make_channel("conc")
        ChannelKey.objects.create(channel=channel, name="k1", api_key="k1")
        ChannelKey.objects.create(channel=channel, name="k2", api_key="k2")
        proxies = [
            Proxy.objects.create(channel=channel, name=f"p{i}",
                                 host="127.0.0.1", port=11000 + i)
            for i in range(4)
        ]
        results = []
        lock = threading.Lock()

        def worker(px):
            ok, _ = proxy_service.set_enabled(px, True)
            with lock:
                results.append(ok)

        # 上限 = 2 keys - 1 = 1；4 个并发启用最多成功 1 个
        threads = [threading.Thread(target=worker, args=(p,)) for p in proxies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(sum(results), 1)
        self.assertLessEqual(
            Proxy.objects.filter(channel=channel, enabled=True).count(), 1)


class ProxyCheckerDecouplingTests(
        __import__("django.test", fromlist=["TransactionTestCase"]).TransactionTestCase):
    # run_db 在异步线程内写库；TestCase 的事务包裹会让旁线程写直接锁死，
    # 必须用 TransactionTestCase（提交可见）才能测到真实写入。
    """M6/M7 回归：拨测源 429 不等于代理死；连接级全失败才算不可用。"""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _proxy(self):
        channel = make_channel("pc")
        return Proxy.objects.create(channel=channel, name="pp",
                                    host="127.0.0.1", port=1080)

    def _fake_client(self, behavior):
        """behavior: list of ('http', status) | ('raise',) per probe URL."""
        from services import proxy_checker

        class _Resp:
            def __init__(self, status):
                self.status_code = status

            def json(self):
                return {"ip": "1.2.3.4", "country": "US"}

        class _Client:
            def __init__(self, *a, **kw):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                what = behavior[min(self.calls, len(behavior) - 1)]
                self.calls += 1
                if what[0] == "raise":
                    raise OSError("connection refused")
                return _Resp(what[1])

        return _Client

    def test_probe_429_means_proxy_alive(self):
        from services import proxy_checker
        p = self._proxy()
        fake = self._fake_client([("http", 429)])
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                proxy_checker.httpx, "AsyncClient", fake):
            res = self._run(proxy_checker.check_proxy(p, timeout=1))
        self.assertTrue(res["ok"])
        p.refresh_from_db()
        self.assertEqual(p.success_count, 1)
        self.assertEqual(p.failure_count, 0)

    def test_connection_level_failure_means_dead(self):
        from services import proxy_checker
        p = self._proxy()
        fake = self._fake_client([("raise",)])
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                proxy_checker.httpx, "AsyncClient", fake):
            res = self._run(proxy_checker.check_proxy(p, timeout=1))
        self.assertFalse(res["ok"])
        p.refresh_from_db()
        self.assertEqual(p.failure_count, 1)

    def test_first_probe_429_second_200_gets_geo(self):
        from services import proxy_checker
        p = self._proxy()
        fake = self._fake_client([("http", 429), ("http", 200)])
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                proxy_checker.httpx, "AsyncClient", fake):
            res = self._run(proxy_checker.check_proxy(p, timeout=1))
        self.assertTrue(res["ok"])
        self.assertEqual(res["ip"], "1.2.3.4")
        p.refresh_from_db()
        self.assertEqual(p.public_ip, "1.2.3.4")


class ProxySecretStorageTests(TestCase):
    """代理密码必须加密落库（2026-09-05 审查发现的覆盖盲区）。

    ChannelKey 侧早有等价守卫（test_regressions.Low4 + test_keys 的 hint 用例），
    代理侧一直没人测：`Proxy.save()` 的加密、`encrypt_secret` 的幂等、
    以及密文长度是否被列宽截断，全部处于未验证状态。
    """

    def _proxy(self, password="s3cret-pass", **kw):
        ch = kw.pop("channel", None) or make_channel()
        return Proxy.objects.create(channel=ch, name="p", protocol="socks5",
                                    host="1.2.3.4", port=1080,
                                    username="u", password=password, **kw)

    def _raw(self, p):
        """绕过实例属性，直接读库里的列。"""
        return Proxy.objects.filter(pk=p.pk).values_list("password", flat=True).first()

    def test_password_encrypted_at_rest(self):
        p = self._proxy()
        raw = self._raw(p)
        self.assertTrue(raw.startswith("enc:v1:"))
        self.assertNotIn("s3cret-pass", raw)
        self.assertEqual(crypto.decrypt_secret(raw), "s3cret-pass")

    def test_second_save_does_not_double_encrypt(self):
        """幂等：从库里读出的实例（password 已是密文）再 save() 不得套第二层。

        管理端 PATCH 就是这个形态——get() 出来改两个字段再 save()。
        若密文被二次加密，代理鉴权会静默坏掉（解密得到的是内层密文）。
        """
        p = self._proxy()
        p.name = "renamed"
        p.save()
        self.assertEqual(crypto.decrypt_secret(self._raw(p)), "s3cret-pass")

    def test_long_password_survives_column_width(self):
        """TextField 迁移（0022）的守卫：明文 270 字符 → 密文远超旧的 128 上限。

        SQLite 不校验长度，所以这条在 SQLite 上恒绿；它真正守的是
        PostgreSQL 迁移路径（AGENTS.md 第六十二节要求预留的结构）。
        """
        long_pw = "p@ssw0rd-" * 30
        p = self._proxy(password=long_pw)
        raw = self._raw(p)
        self.assertGreater(len(raw), 128, "密文应远超旧 CharField 的 128 上限")
        self.assertEqual(crypto.decrypt_secret(raw), long_pw)

    def test_proxy_url_carries_decrypted_credentials(self):
        p = self._proxy()
        self.assertEqual(p.url, "socks5://u:s3cret-pass@1.2.3.4:1080")

    def test_serializer_never_emits_real_password(self):
        from api.serializers import ProxySerializer
        data = ProxySerializer(self._proxy()).data
        self.assertNotIn("s3cret-pass", str(dict(data)))
        self.assertEqual(data["password"], "••••••")


class ProxyUnhealthyRecoveryTests(TestCase):
    """B7：unhealthy 必须是**可恢复**的熔断状态，不是永久判决。

    旧实现 `schedulable_proxies` 无条件跳过 UNHEALTHY，而 `cooldown_until`
    过期后没有任何自动路径把状态改回 HEALTHY —— 唯一出路是有人在控制台手点测速。
    一条代理被偶发抖动连续打挂三次就永久退出调度池，在 1200+ 代理的池子里
    等于静默损失线路，且前端只会显示"异常"，看不出它其实早就该被重新试探。
    """

    def setUp(self):
        self.channel = make_channel("b7")
        self.proxy = Proxy.objects.create(
            channel=self.channel, name="p0", protocol="socks5", host="1.2.3.4",
            port=1080, enabled=True, status=ProxyStatus.UNHEALTHY,
            consecutive_failures=3)

    def test_expired_cooldown_makes_it_schedulable_again(self):
        self.proxy.cooldown_until = timezone.now() - timedelta(seconds=1)
        self.proxy.save(update_fields=["cooldown_until"])
        ids = [p.id for p in proxy_service.schedulable_proxies(self.channel)]
        self.assertIn(self.proxy.id, ids,
                      "冷却已过期却仍被永久排除 —— B7 回归")

    def test_active_cooldown_still_excludes_it(self):
        """反向守卫：half-open 不等于无视冷却。"""
        self.proxy.cooldown_until = timezone.now() + timedelta(seconds=60)
        self.proxy.save(update_fields=["cooldown_until"])
        ids = [p.id for p in proxy_service.schedulable_proxies(self.channel)]
        self.assertNotIn(self.proxy.id, ids)

    def test_no_cooldown_record_means_never_blocked(self):
        """被标 unhealthy 但从未设冷却（历史行 / 手工置状态）也必须能回来。"""
        self.proxy.cooldown_until = None
        self.proxy.save(update_fields=["cooldown_until"])
        ids = [p.id for p in proxy_service.schedulable_proxies(self.channel)]
        self.assertIn(self.proxy.id, ids)

    def test_cancel_unhealthy_channel_still_bypasses_everything(self):
        """渠道级"关闭异常标记"的既有语义不变。"""
        Channel.objects.filter(pk=self.channel.pk).update(
            disable_proxy_unhealthy=True)
        self.channel.refresh_from_db()
        self.proxy.cooldown_until = timezone.now() + timedelta(seconds=60)
        self.proxy.save(update_fields=["cooldown_until"])
        ids = [p.id for p in proxy_service.schedulable_proxies(self.channel)]
        self.assertIn(self.proxy.id, ids)
