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
        # 2026-09 收紧：纯档位意图不再合成 thinking 开关——档位本身
        # 就是开启表达，凭空注入是发明意图（严格上游 400 风险面）
        self.assertEqual(out["reasoning_effort"], "high")
        self.assertNotIn("chat_template_kwargs", out)

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
        # 内部档位对齐 OpenRouter 完整梯度：
        # none < minimal < low < medium < high < xhigh < max
        # minimal/xhigh 是独立档位，不再折叠进 low/max（R14）
        cases = {
            "xhigh": "xhigh", "extra_high": "xhigh",
            "maximum": "max", "ultra": "max", "max": "max",
            "minimal": "minimal", "min": "minimal",
            "auto": "low", "low": "low",
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
    def setUp(self):
        from services import sysconfig
        sysconfig.invalidate()

    def tearDown(self):
        from services import sysconfig
        sysconfig.invalidate()

    def test_passthrough_disabled(self):
        set_setting("thinking_passthrough", "false")
        spec = parse({"reasoning_effort": "high"})
        self.assertEqual(to_upstream(spec, "deepseek-ai/deepseek-r1"), {})

    def test_passthrough_enabled_by_default(self):
        # 2026-09 收紧：推断态 enabled（无 explicit_toggle 标记）不再
        # 合成开关——on 意图由档位/预算字段本身表达。显式开关见
        # test_explicit_toggle_reaches_toggling_model。
        spec = ThinkingSpec(enabled=True)
        self.assertNotIn("chat_template_kwargs", to_upstream(spec, "any/model"))
        self.assertEqual(to_upstream(spec, "any/model")["reasoning_effort"], "high")

    def test_explicit_toggle_always_injected(self):
        """显式开关注入不受推断收窄影响（off 场景 + 显式 on 场景）。"""
        # off：推断 False 也注入（关不掉是真实故障）
        out = to_upstream(parse({"enable_thinking": False}), "qwen/qwen3.8-flash")
        self.assertEqual(out["chat_template_kwargs"], {"enable_thinking": False})
        # 显式 on：正常注入
        out = to_upstream(parse({"thinking": True}), "deepseek-ai/deepseek-v4-pro")
        self.assertEqual(out["chat_template_kwargs"], {"thinking": True})

    def test_explicit_toggle_reaches_toggling_model(self):
        """显式开关注入不受档位驱动收窄影响（客户端意图必须传递）。"""
        out = to_upstream(parse({"thinking": True}), "deepseek-ai/deepseek-v4-pro")
        self.assertEqual(out["chat_template_kwargs"], {"thinking": True})

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
        }
        out = _build_upstream_body(body, body["model"])
        self.assertEqual(out["model"], "deepseek-ai/deepseek-v4-pro-0813")
        self.assertEqual(out["temperature"], 0.5)
        self.assertTrue(out["stream"])
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
        # 2026-09 收紧：纯档位意图不合成 thinking 开关（发明意图修复）。
        # 显式开关仍正常传递（见 test_chat_template_kwargs_known_keys）。
        self.assertNotIn("chat_template_kwargs", body)

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

    def test_unknown_params_forwarded_losslessly(self):
        body = self._call_upstream({
            "model": "deepseek-ai/deepseek-v4-pro-0813",
            "messages": [{"role": "user", "content": "hi"}],
            "bogus_param": 1,
        })
        self.assertEqual(body.get("bogus_param"), 1)
        self.assertNotIn("chat_template_kwargs", body)


