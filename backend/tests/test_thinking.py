"""思考强度参数的解析、归一化与透传。"""
import json
from unittest.mock import patch

import httpx
from django.test import RequestFactory, TestCase, TransactionTestCase

from apps.core.models import AIModel, Channel, ChannelKey, SystemSetting
from services import api_key_service, channel_service, race_engine, thinking
from services.thinking import ThinkingSpec, parse, to_upstream


def set_setting(key: str, value: str):
    """写到默认渠道上——运行时参数是按渠道隔离的。"""
    SystemSetting.objects.update_or_create(
        channel=channel_service.ensure_default_channel(), key=key,
        defaults={"value": value})


class ParseTests(TestCase):
    def test_no_intent(self):
        spec = parse({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertFalse(spec.is_set())
        self.assertEqual(to_upstream(spec, "any/model"), {})

    def test_top_level_thinking_bool(self):
        spec = parse({"thinking": True})
        self.assertTrue(spec.enabled)
        out = to_upstream(spec, "any/model")  # 未命中知识库 -> 通用默认：双开关
        self.assertEqual(
            out["chat_template_kwargs"], {"thinking": True, "enable_thinking": True},
        )

    def test_deepseek_uses_thinking_only(self):
        # DeepSeek 族只认 chat_template_kwargs.thinking（不认 enable_thinking）
        spec = parse({"thinking": True})
        out = to_upstream(spec, "deepseek-ai/deepseek-v4-pro-0813")
        self.assertEqual(out["chat_template_kwargs"], {"thinking": True})
        self.assertNotIn("enable_thinking", out["chat_template_kwargs"])

    def test_qwen_uses_enable_thinking_only(self):
        spec = parse({"enable_thinking": False})
        out = to_upstream(spec, "qwen/qwen3-235b-a22b")
        self.assertEqual(out["chat_template_kwargs"], {"enable_thinking": False})

    def test_kimi_k3_never_sends_thinking(self):
        # K3 始终思考、仅认顶层 reasoning_effort；传 thinking 会报错
        spec = parse({"reasoning_effort": "high"})
        out = to_upstream(spec, "moonshotai/kimi-k3")
        self.assertNotIn("chat_template_kwargs", out)
        self.assertNotIn("thinking", out)
        self.assertEqual(out["reasoning_effort"], "high")

    def test_kimi_k2_uses_thinking_type(self):
        spec = parse({"thinking": False})
        out = to_upstream(spec, "moonshotai/kimi-k2.6")
        self.assertEqual(out["thinking"], {"type": "disabled"})

    def test_always_on_model_ignores_disable(self):
        spec = parse({"thinking": False})
        out = to_upstream(spec, "deepseek-ai/deepseek-r1")
        # 常开模型：不发送关闭开关，也不发档位
        self.assertEqual(out, {})

    def test_effort_implied_enabled_on_deepseek(self):
        spec = parse({"reasoning_effort": "high"})
        self.assertTrue(spec.enabled)
        out = to_upstream(spec, "deepseek-ai/deepseek-v4-pro")
        self.assertEqual(out["reasoning_effort"], "high")
        self.assertTrue(out["chat_template_kwargs"]["thinking"])

    def test_effort_clamped_to_model_supported(self):
        # DeepSeek 只支持 high/max：客户端 low 提到 high，max 保持
        out_low = to_upstream(parse({"reasoning_effort": "low"}),
                              "deepseek-ai/deepseek-v4-flash-0731")
        self.assertEqual(out_low["reasoning_effort"], "high")
        out_max = to_upstream(parse({"reasoning_effort": "max"}),
                              "deepseek-ai/deepseek-v4-flash-0731")
        self.assertEqual(out_max["reasoning_effort"], "max")
        # GLM 支持 low/high/max：medium 就近落到 high
        out_med = to_upstream(parse({"reasoning_effort": "medium"}),
                              "z-ai/glm-5.1")
        self.assertEqual(out_med["reasoning_effort"], "high")

    def test_effort_aliases(self):
        cases = {
            "xhigh": "max", "maximum": "max", "ultra": "max", "max": "max",
            "minimal": "low", "auto": "low", "low": "low",
            "balanced": "medium", "medium": "medium",
            "high": "high",
        }
        for raw, expected in cases.items():
            self.assertEqual(parse({"reasoning_effort": raw}).effort, expected, raw)

    def test_effort_off_disables(self):
        spec = parse({"reasoning_effort": "none"})
        self.assertFalse(spec.enabled)
        self.assertIsNone(spec.effort)
        out = to_upstream(spec, "deepseek-ai/deepseek-v4-pro")
        self.assertNotIn("reasoning_effort", out)
        self.assertFalse(out["chat_template_kwargs"]["thinking"])

    def test_unknown_effort_passes_through(self):
        # 中转层不替上游判定档位合法性，识别不了的写法原样下发
        self.assertEqual(parse({"reasoning_effort": "Turbo"}).effort, "turbo")

    def test_effort_numeric(self):
        self.assertEqual(parse({"reasoning_effort": 1}).effort, "low")
        self.assertEqual(parse({"reasoning_effort": 3}).effort, "high")
        self.assertEqual(parse({"reasoning_effort": 9}).effort, "max")
        self.assertFalse(parse({"reasoning_effort": 0}).enabled)

    def test_explicit_switch_beats_effort_off(self):
        spec = parse({"thinking": True, "reasoning_effort": "off"})
        self.assertTrue(spec.enabled)

    def test_budget(self):
        spec = parse({"reasoning_budget": 16384})
        self.assertEqual(spec.budget, 16384)
        self.assertTrue(spec.enabled)
        self.assertEqual(to_upstream(spec, "x/y")["reasoning_budget"], 16384)

    def test_thinking_budget_alias(self):
        self.assertEqual(parse({"thinking_budget": "4096"}).budget, 4096)

    def test_chat_template_kwargs_known_keys_are_normalized(self):
        spec = parse({"chat_template_kwargs": {"thinking": True, "reasoning_effort": "max"}})
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.effort, "max")
        out = to_upstream(spec, "deepseek-ai/deepseek-v4-pro")
        self.assertEqual(out["reasoning_effort"], "max")
        self.assertTrue(out["chat_template_kwargs"]["thinking"])

    def test_chat_template_kwargs_unknown_keys_pass_through(self):
        spec = parse({"chat_template_kwargs": {"clear_thinking": False, "custom_x": 1}})
        self.assertEqual(spec.template_kwargs, {"clear_thinking": False, "custom_x": 1})
        self.assertEqual(
            to_upstream(spec, "z-ai/glm-5.1")["chat_template_kwargs"]["custom_x"], 1)

    def test_clear_thinking_top_level(self):
        spec = parse({"clear_thinking": True})
        self.assertTrue(spec.template_kwargs["clear_thinking"])

    def test_extra_body_is_flattened(self):
        spec = parse({"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}})
        self.assertTrue(spec.enabled)

    def test_top_level_wins_over_extra_body(self):
        spec = parse({"reasoning_effort": "low",
                      "extra_body": {"reasoning_effort": "high"}})
        self.assertEqual(spec.effort, "low")

    def test_junk_payload_is_safe(self):
        self.assertFalse(parse(None).is_set())
        self.assertFalse(parse("nonsense").is_set())
        self.assertFalse(parse({"thinking": "maybe"}).is_set())


class UpstreamGateTests(TestCase):
    def test_passthrough_disabled(self):
        set_setting("thinking_passthrough", "false")
        spec = parse({"reasoning_effort": "high"})
        self.assertEqual(to_upstream(spec, "deepseek-ai/deepseek-r1"), {})

    def test_passthrough_enabled_by_default(self):
        spec = ThinkingSpec(enabled=True)
        self.assertIn("chat_template_kwargs", to_upstream(spec, "any/model"))

    def test_strip_models(self):
        set_setting("thinking_strip_models", "mistral-large, stepfun")
        spec = parse({"reasoning_effort": "high"})
        self.assertEqual(to_upstream(spec, "mistralai/mistral-large-3"), {})
        self.assertEqual(to_upstream(spec, "stepfun/step-3"), {})
        self.assertIn("reasoning_effort", to_upstream(spec, "deepseek-ai/deepseek-r1"))

    def test_strip_list_is_case_insensitive(self):
        set_setting("thinking_strip_models", "Mistral")
        self.assertEqual(to_upstream(ThinkingSpec(enabled=True), "mistralai/x"), {})


class ViewIntegrationTests(TestCase):
    def test_openai_view_builds_upstream_body(self):
        from api.openai_views import _build_upstream_body

        body = {
            "model": "deepseek-ai/deepseek-v4-pro-0813",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
            "stream": True,
            "reasoning_effort": "high",
            "chat_template_kwargs": {"thinking": True},
            "bogus_param": 1,
        }
        out = _build_upstream_body(body, body["model"])
        self.assertEqual(out["model"], "deepseek-ai/deepseek-v4-pro-0813")
        self.assertEqual(out["temperature"], 0.5)
        self.assertTrue(out["stream"])
        self.assertNotIn("bogus_param", out)
        self.assertEqual(out["reasoning_effort"], "high")
        # DeepSeek 族：只发 thinking 开关，避免 enable_thinking 这种非法关键字
        self.assertTrue(out["chat_template_kwargs"]["thinking"])
        self.assertNotIn("enable_thinking", out["chat_template_kwargs"])

    def test_openai_view_without_thinking_adds_nothing(self):
        from api.openai_views import _build_upstream_body

        out = _build_upstream_body(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "m")
        self.assertEqual(sorted(out), ["messages", "model"])

    def test_thinking_keys_are_not_double_passed(self):
        from api.openai_views import _build_upstream_body

        out = _build_upstream_body(
            {"model": "m", "messages": [], "thinking": True}, "m")
        self.assertNotIn("extra_body", out)
        self.assertNotIn("thinking_budget", out)
        self.assertEqual(out["chat_template_kwargs"]["thinking"], True)


class UpstreamWireTests(TransactionTestCase):
    """端到端：参数必须真的出现在发往上游的 HTTP 请求体里。

    用 TransactionTestCase 而非 TestCase：竞速跑在 asyncio 事件循环里，
    Django 的 DB 连接是按 task 隔离的，看不到 TestCase 未提交事务中的数据。
    """

    def _call_upstream(self, client_body: dict) -> dict:
        captured: dict = {}

        def handler(request: httpx.Request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        model_name = "deepseek-ai/deepseek-v4-pro-0813"
        channel = Channel.objects.create(
            name="Test", slug="test", base_url="https://upstream.test/v1")
        ChannelKey.objects.create(channel=channel, name="k1", api_key="nvapi-test")
        AIModel.objects.create(channel=channel, model_name=model_name, enabled=True)
        _user, raw_key = api_key_service.create_key("tester")

        orig_kwargs = race_engine._client_kwargs

        def patched(route, stream):
            kwargs = orig_kwargs(route, stream)
            kwargs["transport"] = httpx.MockTransport(handler)
            return kwargs

        from api import openai_views

        request = RequestFactory().post(
            "/v1/chat/completions",
            data=json.dumps(client_body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw_key}",
        )
        with patch.object(race_engine, "_client_kwargs", patched), \
             patch.object(openai_views, "_finish_log"):
            response = openai_views.chat_completions(request)
        self.assertEqual(response.status_code, 200)
        return captured.get("body", {})

    def test_reasoning_effort_reaches_upstream(self):
        body = self._call_upstream({
            "model": "deepseek-ai/deepseek-v4-pro-0813",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        })
        self.assertEqual(body["reasoning_effort"], "high")
        # DeepSeek 族只发 thinking 开关，不发 enable_thinking（避免非法关键字）
        self.assertTrue(body["chat_template_kwargs"]["thinking"])
        self.assertNotIn("enable_thinking", body["chat_template_kwargs"])

    def test_extra_body_chat_template_kwargs_reaches_upstream(self):
        body = self._call_upstream({
            "model": "deepseek-ai/deepseek-v4-pro-0813",
            "messages": [{"role": "user", "content": "hi"}],
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True},
                           "reasoning_budget": 8192},
        })
        # 客户端用 enable_thinking 表达意图，DeepSeek 族转译为 thinking
        self.assertTrue(body["chat_template_kwargs"]["thinking"])
        self.assertNotIn("enable_thinking", body["chat_template_kwargs"])
        self.assertEqual(body["reasoning_budget"], 8192)

    def test_unknown_params_still_stripped(self):
        body = self._call_upstream({
            "model": "deepseek-ai/deepseek-v4-pro-0813",
            "messages": [{"role": "user", "content": "hi"}],
            "bogus_param": 1,
        })
        self.assertNotIn("bogus_param", body)
        self.assertNotIn("chat_template_kwargs", body)
