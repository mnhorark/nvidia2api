"""运维特性测试：日志分页、日志清理、模型同步裁剪、实时并发计数。"""
import json
import unittest
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
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

    def test_list_omits_heavy_detail_fields(self):
        # 列表必须轻量：不带 routes/thinking 高成本字段（日志页轮询性能的回归守卫）
        RequestLog.objects.create(
            channel=self.ch, request_id="heavy", model="m", status="success",
            routes=[{"name": "a", "kind": "direct", "key_name": "k",
                     "proxy_name": "", "status": "winner", "latency_ms": 1,
                     "error": "", "http_status": 200}],
            client_thinking={"reasoning_effort": "high"},
            upstream_thinking={"chat_template_kwargs": {"thinking": True}},
        )
        req = RequestFactory().get("/api/admin/logs?limit=100",
                                   HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                   HTTP_X_CHANNEL="ops")
        resp = admin_views.LogListView.as_view()(req)
        item = resp.data["results"][0]
        self.assertNotIn("routes", item)
        self.assertNotIn("client_thinking", item)
        self.assertNotIn("upstream_thinking", item)

    def test_detail_returns_heavy_fields(self):
        log = RequestLog.objects.create(
            channel=self.ch, request_id="detail-1", model="m", status="success",
            routes=[{"name": "a", "kind": "direct", "key_name": "k",
                     "proxy_name": "", "status": "winner", "latency_ms": 1,
                     "error": "", "http_status": 200}],
            client_thinking={"reasoning_effort": "high"},
        )
        req = RequestFactory().get(f"/api/admin/logs/{log.id}",
                                   HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}",
                                   HTTP_X_CHANNEL="ops")
        resp = admin_views.LogDetailView.as_view()(req, pk=log.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["routes"], log.routes)
        self.assertEqual(resp.data["client_thinking"], {"reasoning_effort": "high"})


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


