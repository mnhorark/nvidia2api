"""渠道熔断（circuit breaker）与健康检查端点测试。"""
import json

from django.conf import settings
from django.test import RequestFactory, TestCase
from django.utils import timezone
from datetime import timedelta

from api import health_views
from apps.core.models import Channel, ChannelKey
from services import channel_health, channel_service, key_service


class CircuitBreakerTests(TestCase):
    def setUp(self):
        self.ch = Channel.objects.create(name="CB", slug="cb", base_url="https://c.test/v1")

    def test_systematic_failures_trip_cooldown(self):
        for _ in range(5):
            channel_health.record(self.ch, False, http_status=502, error_type="all_routes_failed")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 5)
        self.assertIsNotNone(self.ch.cooldown_until)
        self.assertTrue(channel_health.is_open(self.ch))

    def test_key_level_failures_do_not_trip(self):
        channel_health.record(self.ch, False, http_status=401, error_type="http_error")
        channel_health.record(self.ch, False, http_status=429, error_type="rate_limited")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 0)
        self.assertIsNone(self.ch.cooldown_until)
        self.assertFalse(channel_health.is_open(self.ch))

    def test_success_resets_failures(self):
        channel_health.record(self.ch, False, http_status=500, error_type="upstream_error")
        channel_health.record(self.ch, False, http_status=500, error_type="upstream_error")
        channel_health.record(self.ch, True)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 0)
        self.assertIsNone(self.ch.cooldown_until)

    def test_cooldown_expires_automatically(self):
        for _ in range(5):
            channel_health.record(self.ch, False, http_status=502, error_type="all_routes_failed")
        Channel.objects.filter(pk=self.ch.pk).update(
            cooldown_until=timezone.now() - timedelta(seconds=1))
        self.ch.refresh_from_db()
        self.assertFalse(channel_health.is_open(self.ch))

    def test_available_keys_skipped_in_cooldown(self):
        ChannelKey.objects.create(channel=self.ch, name="k", api_key="nvapi-x")
        for _ in range(5):
            channel_health.record(self.ch, False, http_status=502, error_type="all_routes_failed")
        self.assertEqual(key_service.available_keys(self.ch), [])
        # 熔断解除后恢复调度
        Channel.objects.filter(pk=self.ch.pk).update(cooldown_until=None,
                                                     consecutive_failures=0)
        self.ch.refresh_from_db()
        self.assertEqual(len(key_service.available_keys(self.ch)), 1)

    def test_default_channel_skips_cooldown(self):
        healthy = Channel.objects.create(name="Healthy", slug="healthy",
                                         base_url="https://h.test/v1", is_default=True)
        for _ in range(5):
            channel_health.record(self.ch, False, http_status=502, error_type="all_routes_failed")
        # is_default 渠道在熔断中时，默认渠道应回落到健康渠道
        picked = channel_service.default_channel()
        self.assertEqual(picked.pk, healthy.pk)


class HealthEndpointTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_liveness_ok(self):
        resp = health_views.liveness(self.factory.get("/healthz"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["status"], "ok")

    def test_admin_health_requires_auth(self):
        resp = health_views.admin_health(self.factory.get("/api/admin/health"))
        self.assertEqual(resp.status_code, 401)

    def test_admin_health_ok(self):
        resp = health_views.admin_health(self.factory.get(
            "/api/admin/health",
            HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}"))
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content)
        self.assertEqual(body["status"], "ok")
        self.assertIn("counts", body)
        self.assertIn("key_status", body)

    def test_metrics_text(self):
        resp = health_views.metrics(self.factory.get("/metrics"))
        self.assertEqual(resp.status_code, 200)
        text = resp.content.decode()
        self.assertIn("nvidia2api_requests_total", text)
        self.assertIn("nvidia2api_upstream_status", text)