class R14_EffortVocabularyTests(TestCase):
    """R14：思考档位词汇表扩展（对齐 OpenRouter reasoning.effort 完整梯度）。

    调研结论（2026-09，openrouter.ai/docs）：
    - reasoning.effort 梯度: none / minimal / low / medium / high / xhigh / max
    - 各模型声明自己的 supported_efforts（GLM-5.3: low/high/max 恒开启；
      doubao 支持 minimal；Anthropic 经 output_config.effort 支持 xhigh/max）
    - Anthropic 风格走 budget_tokens，Gemini 风格走数值预算——
      档位与预算需要双向翻译，否则意图在转换中丢失
    """

    def test_numeric_effort_mapping_extended(self):
        # 0=off 1=low 2=medium 3=high 4=xhigh 5+=max
        from services.thinking import _normalize_effort
        self.assertEqual(_normalize_effort(0), "off")
        self.assertEqual(_normalize_effort(1), "low")
        self.assertEqual(_normalize_effort(2), "medium")
        self.assertEqual(_normalize_effort(3), "high")
        self.assertEqual(_normalize_effort(4), "xhigh")
        self.assertEqual(_normalize_effort(5), "max")
        self.assertEqual(_normalize_effort(9), "max")

    def test_minimal_xhigh_are_distinct_levels(self):
        spec_min = parse({"reasoning_effort": "minimal"})
        self.assertEqual(spec_min.effort, "minimal")
        self.assertTrue(spec_min.enabled)
        spec_xh = parse({"reasoning_effort": "xhigh"})
        self.assertEqual(spec_xh.effort, "xhigh")

    def test_clamp_order_and_tiebreak(self):
        from services.thinking import _clamp_effort
        # 修正后的强序: none<minimal<low<medium<high<xhigh<max
        # kimi-k3 只认 low/high/max：minimal 就近下落 low，xhigh 同距取更高档 max
        self.assertEqual(_clamp_effort("minimal", ("low", "high", "max")), "low")
        self.assertEqual(_clamp_effort("xhigh", ("low", "high", "max")), "max")
        self.assertEqual(_clamp_effort("minimal", ("low", "medium", "high")), "low")
        self.assertEqual(_clamp_effort("xhigh", ("minimal", "low", "medium", "high")), "high")
        # doubao 支持 minimal：原样保留
        self.assertEqual(_clamp_effort("minimal", ("minimal", "low", "medium", "high")), "minimal")

    def test_muse_effort_passthrough_direct(self):
        """muse 已移出网关分支（zen 对 reasoning 对象不生效，实测 kimi-k3
        同参直传生效）——muse 走普通路径 reasoning_effort 直传，且 cap
        放开为全档：minimal/xhigh 不被折叠。"""
        out = to_upstream(parse({"reasoning_effort": "minimal"}), "muse-spark-1.3-contributor-free")
        self.assertEqual(out["reasoning_effort"], "minimal")
        self.assertNotIn("reasoning", out)  # 不再发网关对象
        out = to_upstream(parse({"reasoning_effort": "xhigh"}), "muse-spark-1.3-contributor-free")
        self.assertEqual(out["reasoning_effort"], "xhigh")
        out = to_upstream(parse({"reasoning_effort": "high"}), "muse-spark-1.3-contributor-free")
        self.assertEqual(out["reasoning_effort"], "high")
        # 2026-09 收紧：档位意图不再经换算表合成 thinking_budget——
        # 凭空注入 32K 预算挤占模型上下文窗口（zcode muse 案），且是
        # 客户端未表达的意图。预算双通道仅在客户端显式给预算时生效。
        self.assertNotIn("chat_template_kwargs", out)

    def test_true_gateway_still_uses_reasoning_object(self):
        """真网关（openrouter host）仍走 reasoning 对象格式。"""
        from apps.core.models import Channel
        ch = Channel.objects.create(
            name="or-test", slug="or-test",
            base_url="https://openrouter.ai/api/v1")
        out = to_upstream(parse({"reasoning_effort": "minimal"}),
                          "some-model", ch)
        self.assertEqual(out["reasoning"]["effort"], "minimal")

    def test_effort_to_budget_synthesis_for_thinking_type(self):
        # Claude 风格 thinking 对象：客户端给档位没给预算 → 按档位表合成预算
        out = to_upstream(parse({"reasoning_effort": "high"}), "kimi-k2")
        self.assertEqual(out["thinking"]["type"], "enabled")
        self.assertEqual(out["thinking"]["budget_tokens"], 16384)
        out = to_upstream(parse({"reasoning_effort": "minimal"}), "kimi-k2")
        self.assertEqual(out["thinking"]["budget_tokens"], 1024)

    def test_budget_passthrough_for_thinking_type(self):
        # 客户端给了预算 → 原样进 thinking 对象，不覆盖
        out = to_upstream(parse({"thinking": {"type": "enabled", "budget_tokens": 5000}}), "kimi-k2")
        self.assertEqual(out["thinking"]["budget_tokens"], 5000)

    def test_budget_to_effort_fallback_when_no_budget_support(self):
        # 只认档位的渠道：预算意图回落为最近档位（不整段丢弃）
        from services.thinking import budget_to_effort
        self.assertEqual(budget_to_effort(500), "minimal")
        self.assertEqual(budget_to_effort(4096), "low")
        self.assertEqual(budget_to_effort(20000), "high")
        self.assertEqual(budget_to_effort(999999), "max")
        self.assertEqual(budget_to_effort(0), "off")

    def test_budget_effort_roundtrip_sanity(self):
        from services.thinking import budget_to_effort, effort_to_budget
        for eff in ("minimal", "low", "medium", "high", "xhigh", "max"):
            tok = effort_to_budget(eff)
            self.assertIsNotNone(tok, eff)
            self.assertEqual(budget_to_effort(tok), eff)


