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
        with patch.object(admin_views, "_LOGIN_FAIL_SWEEP_THRESHOLD", 4):
            for i in range(6):
                self._login(xff=f"198.51.100.{i}")
            self.assertEqual(len(admin_views._login_fail_bucket), 6)
            # 时间越过窗口后，下一次失败会顺带清扫掉所有过期桶（只留本次的）
            with patch.object(admin_views.time, "monotonic",
                              return_value=admin_views.time.monotonic()
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
        with patch("services.crypto.Fernet") as fake:
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
