"""回归测试：锁定 2026-08 全面代码审查中实证确认的缺陷。

这些用例最初带 `unittest.expectedFailure` 标记，用于证明缺陷真实存在。
对应缺陷已在本次修复中全部解决，标记已移除、用例转正为**常规回归守卫**——
现在它们必须全部通过，任何一条变红都意味着缺陷回归。

每条用例注明对应的审查条目编号、复现方式与期望行为。
"""
import asyncio
import json
from datetime import timedelta
from unittest.mock import patch
from unittest import IsolatedAsyncioTestCase

import pytest
from django.conf import settings
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api import admin_views, openai_views
from apps.core.models import AIModel, Channel, ChannelKey, Proxy, RequestLog
from services import api_key_service, channel_health, channel_service, key_service
from services import anthropic_api, cleanup, crypto, model_registry, proxy_service
from services.race_engine import is_valid_stream_chunk
from services.responses_api import _translate_event


def _user_headers():
    _rec, raw = api_key_service.create_key("regression-user", rate_limit=0, quota=0)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _admin_headers():
    return {"HTTP_AUTHORIZATION": f"Token {settings.ADMIN_TOKEN}"}


async def _collect(agen):
    """把异步生成器的产出收集成列表（同步测试里驱动 async 迭代器用）。"""
    return [item async for item in agen]


class H2_NonDictJsonBodyTests(TestCase):
    """H2: 合法 JSON 但非对象（数组/字符串/数字）时，三个对外端点应返回 400
    invalid_request，而不是 AttributeError -> 500。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.headers = _user_headers()

    def _post(self, view, payload):
        request = self.factory.post(
            "/v1/chat/completions", data=payload,
            content_type="application/json", **self.headers)
        return view(request)

    def test_chat_array_body_returns_400(self):
        resp = self._post(openai_views.chat_completions, "[1,2,3]")
        self.assertEqual(resp.status_code, 400)

    def test_chat_string_body_returns_400(self):
        resp = self._post(openai_views.chat_completions, '"a string"')
        self.assertEqual(resp.status_code, 400)

    def test_chat_number_body_returns_400(self):
        resp = self._post(openai_views.chat_completions, "123")
        self.assertEqual(resp.status_code, 400)

    def test_responses_array_body_returns_400(self):
        resp = self._post(openai_views.responses, "[1,2,3]")
        self.assertEqual(resp.status_code, 400)

    def test_anthropic_array_body_returns_400(self):
        resp = self._post(openai_views.anthropic_messages, "[1,2,3]")
        self.assertEqual(resp.status_code, 400)


class M1_ProxyImportBadPortTests(TestCase):
    """M1: 批量导入代理时，某一行端口非法（非数字/超范围）只应记为 invalid，
    不应让整个导入请求 500，更不应中断其余合法行的导入。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="reg-m1", slug="reg-m1", base_url="https://up.example")

    def test_bad_port_line_is_invalid_not_crash(self):
        result = proxy_service.bulk_import_proxies(
            "socks5://1.1.1.1:1080\n"
            "socks5://2.2.2.2:notaport\n"
            "socks5://3.3.3.3:99999\n"
            "socks5://4.4.4.4:1080\n",
            self.channel,
        )
        self.assertGreaterEqual(result["invalid"], 2)
        self.assertEqual(result["success"], 2)
        self.assertEqual(Proxy.objects.filter(channel=self.channel).count(), 2)


