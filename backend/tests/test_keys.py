import threading
from datetime import timedelta

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.core.models import Channel, ChannelKey, ChannelKeyStatus
from services import channel_service, key_service
from services.crypto import decrypt_secret


def _plain_keys(qs):
    """API Key 现在加密存储，按明文取值需解密后比较。"""
    return {decrypt_secret(k.api_key or ""): k for k in qs}


def _ch():
    return channel_service.ensure_default_channel()


class KeyImportTests(TestCase):
    def test_named_and_bare_formats(self):
        text = "主账号01---nvapi-aaa\n主账号02---nvapi-bbb\nnvapi-ccc\n\n  nvapi-ddd  \n"
        channel = channel_service.ensure_default_channel()
        res = key_service.bulk_import_keys(text, channel)
        self.assertEqual(res["success"], 4)
        self.assertEqual(ChannelKey.objects.count(), 4)
        plain = _plain_keys(ChannelKey.objects.all())
        auto = plain["nvapi-ccc"]
        self.assertTrue(auto.name.startswith("NVIDIA Key"))
        self.assertEqual(plain["nvapi-aaa"].name, "主账号01")

    def test_dedup_and_invalid(self):
        ChannelKey.objects.create(channel=_ch(), name="x", api_key="nvapi-dup")
        res = key_service.bulk_import_keys("nvapi-dup\nnvapi-dup\n   \nbad key\nnvapi-new",
                                     channel_service.ensure_default_channel())
        self.assertEqual(res["duplicate"], 2)
        self.assertEqual(res["invalid"], 1)
        self.assertEqual(res["success"], 1)

    def test_allow_duplicate_keys_channel_imports_duplicates(self):
        channel = Channel.objects.create(
            name="Zen", slug="zen", base_url="https://z.test/v1",
            allow_duplicate_keys=True)
        res = key_service.bulk_import_keys("public\npublic", channel)
        self.assertEqual(res["success"], 2)
        self.assertEqual(
            sum(1 for k in channel.keys.all() if decrypt_secret(k.api_key or "") == "public"),
            2)

    def test_anonymous_nokey_import(self):
        """无鉴权渠道支持批量导入“无需 Key”的匿名线路槽位。"""
        channel = Channel.objects.create(
            name="LLM7", slug="llm7", base_url="https://api.llm7.io/v1",
            auth_scheme="none")
        res = key_service.bulk_import_keys(
            "直连A---\n直连B---@nokey\n@nokey\n", channel)
        self.assertEqual(res["success"], 3)
        self.assertEqual(res["invalid"], 0)
        self.assertEqual(res["duplicate"], 0)
        anon = channel.keys.filter(api_key="").order_by("id")
        self.assertEqual(anon.count(), 3)
        # 命名正确：`名称---` 保留名称，裸 `@nokey` 自动命名
        self.assertEqual(anon[0].name, "直连A")
        self.assertEqual(anon[1].name, "直连B")
        self.assertTrue(anon[2].name.startswith("LLM7 Key"))

    def test_no_auth_bare_lines_become_anonymous_lines(self):
        """无鉴权渠道：裸行直接作为匿名线路名称，与显式 `名称---` 可共存。"""
        channel = Channel.objects.create(
            name="Mixed", slug="mixed", base_url="https://m.test/v1",
            auth_scheme="none")
        res = key_service.bulk_import_keys("线路A\n线路B\n直连---", channel)
        self.assertEqual(res["success"], 3)
        self.assertEqual(res["invalid"], 0)
        anon = channel.keys.filter(api_key="").order_by("id")
        self.assertEqual(anon.count(), 3)
        self.assertEqual(anon[0].name, "线路A")
        self.assertEqual(anon[1].name, "线路B")
        self.assertEqual(anon[2].name, "直连")

    def test_mask(self):
        masked = key_service.mask_key("nvapi-0123456789abcdef")
        self.assertNotIn("23456789", masked)


