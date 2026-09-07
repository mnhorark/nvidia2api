"""§六 第 12 项：数据面视图改 async 的两条前置专属测试。

覆盖此前完全没有测试驱动过的两条路径：

1. **竞速窗口心跳注入**（`_stream_response` 里把 `race_stream` 包进
   `asyncio.wait({race_task}, timeout=race_heartbeat)` 循环，静默期注入
   `: keep-alive`）。`test_ops.py` 的用例只覆盖 `_drain` 层，也就是"胜出之后
   上游静默"；而首帧等待最长的那段（winner 诞生之前）完全没测过。
2. **客户端断开强制结算**（`_stream_response` 的 finally 里 `not settled["done"]`
   分支：置 failed + 记一次失败 + 结算 claim_quota 预占）。此前测试里
   aclose / GeneratorExit / athrow 零命中。

为什么这两条是"改 async"的前置条件：把数据面视图改成 async 会改变收尾路径的
线程语义（`run_db` 的 `to_thread` 分支从"测试里永不启用"变成真的启用），
而这两段代码正是收尾与资源回收的落点。没有它们，改 async 就是在最关键链路上
无网重构。
"""
from __future__ import annotations

import asyncio
import json
import time as _time
import unittest
import uuid
from contextlib import suppress

import pytest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TransactionTestCase

from apps.core.models import AIModel, Channel, ChannelKey, RequestLog
from services import api_key_service, key_service, sysconfig


