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

    def test_no_available_route_does_not_self_trip(self):
        """no_available_route 是"熔断/Key 用尽"的结果而非上游故障：
        一旦计入熔断计数，冷却结束后瞬间再次熔断，形成自我强化的死循环。
        多次返回 503 no_available_route 不得累计熔断计数、不得触发熔断。"""
        for _ in range(12):
            channel_health.record(self.ch, False, http_status=503,
                                  error_type="no_available_route")
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

    def test_capacity_failures_with_429_evidence_do_not_trip(self):
        """429 风暴里夹带 502 的竞速失败属于"号池容量/代理质量"问题，
        不是渠道宕机：线路明细只要出现 rate_limited/429 证据，就不计入熔断。
        否则 5 次这类请求就能把全渠道熔断，期间所有可用 Key 一律 503。"""
        routes = [
            {"name": "p1+k1", "status": "failed", "error": "rate_limited",
             "http_status": 429},
            {"name": "p2+k2", "status": "failed", "error": "upstream_server_error",
             "http_status": 502},
        ]
        for _ in range(10):
            channel_health.record(self.ch, False, http_status=502,
                                  error_type="all_routes_failed", routes=routes)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 0)
        self.assertIsNone(self.ch.cooldown_until)
        self.assertFalse(channel_health.is_open(self.ch))

    def test_pure_channel_failure_still_trips(self):
        """没有 Key 级应答证据（无 429/401/403）、全是连接/5xx 失败，
        仍是确凿的渠道级故障，照常熔断。"""
        routes = [
            {"name": "p1+k1", "status": "failed", "error": "upstream_server_error",
             "http_status": 502},
            {"name": "p2+k2", "status": "failed", "error": "connect_error",
             "http_status": 0},
        ]
        for _ in range(5):
            channel_health.record(self.ch, False, http_status=502,
                                  error_type="all_routes_failed", routes=routes)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 5)
        self.assertIsNotNone(self.ch.cooldown_until)
        self.assertTrue(channel_health.is_open(self.ch))

    def test_stream_idle_timeout_does_not_trip(self):
        """流式判死超时（504）是"模型长思考静默过久"，不是"渠道宕机"：
        http_status=504 满足 >=500，但绝不能因此熔断整个渠道——
        否则几次 kimi/R1 长思考超时就把 312 Key 渠道整锅熔断。"""
        for _ in range(10):
            channel_health.record(self.ch, False, http_status=504,
                                  error_type="stream_idle_timeout")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 0)
        self.assertIsNone(self.ch.cooldown_until)
        self.assertFalse(channel_health.is_open(self.ch))


    def test_429_evidence_resets_consecutive_failures(self):
        """有 429 应答证据 = 渠道活着，等价成功，应清零连续失败计数。
        先累计 3 次确凿失败，再来一次 429 风暴请求，计数应归零而非继续累加。"""
        for _ in range(3):
            channel_health.record(self.ch, False, http_status=502,
                                  error_type="all_routes_failed", routes=[
                                      {"name": "x", "status": "failed",
                                       "error": "connect_error", "http_status": 0}])
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 3)
        channel_health.record(self.ch, False, http_status=502,
                              error_type="all_routes_failed", routes=[
                                  {"name": "p1+k1", "status": "failed",
                                   "error": "rate_limited", "http_status": 429}])
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

    def test_metrics_requires_auth(self):
        """/metrics 暴露池规模与用量情报，匿名必须拒绝（M6）。"""
        self.assertEqual(
            health_views.metrics(self.factory.get("/metrics")).status_code, 401)
        self.assertEqual(
            health_views.metrics(self.factory.get(
                "/metrics", HTTP_AUTHORIZATION="Token wrong-token")).status_code, 401)

    def test_metrics_text(self):
        resp = health_views.metrics(self.factory.get(
            "/metrics", HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}"))
        self.assertEqual(resp.status_code, 200)
        text = resp.content.decode()
        self.assertIn("nvidia2api_requests_total", text)
        self.assertIn("nvidia2api_upstream_status", text)