class RateLimitTests(TestCase):
    def setUp(self):
        self.key = ChannelKey.objects.create(channel=_ch(), name="k1", api_key="nvapi-x", rpm_limit=5)

    def test_rpm_enforced(self):
        self.assertTrue(all(key_service.claim_rpm_slot(self.key.id) for _ in range(5)))
        self.assertFalse(key_service.claim_rpm_slot(self.key.id))
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, ChannelKeyStatus.RATE_LIMITED)

        # window rolls over
        ChannelKey.objects.filter(pk=self.key.id).update(
            minute_window_start=timezone.now() - timedelta(seconds=61))
        self.assertTrue(key_service.claim_rpm_slot(self.key.id))

    def test_cooldown_blocks(self):
        ChannelKey.objects.filter(pk=self.key.id).update(
            cooldown_until=timezone.now() + timedelta(seconds=30))
        self.assertFalse(key_service.claim_rpm_slot(self.key.id))

    def test_rpm_zero_means_unlimited(self):
        """rpm_limit=0 视为不限流：直接成功且不计数。"""
        key = ChannelKey.objects.create(channel=_ch(), name="k0", api_key="nvapi-0",
                                        rpm_limit=0)
        for _ in range(20):
            self.assertTrue(key_service.claim_rpm_slot(key.id))
        key.refresh_from_db()
        self.assertEqual(key.minute_request_count, 0)

    def test_concurrent_claims_do_not_exceed_rpm(self):
        pass


class ConcurrentRateLimitTests(TransactionTestCase):
    def test_concurrent_claims_do_not_exceed_rpm(self):
        key = ChannelKey.objects.create(channel=_ch(), name="k1", api_key="nvapi-x", rpm_limit=5)
        results = []

        def worker():
            results.append(key_service.claim_rpm_slot(key.id))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 5)
        key.refresh_from_db()
        self.assertEqual(key.minute_request_count, 5)


class FailureStatusTests(TestCase):
    def setUp(self):
        self.key = ChannelKey.objects.create(channel=_ch(), name="k", api_key="nvapi-y", rpm_limit=40)

    def test_401_marks_invalid(self):
        key_service.report_failure(self.key.id, "http_error", 401)
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, ChannelKeyStatus.INVALID)

    def test_401_breaker_off_keeps_cooldown_but_not_invalid(self):
        ch = self.key.channel
        ch.disable_key_invalid = True
        ch.save()
        key_service.report_failure(self.key.id, "http_error", 401)
        self.key.refresh_from_db()
        # 不永久标记为 invalid
        self.assertNotEqual(self.key.status, ChannelKeyStatus.INVALID)
        # 冷却仍保留（会自动恢复）
        self.assertIsNotNone(self.key.cooldown_until)

    def test_anonymous_key_401_always_invalid_even_with_breaker_off(self):
        # 匿名线路（空 api_key）的 401 是"上游必须鉴权"的确定性信号，
        # 即使关闭了无效标记也必须标 invalid，否则会无限循环 401。
        anon = ChannelKey.objects.create(
            channel=self.key.channel, name="anon", api_key="", rpm_limit=40)
        ch = self.key.channel
        ch.disable_key_invalid = True
        ch.save()
        key_service.report_failure(anon.id, "http_error", 401)
        anon.refresh_from_db()
        self.assertEqual(anon.status, ChannelKeyStatus.INVALID)

    def test_anonymous_key_403_always_invalid_even_with_breaker_off(self):
        anon = ChannelKey.objects.create(
            channel=self.key.channel, name="anon2", api_key="", rpm_limit=40)
        ch = self.key.channel
        ch.disable_key_invalid = True
        ch.save()
        key_service.report_failure(anon.id, "http_error", 403)
        anon.refresh_from_db()
        self.assertEqual(anon.status, ChannelKeyStatus.INVALID)

    def test_429_breaker_off_still_rate_limits(self):
        ch = self.key.channel
        ch.disable_key_invalid = True
        ch.save()
        key_service.report_failure(self.key.id, "http_error", 429)
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, ChannelKeyStatus.RATE_LIMITED)
        self.assertIsNotNone(self.key.cooldown_until)

    def test_429_marks_rate_limited_with_cooldown(self):
        key_service.report_failure(self.key.id, "http_error", 429)
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, ChannelKeyStatus.RATE_LIMITED)
        self.assertIsNotNone(self.key.cooldown_until)

    def test_success_resets(self):
        key_service.report_failure(self.key.id, "http_error", 429)
        key_service.report_success(self.key.id)
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, ChannelKeyStatus.AVAILABLE)
        self.assertIsNone(self.key.cooldown_until)
