"""运维特性测试：日志分页、日志清理、模型同步裁剪、实时并发计数。"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api import admin_views, openai_views
from apps.core.models import AIModel, Channel, RequestLog
from services import cleanup


def _make_channel(**kw) -> Channel:
    defaults = dict(name="Ops", slug="ops", base_url="https://o.test/v1")
    defaults.update(kw)
    return Channel.objects.create(**defaults)


class LogPaginationTests(TestCase):
    def setUp(self):
        self.ch = _make_channel()
        for i in range(12):
            RequestLog.objects.create(channel=self.ch, request_id=f"r{i}",
                                      model="m", status="success" if i % 2 == 0 else "failed")

    def test_default_limit_and_total(self):
        req = RequestFactory().get("/api/admin/logs",
                                   HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                   HTTP_X_CHANNEL="ops")
        resp = admin_views.LogListView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["total"], 12)
        self.assertEqual(len(resp.data["results"]), 12)  # 12 < 默认 limit 100
        self.assertFalse(resp.data["has_more"])

    def test_limit_offset_has_more(self):
        req = RequestFactory().get("/api/admin/logs?limit=5&offset=5",
                                   HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                   HTTP_X_CHANNEL="ops")
        resp = admin_views.LogListView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 5)
        self.assertEqual(resp.data["offset"], 5)
        self.assertTrue(resp.data["has_more"])

    def test_filter_applies_then_paginates(self):
        req = RequestFactory().get("/api/admin/logs?status=success&limit=5",
                                   HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                   HTTP_X_CHANNEL="ops")
        resp = admin_views.LogListView.as_view()(req)
        self.assertEqual(resp.data["total"], 6)
        self.assertEqual(len(resp.data["results"]), 5)
        self.assertTrue(resp.data["has_more"])

    def test_bad_limit_returns_400(self):
        req = RequestFactory().get("/api/admin/logs?limit=abc",
                                   HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                   HTTP_X_CHANNEL="ops")
        resp = admin_views.LogListView.as_view()(req)
        self.assertEqual(resp.status_code, 400)


class LogCleanupTests(TestCase):
    @staticmethod
    def _create_log(ch, request_id, age_days):
        rec = RequestLog.objects.create(channel=ch, request_id=request_id, model="m")
        if age_days is not None:
            RequestLog.objects.filter(pk=rec.pk).update(
                created_at=timezone.now() - timedelta(days=age_days))
        return rec

    def test_clean_old_logs(self):
        ch = _make_channel()
        self._create_log(ch, "old", 60)
        self._create_log(ch, "new", None)
        result = cleanup.clean_old_logs(days=30, channel=ch)
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(RequestLog.objects.filter(request_id="old").exists())
        self.assertTrue(RequestLog.objects.filter(request_id="new").exists())

    def test_retention_zero_disables(self):
        ch = _make_channel()
        self._create_log(ch, "old", 600)
        result = cleanup.clean_old_logs(days=0, channel=ch)
        self.assertEqual(result["deleted"], 0)
        self.assertTrue(RequestLog.objects.filter(request_id="old").exists())

    def test_dry_run_no_delete(self):
        ch = _make_channel()
        self._create_log(ch, "old", 60)
        result = cleanup.clean_old_logs(days=30, channel=ch, dry_run=True)
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(RequestLog.objects.filter(request_id="old").exists())

    def test_clean_api_endpoint(self):
        ch = _make_channel()
        self._create_log(ch, "old", 60)
        req = RequestFactory().post("/api/admin/logs/clean",
                                    data=json.dumps({"days": 30}),
                                    content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                    HTTP_X_CHANNEL="ops")
        resp = admin_views.LogCleanView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["deleted"], 1)


class ModelPruneTests(TestCase):
    def test_prune_only_synced_disabled_stale(self):
        ch = _make_channel()
        AIModel.objects.create(channel=ch, model_name="gone", provider="ops", enabled=False)
        AIModel.objects.create(channel=ch, model_name="manual", provider="manual", enabled=False)
        AIModel.objects.create(channel=ch, model_name="kept", provider="ops", enabled=True)
        upstream = {"data": [{"id": "kept"}]}

        from services import upstream_service
        with patch.object(upstream_service, "list_models_raw", return_value=(200, upstream)):
            result = upstream_service.sync_models(ch, api_key="nvapi-x", prune=True)
        self.assertEqual(result["pruned"], 1)
        self.assertFalse(AIModel.objects.filter(model_name="gone").exists())
        self.assertTrue(AIModel.objects.filter(model_name="manual").exists())
        self.assertTrue(AIModel.objects.filter(model_name="kept").exists())


class ActiveRequestsTests(TestCase):
    def test_counter_returns_non_negative(self):
        # 计数器默认 0；直接读应返回整数
        self.assertIsInstance(openai_views.active_requests(), int)
        self.assertGreaterEqual(openai_views.active_requests(), 0)

    def test_dashboard_returns_active_requests(self):
        ch = _make_channel()
        req = RequestFactory().get("/api/admin/dashboard",
                                   HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                   HTTP_X_CHANNEL="ops")
        resp = admin_views.DashboardView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("active_requests", resp.data)
        self.assertIsInstance(resp.data["active_requests"], int)


class GenerationSpeedTests(TestCase):
    def setUp(self):
        self.ch = _make_channel()
        self._seq = 0

    def _serialize(self, **kw):
        from api.serializers import RequestLogSerializer
        self._seq += 1
        log = RequestLog.objects.create(
            channel=self.ch, request_id=f"spd{self._seq}", model="m",
            status="success", **kw)
        return RequestLogSerializer(log).data["generation_speed"]

    def test_stream_speed_excludes_ttft(self):
        # 流式：耗时 2000ms、首字 500ms、输出 150 token
        # 生成耗时 = 1500ms -> 150 / 1.5 = 100.0 tok/s
        speed = self._serialize(is_stream=True, duration_ms=2000,
                                first_token_ms=500, completion_tokens=150)
        self.assertEqual(speed, 100.0)

    def test_non_stream_speed_uses_total_duration(self):
        # 非流式：耗时 1000ms、输出 50 token -> 50 tok/s
        speed = self._serialize(is_stream=False, duration_ms=1000,
                                completion_tokens=50)
        self.assertEqual(speed, 50.0)

    def test_no_output_returns_none(self):
        self.assertIsNone(self._serialize(is_stream=True, duration_ms=1000,
                                          completion_tokens=0))

    def test_ttft_geq_duration_returns_none(self):
        # 首字不小于总耗时视为数据异常（与 new-api 口径一致），不计算
        self.assertIsNone(self._serialize(is_stream=True, duration_ms=500,
                                          first_token_ms=500, completion_tokens=10))
        self.assertIsNone(self._serialize(is_stream=True, duration_ms=500,
                                          first_token_ms=600, completion_tokens=10))

    def test_stream_gen_time_too_short_returns_none(self):
        # 生成阶段过短（首字≈总耗时、输出一次性涌入）会算出虚高 tok/s，
        # 低于最小统计窗口时不展示。复现真实案例：首字 12.71s、总耗时 12.87s。
        self.assertIsNone(self._serialize(is_stream=True, duration_ms=12866,
                                          first_token_ms=12705, completion_tokens=151))
        self.assertIsNone(self._serialize(is_stream=True, duration_ms=1000,
                                          first_token_ms=700, completion_tokens=150))


class ModelAliasAdminTests(TestCase):
    def test_admin_patch_aliases_normalized(self):
        """admin 接口保存附加别名：逗号拆开、去空格、去重。"""
        from api import admin_views
        ch = _make_channel()
        m = AIModel.objects.create(channel=ch, model_name="raw/name", enabled=True)
        req = RequestFactory().patch(
            f"/api/admin/models/{m.id}",
            data=json.dumps({"aliases": ["a1", "a2, a3", " a1 "]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
            HTTP_X_CHANNEL="ops")
        resp = admin_views.ModelDetailView.as_view()(req, pk=m.id)
        self.assertEqual(resp.status_code, 200)
        m.refresh_from_db()
        self.assertEqual(m.aliases, ["a1", "a2", "a3"])


class ResponsesConversionTests(TestCase):
    def test_assistant_tool_calls_preserved_in_input(self):
        """assistant 消息的 tool_calls 必须转成独立 function_call 条目，
        否则后续 role=tool 的 function_call_output 无对应定义，上游会 400。"""
        from services import responses_api

        body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "天气如何"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            ],
        }
        out = responses_api.chat_to_responses_body(body)
        types = [i["type"] for i in out["input"]]
        # 消息 + function_call + function_call_output
        self.assertEqual(types, ["message", "function_call", "function_call_output"])
        fc = next(i for i in out["input"] if i["type"] == "function_call")
        self.assertEqual(fc["call_id"], "call_1")
        self.assertEqual(fc["name"], "get_weather")