async def _take(gen, n: int = 10**6, timeout: float = 20.0):
    """最多取 n 帧、最多等 timeout 秒。

    这条不是可有可无的：证伪跑时把心跳注入拿掉，`__anext__` 会永远等在
    `asyncio.wait({race_task}, ...)` 上（竞速 task 永不完成），守卫**挂死而不是变红**。
    一个不会失败只会卡住的守卫等于没有守卫。
    """
    out = []

    async def _collect():
        async for chunk in gen:
            out.append(chunk)
            if len(out) >= n:
                break
        return out

    try:
        return await asyncio.wait_for(_collect(), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时后必须关掉生成器，否则测试收尾会卡住而不是失败
        with suppress(Exception):
            await gen.aclose()
        raise


async def _next(gen, timeout: float = 10.0):
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


def _winner_for(ch, key_id, lines_fn):
    """造一个 StreamWinner，形状对齐 race_engine 的真实返回。"""
    from services.race_engine import StreamWinner

    route = MagicMock()
    route.kind = "direct"
    route.key.name = "k0"
    route.key.id = key_id
    route.key.channel = ch
    route.proxy = None
    w = StreamWinner(route=route, cm=MagicMock(), req_cm=MagicMock(),
                     aiter=None, first_line="data: x\n\n")
    w.report = [{"name": "direct:k0", "status": "winner"}]
    w.lines = lines_fn
    w.close = AsyncMock()
    return w, route


def _silent_streaming_sysconfig(heartbeat: float):
    """把流式相关的静默判死档位全关掉，只留要测的那个心跳间隔。

    档位留 0 是刻意的：默认配置下它们本就不限制，测试要的是"只有竞速窗口
    在产心跳"这个干净环境。
    """
    original_get = sysconfig.get
    off = ("stream_idle_timeout", "stream_content_idle_timeout",
           "stream_max_duration", "retry_backoff_seconds", "retry_count")

    def fake_get(key, channel=None):
        if key == "stream_heartbeat_interval":
            return heartbeat
        if key in off:
            return 0
        return original_get(key, channel)
    return fake_get


class RaceWindowHeartbeatTests(IsolatedAsyncioTestCase):
    """竞速窗口（winner 诞生之前）必须注入 SSE 注释行保活。"""

    async def asyncSetUp(self):
        # 唯一 slug：IsolatedAsyncioTestCase + django_db 下同类用例之间不回滚
        slug = f"hb-{uuid.uuid4().hex[:8]}"
        self.ch = Channel.objects.create(name=slug, slug=slug,
                                         base_url="https://up.test/v1")
        self.key = ChannelKey.objects.create(channel=self.ch, name="k0",
                                             api_key="nvapi-hb", rpm_limit=100)
        self.user, self.raw = api_key_service.create_key(f"{slug}-user")
        AIModel.objects.create(channel=self.ch, model_name="m", enabled=True)

    def _log(self, rid):
        return RequestLog.objects.create(
            channel=self.ch, request_id=rid, user_api_key=self.user,
            model="m", routes_count=1, is_stream=True)

    @pytest.mark.django_db
    async def test_keepalive_frames_precede_any_data_frame(self):
        async def lines():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                   '"finish_reason":"stop"}]}\n\n')
            yield "data: [DONE]\n\n"

        w, route = _winner_for(self.ch, self.key.id, lines)

        async def slow_race(rs, body):
            # 模拟首帧等待：远长于心跳间隔，期间客户端零字节
            await asyncio.sleep(0.25)
            return w

        log = self._log("hb-1")
        holder = {"log": log, "started": _time.monotonic()}
        from api import openai_views

        with patch("api.openai_views.race_stream", new=slow_race), \
                patch.object(sysconfig, "get", _silent_streaming_sysconfig(0.02)):
            gen = openai_views._stream_response(
                [route], {}, holder, self.user, self.ch, 1)
            chunks = await _take(gen)

        keepalives = [i for i, c in enumerate(chunks) if c == ": keep-alive\n\n"]
        first_data = next((i for i, c in enumerate(chunks)
                           if c.startswith("data:")), len(chunks))
        self.assertTrue(keepalives,
                        f"竞速窗口没有注入心跳：{chunks[:3]}")
        # 竞速窗口里不可能产生 data 帧，所以"全部先于首个 data 帧"就证明这些
        # 心跳来自竞速窗口，而不是 _drain 的静默兜底。
        self.assertLess(max(keepalives), first_data,
                        "心跳未全部先于首个 data 帧，说明不是竞速窗口注入的")
        # 0.25s / 0.02s ≈ 12 个周期；只有一两条说明循环没有真正转起来
        self.assertGreaterEqual(len(keepalives), 3,
                                f"心跳条数异常少：{len(keepalives)}")

    @pytest.mark.django_db
    async def test_race_task_cancelled_when_client_leaves_at_heartbeat(self):
        """客户端在心跳 yield 点断开时必须撕掉竞速 task，否则上游连接泄漏。"""
        cancelled = {"yes": False}

        async def never_race(rs, body):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled["yes"] = True
                raise

        log = self._log("hb-2")
        holder = {"log": log, "started": _time.monotonic()}
        from api import openai_views

        route = MagicMock()
        route.kind = "direct"
        route.key.name = "k0"
        route.key.id = self.key.id
        route.key.channel = self.ch
        route.proxy = None

        with patch("api.openai_views.race_stream", new=never_race), \
                patch.object(sysconfig, "get", _silent_streaming_sysconfig(0.02)):
            gen = openai_views._stream_response(
                [route], {}, holder, self.user, self.ch, 1)
            try:
                first = await _next(gen)
            except asyncio.TimeoutError:
                # 显式关掉再失败：被 wait_for 取消过 __anext__ 的异步生成器处于
                # "仍在运行"状态，留给测试收尾去 aclose 会卡住整个进程
                # （第一次证伪跑就是这样撞满超时的）。守卫要**失败**，不是挂死。
                with suppress(Exception):
                    await gen.aclose()
                self.fail("竞速窗口没有产出心跳帧——守卫不能只会挂死")
            self.assertEqual(first, ": keep-alive\n\n")
            await gen.aclose()          # 模拟客户端断开

        self.assertTrue(cancelled["yes"],
                        "心跳 yield 点断开时没有取消竞速 task → 上游连接泄漏")