class Qwen38FlashVocabularyTests(TestCase):
    """qwen3.8-flash 官方档位约束回归（2026-09 线上 400 实证，
    req_68ec7675 案）。

    上游仅认 xhigh/medium/low（服务端默认 xhigh），且 reasoning_effort
    与 thinking_budget 互斥。旧实现：qwen capability 无词表（沿用通用
    low/medium/high/max），开关意图被注入默认档 high —— 不在词表，
    全线路整包 400。
    """

    MODEL = "qwen/qwen3.8-flash"

    def test_toggle_intent_does_not_invent_effort(self):
        # 客户端只开 enable_thinking：不得发明档位（上游默认即 xhigh）
        out = to_upstream(parse({"enable_thinking": True}), self.MODEL)
        self.assertEqual(out, {"chat_template_kwargs": {"enable_thinking": True}})
        out = to_upstream(parse({"enable_thinking": False}), self.MODEL)
        self.assertEqual(out, {"chat_template_kwargs": {"enable_thinking": False}})

    def test_effort_clamped_to_official_vocabulary(self):
        # max/high → xhigh（官方兼容映射）；minimal → low；合法值原样
        for src, want in [("max", "xhigh"), ("high", "xhigh"),
                          ("xhigh", "xhigh"), ("medium", "medium"),
                          ("low", "low"), ("minimal", "low")]:
            out = to_upstream(parse({"reasoning_effort": src}), self.MODEL)
            self.assertEqual(out.get("reasoning_effort"), want, src)
            # 互斥：effort 通道下发时不得再带 thinking_budget
            self.assertNotIn("thinking_budget",
                             out.get("chat_template_kwargs", {}), src)

    def test_budget_translates_via_official_tiers(self):
        # 官方区间语义：0-4096->low / 4097-16384->medium / 16385-262144->xhigh
        for budget, want in [(4096, "low"), (4097, "medium"),
                             (16384, "medium"), (16385, "xhigh"),
                             (262144, "xhigh"), (300000, "xhigh")]:
            out = to_upstream(parse({"reasoning_budget": budget}), self.MODEL)
            self.assertEqual(out.get("reasoning_effort"), want, budget)
            self.assertNotIn("reasoning_budget", out, budget)

    def test_older_qwen_models_keep_toggle_only(self):
        # 老 qwen 模型（词表未知）：维持开关直传，不注入档位
        out = to_upstream(parse({"enable_thinking": True}), "qwen/qwen3-235b-a22b")
        self.assertEqual(out, {"chat_template_kwargs": {"enable_thinking": True}})

    def test_effort_driven_models_keep_default_injection(self):
        # 档位驱动模型（kimi-k3）开关意图仍注入默认档——不受本次修复影响
        out = to_upstream(parse({"reasoning": True}), "moonshotai/kimi-k3")
        self.assertEqual(out.get("reasoning_effort"), "max")