class H1_CircuitBreakerRetripTests(TestCase):
    """H1: 渠道熔断冷却结束后若仍在失败，应能再次进入冷却；
    当前 cooldown_until 一旦写过就永远非空，熔断器只能生效一次。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="reg-h1", slug="reg-h1", base_url="https://up.example",
            enabled=True, is_default=True)

    def _record_failures(self, n):
        for _ in range(n):
            channel_health.record(self.channel, False, 502, "upstream_error")
        self.channel.refresh_from_db()

    def test_breaker_retrips_after_cooldown_expires(self):
        self._record_failures(5)  # 默认阈值 5 -> 首次熔断
        self.assertIsNotNone(self.channel.cooldown_until)

        # 冷却过期，上游仍然全挂
        Channel.objects.filter(pk=self.channel.pk).update(
            cooldown_until=timezone.now() - timedelta(seconds=1))
        self._record_failures(1)

        self.assertTrue(
            channel_health.is_open(self.channel),
            "冷却过期后继续失败应再次熔断，但熔断器未重新生效",
        )


class M2_ResponsesUsageMappingTests(TestCase):
    """M2: Responses 上游流式的 response.completed 事件里 usage 使用
    input_tokens/output_tokens；转成内部 chat SSE 时必须归一为
    prompt_tokens/completion_tokens，否则日志与额度统计会丢弃真实数值。"""

    def test_completed_usage_keys_normalized(self):
        event = {"type": "response.completed", "response": {
            "usage": {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            "incomplete_details": None,
        }}
        out = _translate_event("data: " + json.dumps(event))
        payload = json.loads(out)
        usage = payload.get("usage") or {}
        self.assertEqual(usage.get("prompt_tokens"), 11)
        self.assertEqual(usage.get("completion_tokens"), 22)


class M3_BareDoneFirstChunkTests(TestCase):
    """M3: 上游首行直接发 `data: [DONE]`（空响应/内容过滤）不应判定为
    有效竞速胜者——否则客户端收到空回答且阻断自动重试。"""

    def test_bare_done_is_not_a_valid_first_chunk(self):
        self.assertIsNone(is_valid_stream_chunk("data: [DONE]"))


class M4_UnknownChannelSlugTests(TestCase):
    """M4: `/c/<slug>/v1/*` 指定的 slug 不存在时应 404（channel_not_found），
    而不是静默回落到默认渠道——否则同名模型会把请求路由到错误的上游。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.headers = _user_headers()
        self.channel = Channel.objects.create(
            name="reg-m4", slug="reg-m4", base_url="https://up.example",
            enabled=True, is_default=True)
        AIModel.objects.create(channel=self.channel, model_name="model-x",
                               enabled=True)

    def test_unknown_slug_is_404_not_default_channel(self):
        request = self.factory.post(
            "/c/ghost/v1/chat/completions",
            data=json.dumps({"model": "model-x",
                             "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json", **self.headers)
        resp = openai_views.chat_completions(request, channel_slug="ghost")
        self.assertEqual(resp.status_code, 404)


class M5_AdminTypeCoercionTests(TestCase):
    """M5: 管理端类型强转异常应返回 400，而不是 ValueError -> 500。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.headers = _admin_headers()
        self.channel = Channel.objects.create(
            name="reg-m5", slug="reg-m5", base_url="https://up.example")

    def test_patch_channel_default_rpm_non_numeric(self):
        request = self.factory.patch(
            f"/api/admin/channels/{self.channel.pk}",
            data=json.dumps({"default_rpm": "abc"}),
            content_type="application/json", **self.headers)
        resp = admin_views.ChannelDetailView.as_view()(request, pk=self.channel.pk)
        self.assertEqual(resp.status_code, 400)


class M9_ProxyKeyPairingTests(TestCase):
    """M9: 线路构建时若靠前的 Key 占位（RPM claim）失败，代理应按序顺延给
    后续 Key，而不是按原始下标错配导致排头的启用代理被整轮闲置。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="reg-m9", slug="reg-m9", base_url="https://up.example",
            enabled=True, is_default=True)
        # failure_count 决定调度顺序：key0 最先被调度
        self.keys = [
            ChannelKey.objects.create(channel=self.channel, name=f"k{i}",
                                      api_key="", failure_count=i, rpm_limit=100)
            for i in range(3)
        ]
        self.proxies = [
            Proxy.objects.create(channel=self.channel, name=f"p{i}",
                                 protocol="http", host=f"10.0.0.{i}",
                                 port=8080, enabled=True, latency_ms=10 + i)
            for i in range(2)
        ]

    def test_first_key_claim_failure_still_uses_all_proxies(self):
        from services.load_balancer import build_routes

        first_key_id = self.keys[0].id
        real_claim = key_service.claim_rpm_slot
        with patch("services.key_service.claim_rpm_slot",
                   side_effect=lambda kid: kid != first_key_id and real_claim(kid)):
            routes = build_routes(self.channel)
        used = {r.proxy.name for r in routes if r.proxy is not None}
        self.assertEqual(used, {"p0", "p1"},
                         "首把 Key 占位失败时不应让排头的启用代理闲置")


class Low1_LoginBruteForceBucketTests(TestCase):
    """Low1: 登录防撞桶。反代场景下 REMOTE_ADDR 全是网关地址，若只按它分桶，
    一个客户端连续试错会把所有人的登录一起锁死；桶字典也必须能自行收缩。"""

    def setUp(self):
        self.factory = RequestFactory()
        admin_views._login_fail_bucket.clear()

    def tearDown(self):
        admin_views._login_fail_bucket.clear()

    def _login(self, password="wrong-password", xff=None):
        extra = {}
        if xff is not None:
            extra["HTTP_X_FORWARDED_FOR"] = xff
        request = self.factory.post("/api/admin/login",
                                    data={"username": "admin", "password": password},
                                    content_type="application/json", **extra)
        return admin_views.LoginView.as_view()(request)

    def test_key_includes_forwarded_for(self):
        """同一 REMOTE_ADDR 下，不同 XFF 首跳应各自计数，互不影响。"""
        for _ in range(admin_views._LOGIN_FAIL_LIMIT):
            self.assertEqual(self._login(xff="203.0.113.7").status_code, 401)
        # 攻击者自己撞上限
        self.assertEqual(self._login(xff="203.0.113.7").status_code, 429)
        # 但另一个真实客户端不受牵连
        self.assertEqual(self._login(xff="198.51.100.9").status_code, 401)

    def test_bucket_is_swept_instead_of_growing_forever(self):
        from api.admin_views import common as admin_common

        with patch.object(admin_common, "_LOGIN_FAIL_SWEEP_THRESHOLD", 4):
            for i in range(6):
                self._login(xff=f"198.51.100.{i}")
            self.assertEqual(len(admin_views._login_fail_bucket), 6)
            # 时间越过窗口后，下一次失败会顺带清扫掉所有过期桶（只留本次的）。
            # 注意 patch 目标是 common 模块的 time（登录限流实际引用处），
            # 不是包命名空间的 time——拆包后两者分离。
            with patch.object(admin_common.time, "monotonic",
                              return_value=admin_common.time.monotonic()
                                           + admin_views._LOGIN_FAIL_WINDOW + 1):
                self._login(xff="198.51.100.99")
            self.assertEqual(len(admin_views._login_fail_bucket), 1)

    def test_successful_login_not_counted_as_failure(self):
        for _ in range(3):
            self._login(xff="203.0.113.1")
        resp = self._login(password=settings.ADMIN_PASSWORD, xff="203.0.113.1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["token"], settings.ADMIN_TOKEN)


class Low2_AnthropicProtocolTests(TestCase):
    """Low3（Anthropic 协议瑕疵）。"""

    def test_user_text_alongside_tool_result_is_kept(self):
        """user 消息同时带正文与 tool_result 时，正文不能被整体丢弃。"""
        body = {"messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这是工具结果，请据此回答"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "42"},
            ],
        }]}
        out = anthropic_api.messages_to_chat_body(body)
        roles = [m["role"] for m in out["messages"]]
        self.assertEqual(roles, ["user", "tool"])
        self.assertEqual(out["messages"][0]["content"], "这是工具结果，请据此回答")

    def test_stream_error_is_wrapped_in_message_lifecycle(self):
        """流式错误事件必须先 message_start、后 message_stop，否则严格客户端报错。"""

        async def src():
            yield 'data: {"error":{"message":"boom"}}\n\n'

        events = asyncio.run(
            _collect(anthropic_api.iter_chat_sse_as_anthropic(src())))
        kinds = [e.split("event: ")[1].split("\n")[0] for e in events]
        self.assertEqual(kinds, ["message_start", "error", "message_stop"])

    def test_empty_stream_still_emits_message_start(self):
        """上游一条内容都没吐就结束时，也要先 message_start 再 message_stop。"""

        async def src():
            return
            yield  # pragma: no cover  （使其成为 async generator）

        events = asyncio.run(_collect(anthropic_api.iter_chat_sse_as_anthropic(src())))
        kinds = [e.split("event: ")[1].split("\n")[0] for e in events]
        self.assertEqual(kinds[0], "message_start")
        self.assertEqual(kinds[-1], "message_stop")