class ClientDisconnectForcedSettleTests(IsolatedAsyncioTestCase):
    """客户端中途断开必须强制结算：日志不得滞留 pending，预占必须结算。"""

    async def asyncSetUp(self):
        slug = f"dc-{uuid.uuid4().hex[:8]}"
        self.ch = Channel.objects.create(name=slug, slug=slug,
                                         base_url="https://up.test/v1")
        self.key = ChannelKey.objects.create(channel=self.ch, name="k0",
                                             api_key="nvapi-dc", rpm_limit=100)
        self.user, self.raw = api_key_service.create_key(f"{slug}-user", quota=1000)
        AIModel.objects.create(channel=self.ch, model_name="m", enabled=True)

    async def _deliver_then_disconnect(self, rid):
        """驱动到"已交付一段正文"，然后 aclose 模拟客户端断开。"""
        async def endless():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"部分"}},'
                   '"finish_reason":null}]}\n\n')
            await asyncio.Event().wait()

        w, route = _winner_for(self.ch, self.key.id, endless)

        async def fake_race(rs, body):
            return w

        log = RequestLog.objects.create(
            channel=self.ch, request_id=rid, user_api_key=self.user,
            model="m", routes_count=1, is_stream=True)
        holder = {"log": log, "started": _time.monotonic()}
        from api import openai_views

        with patch("api.openai_views.race_stream", new=fake_race), \
                patch.object(sysconfig, "get", _silent_streaming_sysconfig(0)):
            gen = openai_views._stream_response(
                [route], {}, holder, self.user, self.ch, 1)
            try:
                await _next(gen)          # 拿到已交付正文的那帧
            except asyncio.TimeoutError:
                self.fail("生成器没有产出任何帧")
            await gen.aclose()
        return log

    @pytest.mark.django_db
    async def test_disconnect_marks_log_failed_not_pending(self):
        log = await self._deliver_then_disconnect("dc-1")
        log.refresh_from_db()
        self.assertEqual(
            log.status, "failed",
            "客户端断开后日志仍滞留 pending —— 强制收尾没有生效")
        self.assertIn(log.error_type, ("stream_truncated", "stream_error"))
        self.assertGreater(log.duration_ms, 0)

    @pytest.mark.django_db
    async def test_disconnect_settles_the_quota_reservation(self):
        calls = {"result": [], "usage": []}
        real_result = api_key_service.record_result
        real_usage = api_key_service.record_usage

        def spy_result(rec, success):
            calls["result"].append(success)
            return real_result(rec, success)

        def spy_usage(rec, *a, **k):
            calls["usage"].append((a, k))
            return real_usage(rec, *a, **k)

        with patch.object(api_key_service, "record_result", side_effect=spy_result), \
                patch.object(api_key_service, "record_usage", side_effect=spy_usage):
            await self._deliver_then_disconnect("dc-2")

        self.assertIn(False, calls["result"],
                      "断开路径没记一次失败：total_requests 会与 success+failed 脱节")
        self.assertTrue(calls["usage"], "断开路径没有结算额度预占")
        self.assertTrue(
            any(a and a[-1] == 1 for a, _ in calls["usage"]),
            f"结算未带上 reservation=1（入口预占的那 1 token）：{calls['usage']}")

    @pytest.mark.django_db
    async def test_normal_completion_settles_exactly_once(self):
        """反向守卫：强制收尾不得在正常路径上重复执行（settled 标记互斥）。"""
        async def clean():
            yield ('data: {"choices":[{"index":0,"delta":{"content":"ok"},'
                   '"finish_reason":"stop"}]}\n\n')
            yield "data: [DONE]\n\n"

        w, route = _winner_for(self.ch, self.key.id, clean)

        async def fake_race(rs, body):
            return w

        log = RequestLog.objects.create(
            channel=self.ch, request_id=f"dc-3-{uuid.uuid4().hex[:6]}", user_api_key=self.user,
            model="m", routes_count=1, is_stream=True)
        holder = {"log": log, "started": _time.monotonic()}
        from api import openai_views

        results: list[bool] = []
        real_result = api_key_service.record_result

        def spy(rec, success):
            results.append(success)
            return real_result(rec, success)

        with patch("api.openai_views.race_stream", new=fake_race), \
                patch.object(sysconfig, "get", _silent_streaming_sysconfig(0)), \
                patch.object(api_key_service, "record_result", side_effect=spy):
            gen = openai_views._stream_response(
                [route], {}, holder, self.user, self.ch, 1)
            await _take(gen)

        log.refresh_from_db()
        self.assertEqual(log.status, "success")
        self.assertEqual(results, [True],
                         f"正常完成只应结算一次，实际 {results}")


