import asyncio
import itertools
from unittest.mock import patch

from django.test import TestCase

from apps.core.models import Channel, ChannelKey
from services.load_balancer import Route
from services import race_engine
from services.race_engine import (
    AllRoutesFailed, RaceResult, is_valid_response, is_valid_stream_chunk,
)


_CHANNEL_SEQ = itertools.count()


def make_route(name, key_suffix):
    """每条线路都挂一个临时渠道：TestCase 会回滚，name/slug 用序号保证不撞。"""
    seq = next(_CHANNEL_SEQ)
    channel = Channel.objects.create(
        name=f"Test-{seq}-{key_suffix}", slug=f"test-{seq}-{key_suffix}",
        base_url="https://upstream.test/v1")
    key = ChannelKey(channel=channel, name=f"key-{key_suffix}",
                     api_key=f"nvapi-{key_suffix}")
    return Route(kind="direct", key=key)


def resp(obj):
    data = {
        "id": "chatcmpl-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return RaceResult(ok=True, route=obj, payload=data, http_status=200)


class ValidationTests(TestCase):
    def test_valid_response(self):
        self.assertTrue(is_valid_response(200, {"choices": [{"message": {"content": "hi"}}]}))
        self.assertFalse(is_valid_response(200, {"choices": []}))
        self.assertFalse(is_valid_response(200, {"error": {"message": "x"}}))
        self.assertFalse(is_valid_response(500, {"choices": [{"message": {"content": "hi"}}]}))

    def test_stream_chunk(self):
        self.assertIsNone(is_valid_stream_chunk("data: garbage"))
        self.assertIsNotNone(is_valid_stream_chunk(
            'data: {"choices":[{"delta":{"content":"h"}}]}'))
        self.assertIsNone(is_valid_stream_chunk('data: {"error":{"message":"x"}}'))

    def test_bare_done_is_not_a_valid_first_chunk(self):
        """上游首行裸发 `data: [DONE]` = 空响应，不能判为竞速胜者。

        过去这里返回 {"done": True} 被当成有效首块，导致客户端收到空白回答且
        因为"已成功"而不触发自动重试换线。
        """
        self.assertIsNone(is_valid_stream_chunk("data: [DONE]"))

    def test_contentless_chunks_are_valid_first_chunks(self):
        """空 delta / 纯角色标记也算"线路开始响应"（回归宽松判胜）：
        竞速胜负锁定在"谁先开始出流"，而非"谁先产出内容"——思考模型会静默
        几十秒才吐首个内容块，若等内容才判胜，竞速窗口会被模型思考时间拖长。
        正文延迟由 _drain 的心跳探测与停滞判死兜底。"""
        self.assertIsNotNone(is_valid_stream_chunk('data: {"choices":[{"delta":{}}]}'))
        self.assertIsNotNone(is_valid_stream_chunk(
            'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}'))

    def test_content_chunks_are_valid(self):
        """正文 / 思考 / 工具调用 / usage / 结束原因都算有效内容。"""
        for payload in (
            '{"choices":[{"delta":{"content":"hi"}}]}',
            '{"choices":[{"delta":{"reasoning_content":"think"}}]}',
            '{"choices":[{"delta":{"tool_calls":[{"index":0}]}}]}',
            '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
            '{"choices":[{"delta":{}}],"usage":{"prompt_tokens":1}}',
        ):
            self.assertIsNotNone(is_valid_stream_chunk("data: " + payload), payload)


class RaceTests(TestCase):
    def _race(self, behaviors):
        """behaviors: list of (delay, ok). Returns (winner_index, cancelled_flags)."""
        cancelled = [False] * len(behaviors)
        delay_slow = 60.0

        async def fake_do_request(route, body, base_url, started=None):
            idx = int(route.key.name.split("-")[-1])
            delay, ok = behaviors[idx]
            try:
                await asyncio.sleep(delay)
                if ok:
                    r = resp(route)
                    return r
                return RaceResult(ok=False, route=route, error_type="boom", http_status=500)
            except asyncio.CancelledError:
                cancelled[idx] = True
                raise

        with patch.object(race_engine, "_do_request", fake_do_request), \
             patch.object(race_engine, "_mark_success"), \
             patch.object(race_engine, "_mark_failure"):
            routes = [make_route(f"r{i}", i) for i in range(len(behaviors))]
            try:
                result = asyncio.run(race_engine._race(routes, {}))
                winner_idx = int(result.route.key.name.split("-")[-1])
                return winner_idx, cancelled, result
            except AllRoutesFailed:
                return -1, cancelled, None

    def test_fast_wins_slow_cancelled(self):
        # A slow (1.5s), B fast (0.05s), C fails
        winner, cancelled, result = self._race([(1.5, True), (0.05, True), (0.01, False)])
        self.assertEqual(winner, 1)
        self.assertTrue(cancelled[0])

    def test_failures_skipped_until_success(self):
        winner, cancelled, result = self._race([(0.01, False), (0.02, False), (0.05, True)])
        self.assertEqual(winner, 2)

    def test_all_failed_raises(self):
        winner, cancelled, result = self._race([(0.01, False), (0.01, False)])
        self.assertEqual(winner, -1)


class StreamRaceTests(TestCase):
    def test_first_valid_stream_wins_rest_cancelled(self):
        from services import race_engine

        cancelled = [False, False, False]

        async def fake_stream(route, body):
            idx = int(route.key.name.split("-")[-1])
            delay = [0.5, 0.05, 0.3][idx]
            ok = [True, True, False][idx]
            try:
                await asyncio.sleep(delay)
                if not ok:
                    return None, {"name": route.name, "error": "boom"}
                # cm, req_cm, resp, aiter, first_line, prelude
                return (None, None, None, None,
                        'data: {"choices":[{"delta":{"content":"h"}}]}', []), None
            except asyncio.CancelledError:
                cancelled[idx] = True
                raise

        with patch.object(race_engine, "_stream_first_valid", fake_stream):
            routes = [make_route(f"r{i}", i) for i in range(3)]
            route, *_ = asyncio.run(
                race_engine.race_stream_winner(routes, {}))
            self.assertEqual(route.key.name, "key-1")
        self.assertTrue(cancelled[0])
        self.assertTrue(cancelled[2])

    def test_stream_all_invalid_raises(self):
        from services import race_engine

        async def fake_stream(route, body):
            return None, {"name": route.name, "error": "boom"}

        with patch.object(race_engine, "_stream_first_valid", fake_stream):
            routes = [make_route("r0", 0)]
            with self.assertRaises(AllRoutesFailed):
                asyncio.run(race_engine.race_stream_winner(routes, {}))


class TotalWallClockTests(TestCase):
    """A2：非流式竞速必须有总墙钟兜底。

    `upstream_read_timeout` 默认 0（不限制）是为了不杀慢模型，但 read 超时
    管不住「一直在动却永远不结束」的上游。而 `_race` 由 `race_chat` 的
    `asyncio.run` 驱动，跑在 asgiref 的**单线程** thread-sensitive 执行器上，
    本项目每个入口都是同步视图 —— 一条挂死的非流式请求冻结的是整个网关。

    所以竞速循环必须有一个宽松到不会误杀真实生成、但确实存在的上限。
    """

    def test_hung_routes_are_bounded_not_waited_forever(self):
        import time as _time
        from unittest.mock import patch

        async def hang(route, body, t0):
            await asyncio.Event().wait()   # 永不完成，也不抛
            return resp(route)

        original_get = race_engine.sysconfig.get

        def fake_get(key, channel=None, default=None):
            if key == "upstream_total_timeout":
                return 0.2
            return original_get(key, channel, default)

        routes = [make_route(f"h{i}", f"h{i}") for i in range(2)]
        with patch.object(race_engine.sysconfig, "get", fake_get), \
                patch.object(race_engine, "_do_request", hang):
            t0 = _time.monotonic()
            with self.assertRaises(AllRoutesFailed) as ctx:
                asyncio.run(race_engine._race(routes, {}))
            elapsed = _time.monotonic() - t0

        self.assertLess(elapsed, 5.0,
                        f"总墙钟没生效，竞速等了 {elapsed:.1f}s 才结束")
        report = ctx.exception.report or []
        self.assertTrue(
            any(r.get("error") == "total_timeout" for r in report),
            f"挂死的线路必须被如实记为 total_timeout，实际 report={report}")

    def test_zero_total_timeout_stays_unbounded(self):
        """0 = 不限制必须真的是不限制（防止有人把 `if total_timeout > 0`
        「优化」成比较式，让 0 变成 0 秒超时）。"""
        from unittest.mock import patch

        done = asyncio.Event()

        async def slow(route, body, t0):
            await asyncio.sleep(0.25)
            return resp(route)

        original_get = race_engine.sysconfig.get

        def fake_get(key, channel=None, default=None):
            if key == "upstream_total_timeout":
                return 0
            return original_get(key, channel, default)

        routes = [make_route("z0", "z0")]
        with patch.object(race_engine.sysconfig, "get", fake_get), \
                patch.object(race_engine, "_do_request", slow):
            result = asyncio.run(race_engine._race(routes, {}))
        self.assertTrue(result.ok)
        del done

    def test_default_is_generous_not_zero(self):
        """默认值必须是「宽松到不会误杀、但确实存在」的正数，不能退回 0。"""
        from services import sysconfig

        self.assertGreater(float(sysconfig.RUNTIME_PARAMS["upstream_total_timeout"][1]), 0)
        self.assertGreater(float(sysconfig.RUNTIME_PARAMS["stream_max_duration"][1]), 0)
        # 而两条**静默**判死超时必须仍是 0（用户明确要求不限制慢模型）
        self.assertEqual(float(sysconfig.RUNTIME_PARAMS["stream_idle_timeout"][1]), 0)
        self.assertEqual(
            float(sysconfig.RUNTIME_PARAMS["stream_content_idle_timeout"][1]), 0)
