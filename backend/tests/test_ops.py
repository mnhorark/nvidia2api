"""运维特性测试：日志分页、日志清理、模型同步裁剪、实时并发计数。"""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api import admin_views, openai_views
from apps.core.models import AIModel, Channel, RequestLog
from services import cleanup


async def _never_ends():
    """永不出数据的异步生成器：用于测试无数据空闲超时。"""
    import asyncio
    await asyncio.Event().wait()
    yield  # pragma: no cover



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


class DrainFirstContentTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """_drain 心跳 + 停滞超时：持续有数据（含思考 reasoning token）不掐断；
    连续超过停滞阈值无任何数据才抛 TimeoutError；静默期内向客户端发心跳。"""

    @staticmethod
    def _winner(agen):
        class FakeWinner:
            def __init__(self, agen):
                self._agen = agen()
                self.gens = [self._agen]

            def lines(self):
                return self._agen
        return FakeWinner(agen)

    async def test_no_data_times_out(self):
        from api.openai_views import _drain
        winner = self._winner(lambda: _never_ends())
        with self.assertRaises(TimeoutError):
            async for _ in _drain(winner, probe_interval=0.03, max_idle_probes=2):
                pass

    async def test_reasoning_flow_does_not_timeout(self):
        """思考 token 持续流动即证明线路没死：即使总时长超过判死阈值也不掐断。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            for _ in range(5):
                yield ('data: {"choices":[{"index":0,"delta":{"reasoning_content":"t"},'
                       '"finish_reason":null}]}\n\n')
                await asyncio.sleep(0.03)

        winner = self._winner(stream)
        got = 0
        async for _ in _drain(winner, probe_interval=0.03, max_idle_probes=2):
            got += 1
        # 总耗时 0.15s > 判死阈值 0.06s：若按"连续无数据"早该被掐断，但思考 token
        # 一直在流动（任一数据即清零重计），必须完整走完
        self.assertGreaterEqual(got, 5)

    async def test_idle_count_resets_after_any_data(self):
        """连续探测判死的容错核心：中途任何数据到达即清零失败计数——
        生成慢但连接活（如两段思考之间）绝不被连续失败计数误杀。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"a"},'
                   '"finish_reason":null}]}\n\n')
            await asyncio.sleep(0.04)  # > probe(0.03)：计 1 次"心跳失败"
            yield ('data: {"choices":[{"index":0,"delta":{"content":"b"},'
                   '"finish_reason":null}]}\n\n')  # 数据到达 → 清零
            await asyncio.sleep(0.04)  # 再静默一轮；若不清零累计 0.08>0.06 会误判
            yield ('data: {"choices":[{"index":0,"delta":{"content":"c"},'
                   '"finish_reason":null}]}\n\n')

        winner = self._winner(stream)
        got = []
        async for chunk in _drain(winner, probe_interval=0.03, max_idle_probes=2):
            got.append(chunk)
        self.assertEqual(len(got), 3)

    async def test_heartbeat_emitted_during_silence(self):
        """上游静默但未达判死阈值时，向客户端周期性发 `: keep-alive` 心跳（流式保活）。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            # 只发一行数据，然后一直静默
            yield ('data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                   '"finish_reason":null}]}\n\n')
            await asyncio.Event().wait()

        winner = self._winner(stream)
        got_beat = 0
        try:
            async for chunk in _drain(winner, probe_interval=1.0, max_idle_probes=3,
                                      heartbeat=0.03, max_duration=0.15):
                # max_duration 0.15s 内应至少收到 2 次心跳，未触发判死（1.0s×3）
                if chunk.startswith(":"):
                    got_beat += 1
        except TimeoutError:
            pass  # max_duration 到期触发 TimeoutError 即测试的预期结束方式
        self.assertGreaterEqual(got_beat, 2)

    async def test_content_arrives_within_timeout(self):
        from api.openai_views import _drain
        async def stream():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                   '"finish_reason":null}]}\n\n')
        winner = self._winner(stream)
        out = []
        async for chunk in _drain(winner, probe_interval=0.05, max_idle_probes=2):
            out.append(chunk)
        self.assertIn("hi", out[0])

    async def test_zero_timeout_no_stall_limit(self):
        """0 = 不限制：慢速（但有数据）的流不应被探测判死打断。"""
        import asyncio
        from api.openai_views import _drain
        async def stream():
            await asyncio.sleep(0.05)
            yield ('data: {"choices":[{"index":0,"delta":{"content":"x"},'
                   '"finish_reason":null}]}\n\n')
        winner = self._winner(stream)
        out = []
        async for chunk in _drain(winner, probe_interval=0, max_idle_probes=0):
            out.append(chunk)
        self.assertIn("x", out[0])


class UpstreamBodyAndTokenTests(TestCase):
    """上游请求体与 token 统计：别名透传真实模型名、流式请求 usage、本地估算兜底。"""

    def test_stream_options_and_real_model_name(self):
        from api.openai_views import _build_upstream_body
        # 客户端用别名调用，流式请求：上游必须带真实模型名 + include_usage
        out = _build_upstream_body(
            {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}],
             "stream": True},
            "moonshotai/kimi-k3")
        self.assertEqual(out["model"], "moonshotai/kimi-k3")
        self.assertEqual(out["stream_options"], {"include_usage": True})

    def test_no_stream_options_when_not_streaming(self):
        from api.openai_views import _build_upstream_body
        out = _build_upstream_body(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "m")
        self.assertNotIn("stream_options", out)

    def test_token_estimation(self):
        from services import tokenizer
        # 空文本 -> 0；普通文本 > 0；messages 结构开销计入
        self.assertEqual(tokenizer.estimate_tokens(""), 0)
        self.assertGreater(tokenizer.estimate_tokens("hello world"), 0)
        msgs = [{"role": "user", "content": "hello world"}]
        self.assertGreater(tokenizer.estimate_messages_tokens(msgs),
                           tokenizer.estimate_tokens("hello world"))