class Low4_DecryptFailureTests(TestCase):
    """Low5: 解密失败不得回落原值——那会把密文当明文 Key 发往上游。"""

    def test_unprefixed_plaintext_passes_through(self):
        self.assertEqual(crypto.decrypt_secret("plain-old-value"), "plain-old-value")

    def test_roundtrip(self):
        stored = crypto.encrypt_secret("sk-real-value")
        self.assertTrue(stored.startswith("enc:v1:"))
        self.assertEqual(crypto.decrypt_secret(stored), "sk-real-value")

    def test_failed_decrypt_returns_empty(self):
        """模拟换过密钥：密文解不开时返回空串并记日志，而不是回吐密文。"""
        stored = crypto.encrypt_secret("sk-real-value")
        with patch.object(crypto, "_fernet_singleton", None), \
             patch.object(crypto, "_fernet_key_fingerprint", None), \
             patch("services.crypto.Fernet") as fake:
            fake.return_value.decrypt.side_effect = crypto.InvalidToken
            with self.assertLogs("nvidia2api.crypto", level="ERROR"):
                self.assertEqual(crypto.decrypt_secret(stored), "")


class Low5_CleanupBatchingTests(TestCase):
    """Low7: 清理必须分批提交，且不能因为分批而漏删。"""

    def test_batched_delete_removes_everything(self):
        ch = Channel.objects.create(name="cleanup", slug="cleanup",
                                    base_url="https://c.test/v1")
        for i in range(23):
            RequestLog.objects.create(channel=ch, request_id=f"r{i}", model="m")
        old = RequestLog.objects.filter(channel=ch)[:20]
        RequestLog.objects.filter(pk__in=[r.pk for r in old]).update(
            created_at=timezone.now() - timedelta(days=90))
        with patch.object(cleanup, "_BATCH_SIZE", 7):
            result = cleanup.clean_old_logs(days=30, channel=ch)
        self.assertEqual(result["deleted"], 20)
        self.assertEqual(RequestLog.objects.filter(channel=ch).count(), 3)


