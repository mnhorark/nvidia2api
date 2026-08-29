import threading
from datetime import timedelta

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.core.models import Channel, ChannelKey, ChannelKeyStatus
from services import channel_service, key_service


def _ch():
    return channel_service.ensure_default_channel()


class KeyImportTests(TestCase):
    def test_named_and_bare_formats(self):
        text = "主账号01---nvapi-aaa\n主账号02---nvapi-bbb\nnvapi-ccc\n\n  nvapi-ddd  \n"
        channel = channel_service.ensure_default_channel()
        res = key_service.bulk_import_keys(text, channel)
        self.assertEqual(res["success"], 4)
        self.assertEqual(ChannelKey.objects.count(), 4)
        auto = ChannelKey.objects.get(api_key="nvapi-ccc")
        self.assertTrue(auto.name.startswith("NVIDIA Key"))
        self.assertEqual(ChannelKey.objects.get(api_key="nvapi-aaa").name, "主账号01")

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
        self.assertEqual(channel.keys.filter(api_key="public").count(), 2)

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