class UpstreamGateTests(TestCase):
    """全局上游 socket 阀门：跨请求统计 & 余量校验（默认 0=不限制）。"""

    def setUp(self):
        # 模块级全局计数器不在 Django 事务内，测试间手动复位，避免串扰
        openai_views._upstream_active = 0

    def tearDown(self):
        openai_views._upstream_active = 0

    def test_reserve_caps_at_limit(self):
        with patch.object(openai_views, "_upstream_limit", return_value=5):
            self.assertEqual(openai_views._reserve_upstream(10), 5)
            self.assertEqual(openai_views._reserve_upstream(10), 1)

    def test_reserve_never_starves_to_zero(self):
        """有限额度耗尽时至少保底 1 条，避免请求被裁成 0 线路重试风暴。"""
        with patch.object(openai_views, "_upstream_limit", return_value=2):
            self.assertEqual(openai_views._reserve_upstream(10), 2)
            # 额度已耗尽，但仍保底 1
            self.assertEqual(openai_views._reserve_upstream(10), 1)

    def test_release_clamps_to_zero_and_reopens_slots(self):
        with patch.object(openai_views, "_upstream_limit", return_value=5):
            self.assertEqual(openai_views._reserve_upstream(10), 5)
            openai_views._release_upstream(3)
            self.assertEqual(openai_views._reserve_upstream(10), 3)
            openai_views._release_upstream(999)
            self.assertEqual(openai_views._reserve_upstream(10), 5)

    def test_zero_limit_means_unlimited_default(self):
        """max_concurrent_upstream=0（默认）视为不限制：全部放行，不裁剪、不饿死。"""
        with patch.object(openai_views, "_upstream_limit", return_value=10**9):
            self.assertEqual(openai_views._reserve_upstream(50), 50)
            self.assertEqual(openai_views._reserve_upstream(50), 50)
            self.assertEqual(openai_views._reserve_upstream(50), 50)

    def test_configured_zero_is_unlimited(self):
        # _upstream_limit 自身语义：0 -> 极大值（不限制）
        from services import sysconfig
        with patch("services.sysconfig.get", return_value=0):
            self.assertEqual(openai_views._upstream_limit(), 10**9)
        with patch("services.sysconfig.get", return_value=None):
            self.assertGreaterEqual(openai_views._upstream_limit(), 10**9)


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

    def test_stream_speed_uses_total_duration(self):
        # 流式：耗时 2000ms、输出 150 token -> 150 / 2.0 = 75.0 tok/s
        # （对齐 new-api：速度不扣 TTFT，denominator 用总耗时）
        speed = self._serialize(is_stream=True, duration_ms=2000,
                                first_token_ms=500, completion_tokens=150)
        self.assertEqual(speed, 75.0)

    def test_non_stream_speed_uses_total_duration(self):
        # 非流式：耗时 1000ms、输出 50 token -> 50 tok/s
        speed = self._serialize(is_stream=False, duration_ms=1000,
                                completion_tokens=50)
        self.assertEqual(speed, 50.0)

    def test_no_output_returns_none(self):
        self.assertIsNone(self._serialize(is_stream=True, duration_ms=1000,
                                          completion_tokens=0))
        self.assertIsNone(self._serialize(is_stream=True, duration_ms=0,
                                          completion_tokens=10))

    def test_burst_flush_no_longer_inflated(self):
        # 上游批量冲刷：首字≈总耗时（12.71s→12.87s）、151 token 一次性涌入。
        # 旧实现扣 TTFT 会把 denominator 压到 161ms 算出 ~938 tok/s 虚高值；
        # 对齐 new-api 用总耗时后得到诚实的有效吞吐 ≈ 11.74 tok/s。
        speed = self._serialize(is_stream=True, duration_ms=12866,
                                first_token_ms=12705, completion_tokens=151)
        self.assertAlmostEqual(speed, 11.74, places=2)

    def test_slow_model_keeps_precision(self):
        # 慢模型 <1 tok/s：保留 2 位小数，避免被 round(…,1) 压成 0.0 显示 "—"
        speed = self._serialize(is_stream=True, duration_ms=25000,
                                completion_tokens=1)
        self.assertEqual(speed, 0.04)


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
            async for _ in _drain(winner, idle_timeout=0.06):
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
        async for _ in _drain(winner, idle_timeout=0.06):
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
        async for chunk in _drain(winner, idle_timeout=0.06):
            got.append(chunk)
        self.assertEqual(len(got), 3)

    async def test_content_silence_survives_probe_deadline(self):
        """已产出真实内容（正文）后的长静默不再按 probe×probes 判死。

        思考模型在吐出若干 token 后可能合法停顿数分钟不吐字节；旧实现 30s×4≈120s
        就把胜出线路掐断、客户端收到"响应迟迟不回然后直接 [DONE] 中断"。
        修复后：已产出真实内容则只由 max_duration 兜底，能撑过 probe 判死窗口。
        """
        import asyncio
        import time as _t
        from api.openai_views import _drain

        async def stream():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                   '"finish_reason":null}]}\n\n')
            await asyncio.Event().wait()  # 静默，远超 probe×probes

        winner = self._winner(stream)
        t0 = _t.monotonic()
        got = 0
        try:
            async for _ in _drain(winner, idle_timeout=0.04,
                                  max_duration=0.12):
                got += 1
        except TimeoutError:
            pass
        elapsed = _t.monotonic() - t0
        self.assertEqual(got, 1)
        # 若仍按 probe(0.02×2=0.04s) 判死，elapsed≈0.04；能撑到 max_duration≈0.12 证明已豁免
        self.assertGreaterEqual(elapsed, 0.10)

    async def test_no_signal_still_deadlines(self):
        """从未产出真实信号（只有空 role/心跳行）的线路仍按短窗口判死——僵尸流换线兜底保留。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            # 结构合法但无正文/思考/工具的空 choices 行，随后静默
            yield 'data: {"choices":[{"index":0,"delta":{}}]}\n\n'
            await asyncio.Event().wait()

        winner = self._winner(stream)
        with self.assertRaises(TimeoutError):
            async for _ in _drain(winner, idle_timeout=0.04):
                pass

    async def test_reasoning_silence_still_killed_by_short_window(self):
        """思考请求在"从未产出真实内容"时同样按短窗口判死（回归守卫：不按是否带
        思考参数豁免）。思考模型也可能真卡死，统一由 probe×probes 兜底；需要更宽容忍
        就调大 stream_idle_timeout，而非跳过判死。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            # 只发一个空 role 块（结构合法、胜出依据），随后静默远超 probe 窗口
            yield 'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
            await asyncio.Event().wait()

        winner = self._winner(stream)
        # 即使假设 reasoning 场景（此处仅验证 probe 窗口对所有请求一视同仁），
        # 未产出真实内容 + 静默超窗口 → 仍判死
        with self.assertRaises(TimeoutError):
            async for _ in _drain(winner, idle_timeout=0.04):
                pass

    async def test_content_idle_timeout_still_kills_when_configured(self):
        """配置 stream_content_idle_timeout>0 时，已产出内容后的超静默仍判死（可选兜底）。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                   '"finish_reason":null}]}\n\n')
            await asyncio.Event().wait()

        winner = self._winner(stream)
        with self.assertRaises(TimeoutError):
            async for _ in _drain(winner, idle_timeout=0.5,
                                  content_idle_timeout=0.08):
                pass

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
            async for chunk in _drain(winner, idle_timeout=3,
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
        async for chunk in _drain(winner, idle_timeout=0.1):
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
        async for chunk in _drain(winner, idle_timeout=0):
            out.append(chunk)
        self.assertIn("x", out[0])

    async def test_data_arriving_after_probe_window_is_not_lost(self):
        """回归：数据在"探测等待窗口之后"才完成时不得丢失、不得误判死。

        旧实现 `if read_task is None or read_task.done(): read_task = ensure_future(...)`
        会在数据完成恰落在"wait 超时返回之后"时直接覆盖已完成的 read_task——
        那行数据丢失、last_data 不更新，判死计数从更早时刻累计，
        导致"刚吐过思考链却被误判线路死亡"。此测试以每段静默(0.08s)略小于
        判死阈值(0.09s)的节奏流动，任何一行丢失/时间戳停滞都会触发误判死；
        3 行必须全部按序交付。
        """
        import asyncio
        from api.openai_views import _drain

        async def stream():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"c0"},'
                   '"finish_reason":null}]}\n\n')  # 立即首行，建立 last_data
            for i in (1, 2):
                await asyncio.sleep(0.06)  # > wait 0.03；仍 < 阈值 0.12（余量足，防计时抖动）
                yield (f'data: {{"choices":[{{"index":0,"delta":{{"content":"c{i}"}},'
                       '"finish_reason":null}]}\n\n')

        winner = self._winner(stream)
        got = []
        async for chunk in _drain(winner, idle_timeout=0.12):
            got.append(chunk)
        self.assertEqual(len(got), 3, f"数据行被丢弃或误判死: {got}")

    async def test_flowing_data_keeps_full_length_flow_alive(self):
        """持续流动的数据（思考链）在任何时刻都更新 last_data：
        即使总时长远超判死阈值，只要数据在流（哪怕每行都落在探测窗口之后），
        线路就保持存活，直到数据真正停止。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            for i in range(8):
                await asyncio.sleep(0.05)
                yield (f'data: {{"choices":[{{"index":0,"delta":{{"reasoning_content":'
                       f'"r{i}"}},"finish_reason":null}}]}}\n\n')

        winner = self._winner(stream)
        got = []
        async for chunk in _drain(winner, idle_timeout=0.12):
            got.append(chunk)
        self.assertEqual(len(got), 8, f"思考链被误掐断: 只交付了 {len(got)}/8")

    async def test_idle_timeout_kills_stalled_winner(self):
        """宽松判胜锁定"先响应后卡死"的慢线：只发 role/空块、限时内无任何真实
        内容（连思考都不吐）→ 抛 TimeoutError，由上层换线重试，而非让客户端干等。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            # 模拟慢线：立即回一个纯 role 空块（宽松判胜因此"赢"了），之后永远卡死
            yield ('data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n')
            await asyncio.Event().wait()

        winner = self._winner(stream)
        with self.assertRaises(TimeoutError):
            async for _ in _drain(winner, idle_timeout=0.05):
                pass

    async def test_reasoning_signal_resets_idle_timeout(self):
        """思考增量算字节/信号：线路持续吐思考链时，last_data 持续刷新，
        idle_timeout 不触发（防止把真在思考的长模型当慢线踢掉）。"""
        import asyncio
        from api.openai_views import _drain

        async def stream():
            for _ in range(4):
                yield ('data: {"choices":[{"index":0,"delta":{"reasoning_content":"t"},'
                       '"finish_reason":null}]}\n\n')
                await asyncio.sleep(0.02)

        winner = self._winner(stream)
        got = 0
        async for _ in _drain(winner, idle_timeout=0.05):
            got += 1
        # 每段静默 0.02s < idle_timeout 0.05，数据持续刷新 → 不触发超时
        self.assertGreaterEqual(got, 4)


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


class EncryptSecretsBackfillTests(TestCase):
    """存量明文敏感字段的补加密命令（manage.py encrypt_secrets）。

    加密写在 save() 里，意味着**功能上线前的历史行永远是明文**，而读路径有
    明文回落所以看不出问题——"敏感字段加密保存"因此是部分失效且无人察觉。
    本组测试守的是补数路径本身。
    """

    def setUp(self):
        from apps.core.models import ChannelKey, Proxy
        self.Proxy = Proxy
        self.ChannelKey = ChannelKey
        self.a = Channel.objects.create(name="EA", slug="ea",
                                        base_url="https://ea.test/v1")
        self.b = Channel.objects.create(name="EB", slug="eb",
                                        base_url="https://eb.test/v1")
        # 明文行（模拟加密功能上线前的历史数据：直接绕过 save() 写库）
        self.plain_key = ChannelKey.objects.create(
            channel=self.a, name="legacy-key", api_key="nvapi-PLAINTEXT-1")
        self.plain_proxy = Proxy.objects.create(
            channel=self.a, name="legacy-proxy", protocol="socks5",
            host="1.1.1.1", port=1080, username="u", password="plain-pass")
        ChannelKey.objects.filter(pk=self.plain_key.pk).update(
            api_key="nvapi-PLAINTEXT-1")
        Proxy.objects.filter(pk=self.plain_proxy.pk).update(
            password="plain-pass")
        # 已是密文的行
        self.enc_key = ChannelKey.objects.create(
            channel=self.b, name="new-key", api_key="nvapi-ENCRYPTED-1")

    def _raw(self, obj, field):
        return type(obj).objects.filter(pk=obj.pk).values_list(
            field, flat=True).first()

    def _run(self, *args):
        out = StringIO()
        call_command("encrypt_secrets", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_does_not_modify(self):
        text = self._run()
        self.assertIn("ChannelKey 1", text)
        self.assertIn("Proxy.password 1", text)
        self.assertFalse(self._raw(self.plain_key, "api_key").startswith("enc:v1:"))
        self.assertFalse(self._raw(self.plain_proxy, "password").startswith("enc:v1:"))

    def test_apply_encrypts_and_roundtrips(self):
        self._run("--apply")
        from services import crypto
        key_raw = self._raw(self.plain_key, "api_key")
        pw_raw = self._raw(self.plain_proxy, "password")
        self.assertTrue(key_raw.startswith("enc:v1:"), key_raw[:12])
        self.assertTrue(pw_raw.startswith("enc:v1:"), pw_raw[:12])
        self.assertNotIn("nvapi-PLAINTEXT-1", key_raw)
        self.assertEqual(crypto.decrypt_secret(key_raw), "nvapi-PLAINTEXT-1")
        self.assertEqual(crypto.decrypt_secret(pw_raw), "plain-pass")

    def test_already_encrypted_rows_untouched(self):
        before = self._raw(self.enc_key, "api_key")
        self._run("--apply")
        self.assertEqual(self._raw(self.enc_key, "api_key"), before)

    def test_idempotent_second_run_has_nothing_to_do(self):
        self._run("--apply")
        text = self._run()
        self.assertIn("ChannelKey 0", text)
        self.assertIn("Proxy.password 0", text)

    def test_channel_scoping(self):
        self._run("--apply", "--channel", "eb")
        # 只处理 eb：ea 的明文行必须仍是明文
        self.assertFalse(self._raw(self.plain_key, "api_key").startswith("enc:v1:"))

    def test_unknown_channel_errors(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._run("--channel", "nope")