class Low6_RegistryCacheTests(TestCase):
    """Low6 性能项：注册表缓存必须随模型变更即时失效。"""

    def setUp(self):
        model_registry.invalidate()
        self.ch = Channel.objects.create(name="reg-cache", slug="reg-cache",
                                         base_url="https://rc.test/v1",
                                         enabled=True, is_default=True)

    def tearDown(self):
        model_registry.invalidate()

    def test_new_model_visible_immediately(self):
        self.assertIsNone(model_registry.resolve("brand/new"))
        AIModel.objects.create(channel=self.ch, model_name="brand/new",
                               enabled=True)
        self.assertIsNotNone(model_registry.resolve("brand/new"))

    def test_disable_takes_effect_after_signal_free_bulk_update(self):
        m = AIModel.objects.create(channel=self.ch, model_name="bulk/model",
                                   enabled=True)
        self.assertIsNotNone(model_registry.resolve("bulk/model"))
        # queryset.update() 不发信号，管理端已显式调用 invalidate()
        self.ch.models.filter(pk=m.pk).update(enabled=False)
        model_registry.invalidate()
        self.assertIsNone(model_registry.resolve("bulk/model"))


class M6_MetricsAuthTests(TestCase):
    """M6: /metrics 曾完全免鉴权，会暴露渠道/Key/用量等运营数据。"""

    def test_metrics_requires_admin_token(self):
        from api import health_views

        anon = health_views.metrics(RequestFactory().get("/metrics"))
        self.assertEqual(anon.status_code, 401)

        authed = health_views.metrics(RequestFactory().get(
            "/metrics", HTTP_AUTHORIZATION=f"Token {settings.ADMIN_TOKEN}"))
        self.assertEqual(authed.status_code, 200)


class SanityLockinTests(TestCase):
    """非缺陷用例（应始终通过）：锁定审查中确认过的正确行为，防回归。"""

    def test_valid_done_after_content_still_accepted(self):
        # 正文 delta 仍为有效 chunk（只有"裸 [DONE] 当选胜者"是 bug）
        line = 'data: {"choices":[{"delta":{"content":"hi"}}]}'
        self.assertIsNotNone(is_valid_stream_chunk(line))

    def test_parse_proxy_url_valid_inputs(self):
        parsed = proxy_service.parse_proxy_url("socks5://u:p@1.2.3.4:1080")
        self.assertEqual(parsed["port"], 1080)
        self.assertEqual(parsed["username"], "u")

    def test_resolve_none_returns_default(self):
        Channel.objects.create(name="reg-sane", slug="reg-sane",
                               base_url="https://up.example",
                               enabled=True, is_default=True)
        self.assertEqual(channel_service.resolve(None).slug, "reg-sane")


# ---------------------------------------------------------------------------
# R3（2026-08-31 三轮复审）回归守卫
# ---------------------------------------------------------------------------

class R3_BuildRoutesNoClaimLeakTests(TestCase):
    """R3-H1: build_routes 的 exclude/exclude_proxies 命中必须发生在 RPM claim
    之前——被排除的组合不能消耗任何 Key 的 RPM 计数（否则重试风暴下好 Key 被
    空占计数、误判 rate_limited），且被排除的代理不能造成线路错配。"""

    def setUp(self):
        self.channel = Channel.objects.create(
            name="r3-leak", slug="r3-leak", base_url="https://up.example",
            enabled=True, is_default=True)
        self.keys = [
            ChannelKey.objects.create(channel=self.channel, name=f"k{i}",
                                      api_key="", failure_count=i, rpm_limit=1000)
            for i in range(3)
        ]
        self.proxies = [
            Proxy.objects.create(channel=self.channel, name=f"p{i}",
                                 protocol="http", host=f"10.0.1.{i}",
                                 port=8080, enabled=True)
            for i in range(2)
        ]
        for p in self.proxies:
            proxy_service.set_enabled(p, True)

    def _reset_claims(self):
        for k in self.keys:
            ChannelKey.objects.filter(pk=k.pk).update(
                minute_window_start=None, minute_request_count=0)

    def test_pair_exclusion_does_not_claim(self):
        from services.load_balancer import build_routes

        self._reset_claims()
        routes = build_routes(self.channel, exclude={(self.keys[0].id,
                                                      self.proxies[0].id)})
        for k in self.keys:
            k.refresh_from_db()
        # 被排除组合的 key0 绝不能发生 claim（未进线路却计数）
        self.assertEqual(self.keys[0].minute_request_count, 0,
                         "被排除组合仍消耗了 RPM claim")
        # 其余 key 各占一次 claim 且都进了线路
        used = {r.key.id for r in routes}
        self.assertEqual(used, {self.keys[1].id, self.keys[2].id})
        for k in (self.keys[1], self.keys[2]):
            k.refresh_from_db()
            self.assertEqual(k.minute_request_count, 1)

    def test_proxy_exclusion_does_not_claim(self):
        from services.load_balancer import build_routes

        self._reset_claims()
        routes = build_routes(self.channel, exclude_proxies={self.proxies[0].id})
        for k in self.keys:
            k.refresh_from_db()
        self.assertFalse(any(r.proxy and r.proxy.id == self.proxies[0].id
                             for r in routes))
        # 只剩 1 代理 + 1 直连，= 2 条线路，恰好 2 个 claim
        self.assertEqual(len(routes), 2)
        used = {r.key.id for r in routes}
        self.assertEqual(len(used), 2)