if __name__ == "__main__":
    unittest.main()


class NonStreamConcurrencyTests(TransactionTestCase):
    """数据面改 async 的核心目的：非流式请求不得再独占一条线程。

    改造前 `race_chat` 是 `asyncio.run(_race(...))`，而同步视图全部由 asgiref 的
    thread-sensitive 执行器承载（`asgiref/sync.py:402` 硬编码 max_workers=1）。
    `asyncio.run` 会阻塞调用线程直到竞速结束，所以一条非流式请求占住那条唯一的
    线程**整个上游往返时长**，不是我们测出的那点 CPU 时间——N 个并发只能一个个排队，
    吞吐上限是 1/上游耗时。

    这条测试直接钉住"等待期是可重叠的"：5 个并发请求、每个上游 0.3s，
    总耗时应当接近 0.3s 而不是 1.5s。
    """

    def setUp(self):
        self.ch = Channel.objects.create(name="conc", slug="conc",
                                         base_url="https://up.test/v1",
                                         is_default=True)
        ChannelKey.objects.create(channel=self.ch, name="k0",
                                  api_key="nvapi-conc", rpm_limit=1000)
        AIModel.objects.create(channel=self.ch, model_name="m", enabled=True)
        self.user, self.raw = api_key_service.create_key("conc-user")

    async def test_concurrent_non_streaming_requests_overlap(self):
        import time
        from unittest.mock import MagicMock, patch

        from api import openai_views
        from services import race_engine
        from services.race_engine import RaceResult

        def make_result(route):
            payload = {
                "id": "chatcmpl-c", "object": "chat.completion", "created": 1,
                "model": "m",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            }
            r = RaceResult(ok=True, route=route, payload=payload,
                           http_status=200)
            r.report = []
            return r

        upstream_seconds = 0.3
        real_race = race_engine._race

        async def slow_race(routes, body):
            # 模拟上游耗时：真实实现里这段时间以前是**占着线程**的
            await asyncio.sleep(upstream_seconds)
            return make_result(routes[0])

        def fake_build(channel=None, **kw):
            route = MagicMock()
            route.kind = "direct"
            route.name = "direct:k0"
            route.key.name = "k0"
            route.key.id = self.ch.keys.first().id
            route.key.channel = self.ch
            route.proxy = None
            route.claimed = False
            return [route]

        from django.test import RequestFactory
        factory = RequestFactory()

        async def one():
            request = factory.post(
                "/v1/chat/completions",
                data=json.dumps({"model": "m",
                                 "messages": [{"role": "user", "content": "hi"}]}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.raw}")
            return await openai_views.chat_completions(request)

        with patch.object(openai_views, "build_routes", side_effect=fake_build), \
                patch.object(race_engine, "_race", new=slow_race), \
                patch.object(openai_views, "race_chat",
                             new=lambda routes, body: race_engine.race_chat(routes, body)):
            t0 = time.monotonic()
            responses = await asyncio.gather(*(one() for _ in range(5)))
            elapsed = time.monotonic() - t0

        self.assertEqual([r.status_code for r in responses], [200] * 5)
        serialized = 5 * upstream_seconds
        self.assertLess(
            elapsed, upstream_seconds * 2.5,
            f"5 个并发非流式请求耗时 {elapsed:.2f}s，接近串行的 {serialized:.2f}s —— "
            "说明上游等待期仍然独占线程，async 化没生效")