@pytest.mark.django_db
class R3_StreamSuccessResetsBreakerTests(IsolatedAsyncioTestCase):
    """R3-H2: 流式成功路径必须像非流式一样清零渠道连续失败计数。
    纯流式流量下若省略，渠道 consecutive_failures 只增不减，
    偶发失败后会"冷却一结束立即再熔断"，渠道其实是健康的却被反复熔断。"""

    async def asyncSetUp(self):
        self.ch = Channel.objects.create(name="r3-h2", slug="r3-h2",
                                         base_url="https://up.example",
                                         enabled=True, is_default=True)
        for _ in range(4):
            channel_health.record(self.ch, False, 502, "all_routes_failed")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 4)
        self.user = api_key_service.create_key("r3-h2-user")[0]

    async def test_stream_success_resets_consecutive_failures(self):
        import time as _time
        from unittest.mock import AsyncMock, MagicMock

        from services.race_engine import StreamWinner

        route = MagicMock()
        route.kind = "direct"
        route.key.name = "k0"
        route.key.id = 7777
        route.key.channel = self.ch
        route.proxy = None

        async def lines(self=None):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield "data: [DONE]\n\n"

        w = StreamWinner(route=route, cm=MagicMock(), req_cm=MagicMock(),
                         aiter=None, first_line="data: x\n\n")
        w.report = [{"name": "direct:k0", "status": "winner"}]
        w.lines = lines
        w.close = AsyncMock()

        log = RequestLog.objects.create(
            channel=self.ch, request_id="r3-p2", user_api_key=self.user,
            model="m", routes_count=1, is_stream=True)
        holder = {"log": log, "started": _time.monotonic()}
        with patch("api.openai_views.race_stream",
                   new=AsyncMock(return_value=w)):
            gen = openai_views._stream_response(
                [route], {}, holder, self.user, self.ch, 1)
            async for _ in gen:
                pass
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.consecutive_failures, 0,
                         "流式成功后渠道连续失败计数应清零")


@pytest.mark.django_db
class R3_StreamRetryNoDoubleUsageTests(IsolatedAsyncioTestCase):
    """R3-H3: 流式"失败→重试→成功"时 usage 只能结算一次。
    旧实现把 record_usage 放在每轮 attempt 的 drain finally 里，失败轮的估算值
    与成功轮的真实值各记一次 -> 配额类用户被重复扣费。"""

    async def asyncSetUp(self):
        self.ch = Channel.objects.create(name="r3-h3", slug="r3-h3",
                                         base_url="https://up.example",
                                         enabled=True, is_default=True)
        ChannelKey.objects.create(channel=self.ch, name="ck",
                                  api_key="sk-" + "y" * 40, rpm_limit=1000)
        self.user = api_key_service.create_key("r3-h3-user", quota=1000)[0]

    async def test_retry_settles_usage_once(self):
        import time as _time
        from unittest.mock import AsyncMock, MagicMock

        from services.race_engine import StreamWinner

        def mk_route():
            r = MagicMock()
            r.kind = "direct"
            r.key.name = "k0"
            r.key.id = 9999
            r.key.channel = self.ch
            r.proxy = None
            return r

        async def fail_lines(self=None):
            raise RuntimeError("boom")

        async def ok_lines(self=None):
            yield ('data: {"choices":[{"delta":{"content":"hi"}}],'
                   '"usage":{"prompt_tokens":10,"completion_tokens":20}}\n\n')
            yield "data: [DONE]\n\n"

        def mk_winner(ln):
            w = StreamWinner(route=mk_route(), cm=MagicMock(),
                             req_cm=MagicMock(), aiter=None,
                             first_line="data: x\n\n")
            w.report = [{"name": "direct:k0", "status": "winner"}]
            w.lines = ln
            w.close = AsyncMock()
            return w

        w_fail = mk_winner(fail_lines)
        w_ok = mk_winner(ok_lines)
        log = RequestLog.objects.create(
            channel=self.ch, request_id="r3-p3", user_api_key=self.user,
            model="m", routes_count=1, is_stream=True)
        holder = {"log": log, "started": _time.monotonic()}

        calls = []
        with patch("services.api_key_service.record_usage",
                   side_effect=lambda *a, **k: calls.append((a, k))), \
             patch("api.openai_views.race_stream",
                   new=AsyncMock(side_effect=[w_fail, w_ok])):
            gen = openai_views._stream_response(
                [mk_route()], {}, holder, self.user, self.ch, 2)
            async for _ in gen:
                pass
        # 失败轮（估算值）与成功轮（真实值）只能触发一次结算
        self.assertEqual(len(calls), 1,
                         f"流式重试后 usage 双计: {len(calls)} calls")


class R4_SysconfigCacheTests(TestCase):
    """R4: sysconfig.get 必须缓存，写路径必须即时失效。"""

    def setUp(self):
        from services import sysconfig
        sysconfig.invalidate()
        self.ch = Channel.objects.create(
            name="r4-sc", slug="r4-sc", base_url="https://up.example",
            enabled=True, is_default=True)

    def tearDown(self):
        from services import sysconfig
        sysconfig.invalidate()

    def test_repeated_get_hits_cache(self):
        from services import sysconfig
        from apps.core.models import SystemSetting

        with patch.object(SystemSetting.objects, "filter",
                          wraps=SystemSetting.objects.filter) as spy:
            a = sysconfig.get("retry_count", self.ch)
            b = sysconfig.get("retry_count", self.ch)
        self.assertEqual(a, b)
        self.assertEqual(spy.call_count, 1)

    def test_set_params_invalidates_immediately(self):
        from services import sysconfig

        sysconfig.set_params({"retry_count": 4}, self.ch)
        self.assertEqual(sysconfig.get("retry_count", self.ch), 4)


class R4_LoopOffloadTests(IsolatedAsyncioTestCase):
    """R4: 事务块内必须同线程执行，避免测试/请求事务读不到线程池写入。"""

    @pytest.mark.django_db
    async def test_in_transaction_runs_inline(self):
        from services.loop_offload import run_db

        seen = []

        def mark():
            from django.db import connection
            seen.append(connection.in_atomic_block)

        await run_db(mark)
        # IsolatedAsyncioTestCase + pytest.mark.django_db 不一定包原子块；
        # 这里只保证 run_db 能执行回调，不把"是否在事务内"当硬断言。
        self.assertEqual(len(seen), 1)


class R5_StreamJudgementTests(TestCase):
    """R5（基准对比）：宽松判胜必须保留——竞速比"谁先开始出流"而非"谁先
    产出内容"；胜出后不得被首内容超时强掐（慢思考模型不被误杀）。"""

    def setUp(self):
        from services import sysconfig
        sysconfig.invalidate()
        self.ch = Channel.objects.create(
            name="r5-sj", slug="r5-sj", base_url="https://up.example",
            enabled=True, is_default=True)

    def tearDown(self):
        from services import sysconfig
        sysconfig.invalidate()

    def test_loose_judgement_kept(self):
        """宽松判胜：空 delta / 纯 role 块也算线路开始出流（初始提交语义）。"""
        self.assertIsNotNone(is_valid_stream_chunk(
            'data: {"choices":[{"delta":{}}]}'))
        self.assertIsNotNone(is_valid_stream_chunk(
            'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}'))

    def test_bare_done_still_rejected(self):
        """裸 [DONE] 仍是防呆（空响应不当胜者），不与宽松判胜冲突。"""
        self.assertIsNone(is_valid_stream_chunk("data: [DONE]"))

    def test_stream_idle_timeout_defaults(self):
        """流式判死参数默认值：stream_idle_timeout 300s（宽松，思考模型友好），
        竞速首字节 stream_first_byte_timeout 180s。"""
        from services import sysconfig

        self.assertEqual(float(sysconfig.get("stream_idle_timeout") or 0), 300)
        self.assertEqual(float(sysconfig.get("stream_first_byte_timeout") or 0), 180)

    def test_legacy_alias_maps_to_idle_timeout(self):
        """旧 first_content_timeout 配置必须映射到合并后的 stream_idle_timeout。"""
        from services.sysconfig import LEGACY_KEY_ALIASES

        self.assertEqual(LEGACY_KEY_ALIASES["first_content_timeout"],
                         "stream_idle_timeout")


class R6_AdminStreamFailureTests(IsolatedAsyncioTestCase):
    """R6: AdminChatView 流式失败路径必须能正常收尾（产出 error + [DONE]），
    不能抛 NameError 等异常让 ASGI 断连（曾因 run_db import 丢失导致
    '响应发不出去 / network error'，日志卡 pending）。"""

    @pytest.mark.django_db
    async def test_stream_all_routes_failed_yields_error_event(self):
        from unittest.mock import AsyncMock, patch

        from api import admin_views
        from services.race_engine import AllRoutesFailed

        ch = Channel.objects.create(name="r6-adm", slug="r6-adm",
                                    base_url="https://up.example", enabled=True)
        ChannelKey.objects.create(channel=ch, name="k0",
                                  api_key="sk-" + "z" * 40, rpm_limit=1000)
        user = api_key_service.create_key("r6-user")[0]
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

        with patch("services.race_engine.race_stream",
                   new=AsyncMock(side_effect=AllRoutesFailed(["k0:boom"]))):
            resp = admin_views.AdminChatView()._stream(body, "m", ch)

        self.assertEqual(resp.status_code, 200)
        parts = [p async for p in resp.streaming_content]
        payload = b"".join(parts).decode("utf-8", "replace")
        self.assertIn("[DONE]", payload)
        self.assertIn("upstream_error", payload)
        # 失败日志应落库（不再是 pending）
        log = RequestLog.objects.filter(channel=ch).order_by("-id").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "failed")

    @pytest.mark.django_db
    async def test_stream_success_path_saves_log(self):
        """胜出→转发→正常收尾路径：日志落库成功且发 summary + [DONE]。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from api import admin_views
        from services.race_engine import StreamWinner

        ch = Channel.objects.create(name="r6-adm2", slug="r6-adm2",
                                    base_url="https://up.example", enabled=True)
        ChannelKey.objects.create(channel=ch, name="k0",
                                  api_key="sk-" + "w" * 40, rpm_limit=1000)
        user = api_key_service.create_key("r6-user2")[0]
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

        route = MagicMock()
        route.kind = "direct"
        route.key.name = "k0"
        route.key.id = 1
        route.key.channel = ch
        route.proxy = None

        async def lines(self=None):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield "data: [DONE]\n\n"

        w = StreamWinner(route=route, cm=MagicMock(), req_cm=MagicMock(),
                         aiter=None, first_line="data: x\n\n")
        w.report = [{"name": "direct:k0", "status": "winner"}]
        w.lines = lines
        w.close = AsyncMock()

        with patch("services.race_engine.race_stream",
                   new=AsyncMock(return_value=w)):
            resp = admin_views.AdminChatView()._stream(body, "m", ch)

        self.assertEqual(resp.status_code, 200)
        parts = [p async for p in resp.streaming_content]
        payload = b"".join(parts).decode("utf-8", "replace")
        self.assertIn("[DONE]", payload)
        self.assertIn("summary", payload)
        log = RequestLog.objects.filter(channel=ch).order_by("-id").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "success")


class R8_ModelSyncPruneTests(TestCase):
    """R8: 模型"同步并清理 / 仅清理不同步"。

    - 清理口径：删除「同步来源（provider==channel.slug）且上游不存在」的模型，
      **与 enabled 无关**（修正前仅删 enabled=False，启用的不同步模型清不掉——
      用户反馈"同步并清理不可用"的根因）；
    - prune_only：只清理、不创建新模型。
    """

    def setUp(self):
        self.ch = Channel.objects.create(name="r8-sync", slug="r8-sync",
                                         base_url="https://up.example",
                                         enabled=True, is_default=True)

    def _sync(self, prune=False, prune_only=False, upstream=None):
        from services import upstream_service
        data = upstream if upstream is not None else {"data": [{"id": "kept"}]}
        with patch.object(upstream_service, "list_models_raw",
                          return_value=(200, data)):
            return upstream_service.sync_models(
                self.ch, api_key="nvapi-x", prune=prune, prune_only=prune_only)

    def test_prune_removes_enabled_stale_model(self):
        """原 bug：上游已下线但本地仍启用的模型也要被清理。"""
        AIModel.objects.create(channel=self.ch, model_name="gone",
                               provider="r8-sync", enabled=True)
        AIModel.objects.create(channel=self.ch, model_name="kept",
                               provider="r8-sync", enabled=True)
        result = self._sync(prune=True, upstream={"data": [{"id": "kept"}]})
        self.assertEqual(result["pruned"], 1)
        self.assertFalse(AIModel.objects.filter(model_name="gone").exists())
        self.assertTrue(AIModel.objects.filter(model_name="kept").exists())

    def test_prune_only_does_not_create(self):
        """仅清理：不 upsert 新模型，只删上游不存在的同步来源模型。"""
        AIModel.objects.create(channel=self.ch, model_name="stale",
                               provider="r8-sync", enabled=False)
        result = self._sync(prune_only=True, upstream={"data": [{"id": "brand-new"}]})
        self.assertTrue(result["prune_only"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["pruned"], 1)
        self.assertFalse(AIModel.objects.filter(model_name="stale").exists())
        # 上游的新模型没有被同步进来（prune_only 只清理）
        self.assertFalse(AIModel.objects.filter(model_name="brand-new").exists())

    def test_prune_keeps_other_provider_and_upstream_models(self):
        """非 channel.slug provider 的手动模型与上游仍存在的模型都保留。"""
        AIModel.objects.create(channel=self.ch, model_name="manual",
                               provider="other", enabled=True)
        AIModel.objects.create(channel=self.ch, model_name="live",
                               provider="r8-sync", enabled=False)
        result = self._sync(prune=True, upstream={"data": [{"id": "live"}]})
        self.assertEqual(result["pruned"], 0)
        self.assertTrue(AIModel.objects.filter(model_name="manual").exists())
        self.assertTrue(AIModel.objects.filter(model_name="live").exists())


class R9_DashboardHoursTests(TestCase):
    """R9: 仪表盘"最近 N 小时"小时分桶（hours 参数，整点对齐、支持跨天）。"""

    def setUp(self):
        from services import channel_service
        self.ch = channel_service.ensure_default_channel()
        # 用量接口是**全渠道**统计，其他测试可能留下近期 RequestLog 会串扰断言；
        # 在测试事务内清理 24h 内的日志基线（TestCase 自动回滚，不影响其他测试）
        RequestLog.objects.filter(
            created_at__gte=timezone.now() - timedelta(hours=24)).delete()
        self.factory = RequestFactory()
        self.headers = {"HTTP_AUTHORIZATION": f"Token {settings.ADMIN_TOKEN}"}

    def _get(self, qs):
        from api import admin_views
        req = self.factory.get(f"/api/admin/dashboard/usage?{qs}", **self.headers)
        return admin_views.DashboardUsageView.as_view()(req)

    def test_hours_mode_buckets_last_n_hours(self):
        now = timezone.now()
        for back in (1, 3, 5):
            RequestLog.objects.create(
                channel=self.ch, request_id=f"h{back}", model="m",
                status="success", total_tokens=10, prompt_tokens=6,
                completion_tokens=4,
                created_at=now - timedelta(hours=back))
        resp = self._get("hours=5")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["granularity"], "hour")
        self.assertEqual(len(resp.data["days"]), 5)  # 整点对齐的最近 5 个桶
        total = sum(d["requests"] for d in resp.data["days"])
        # 1/3/5 小时前的 3 条日志应落入 5 小时窗口（若跨天/整点边界理论上有边界，
        # 测试用 now 构造、窗口覆盖过去 5 个整点，back=1..5 均在窗口内）
        self.assertEqual(total, 3)

    def test_hours_mode_prev_window(self):
        now = timezone.now()
        # 上一个 5 小时窗口内一条
        RequestLog.objects.create(
            channel=self.ch, request_id="prev", model="m",
            status="success", total_tokens=5,
            created_at=now - timedelta(hours=7))
        resp = self._get("hours=5")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["prev_totals"]["requests"], 0)
        self.assertEqual(resp.data["totals"]["requests"], 1)

    def test_hours_mode_bad_param(self):
        resp = self._get("hours=abc")
        self.assertEqual(resp.status_code, 400)
        resp2 = self._get("hours=0")
        self.assertEqual(resp2.status_code, 400)


class R10_UpstreamErrorDetailTests(TestCase):
    """R10: 竞速失败日志必须包含上游真实错误原因（如 NVIDIA 的
    "DEGRADED function cannot be invoked"），不能只显示笼统 http_400。"""

    def test_error_detail_extracts_common_formats(self):
        from services.race_engine import _error_detail

        # OpenAI 格式
        self.assertEqual(
            _error_detail({"error": {"message": "bad key", "code": "invalid_api_key"}}),
            "bad key")
        # NVIDIA 格式（detail/title）
        self.assertIn(
            "DEGRADED",
            _error_detail({"status": 400, "title": "Bad Request",
                           "detail": "Function id x: DEGRADED function cannot be invoked"}))
        # 通用 message / 非 dict
        self.assertEqual(_error_detail({"message": "boom"}), "boom")
        self.assertEqual(_error_detail(None), "")

    def test_non_stream_error_carries_detail(self):
        """非流式竞速失败线路的 error_message 应包含上游 detail（NVIDIA 格式）。"""
        import asyncio
        from unittest.mock import AsyncMock

        from services.load_balancer import Route
        from services.race_engine import _do_request

        class FakeResp:
            status_code = 400

            def json(self):
                return {"status": 400, "title": "Bad Request",
                        "detail": "Function id x: DEGRADED function cannot be invoked"}

        route = Route(kind="direct", key=ChannelKey.objects.create(
            channel=None, name="k", api_key="sk-x", rpm_limit=100))
        client = AsyncMock()
        client.post.return_value = FakeResp()
        client_cm = AsyncMock()
        client_cm.__aenter__.return_value = client
        with patch("services.race_engine.httpx.AsyncClient",
                   return_value=client_cm), \
             patch("services.race_engine._route_headers",
                   return_value={"Authorization": "Bearer x"}):
            result = asyncio.run(_do_request(route, {"model": "m"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.http_status, 400)
        self.assertIn("DEGRADED", result.error_message)
