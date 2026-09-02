"""测试思考参数透传修复：kilo/openrouter/muse-spark/agent 框架。"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from services import thinking
from services.reasoning_decrypt import decrypt_chat_delta, decrypt_sse_chunk, normalize_reasoning_format


def make_channel(host: str = "api.kilo.ai"):
    """创建一个模拟渠道，用于测试。"""
    ch = MagicMock()
    ch.base_url = f"https://{host}/v1"
    ch.auth_scheme = "bearer"
    ch.default_rpm = 40
    ch.disable_key_invalid = False
    ch.disable_proxy_unhealthy = False
    return ch


class KiloOpenRouterReasoningTests(TestCase):
    """测试 Kilo/OpenRouter 网关的思考参数透传（RikkaHub 风格）。"""

    def test_kilo_reasoning_effort_high(self):
        """Kilo 渠道：reasoning_effort=high 应该转为 reasoning: {effort: "high"}"""
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=False):
            ch = make_channel("api.kilo.ai")
            payload = {"reasoning_effort": "high", "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "test-model", ch)
        self.assertEqual(out.get("reasoning"), {"effort": "high"})

    def test_openrouter_reasoning_effort_max(self):
        """OpenRouter 渠道：reasoning_effort=max 应该转为 reasoning: {effort: "max"}"""
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=False):
            ch = make_channel("openrouter.ai")
            payload = {"reasoning_effort": "max", "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "test-model", ch)
        self.assertEqual(out.get("reasoning"), {"effort": "max"})

    def test_kilo_reasoning_effort_off(self):
        """Kilo 渠道：reasoning_effort=off 应该转为 reasoning: {effort: "none"}"""
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=False):
            ch = make_channel("api.kilo.ai")
            payload = {"reasoning_effort": "none", "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "test-model", ch)
        self.assertEqual(out.get("reasoning"), {"effort": "none"})

    def test_openrouter_raw_reasoning_object(self):
        """OpenRouter 渠道：原始 reasoning 对象应该被保留"""
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=False):
            ch = make_channel("openrouter.ai")
            payload = {"reasoning": {"effort": "high"}, "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "test-model", ch)
        self.assertEqual(out.get("reasoning"), {"effort": "high"})

    def test_kilo_with_budget(self):
        """Kilo 渠道：reasoning 带 budget_tokens"""
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=False):
            ch = make_channel("api.kilo.ai")
            payload = {
                "reasoning_effort": "high",
                "reasoning_budget": 8192,
                "model": "test",
                "messages": [],
            }
            out = thinking.build_upstream(payload, "muse-spark-1.2", ch)
        self.assertEqual(out.get("reasoning", {}).get("effort"), "high")
        self.assertEqual(out.get("reasoning", {}).get("budget_tokens"), 8192)

    def test_non_gateway_channel_uses_chat_template(self):
        """非网关渠道：应该使用 chat_template_kwargs 格式（非 always_on 模型）"""
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=False):
            ch = make_channel("integrate.api.nvidia.com")
            # 使用非 always_on 的模型（如 deepseek 非 r1）
            payload = {"reasoning_effort": "high", "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "deepseek-ai/deepseek-v4-pro", ch)
        # NVIDIA 非 r1 模型应该用 chat_template_kwargs 格式
        self.assertIn("chat_template_kwargs", out)
        self.assertIn("reasoning_effort", out)

    def test_nvidia_r1_model_uses_reasoning_effort_only(self):
        """NVIDIA deepseek-r1：always_on 模型只输出 reasoning_effort"""
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=False):
            ch = make_channel("integrate.api.nvidia.com")
            payload = {"reasoning_effort": "high", "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "deepseek-ai/deepseek-r1", ch)
        # deepseek-r1 是 always_on 模型，只输出 reasoning_effort
        self.assertIn("reasoning_effort", out)


class AgentFrameworkThinkingTests(TestCase):
    """测试各种 agent 框架的思考参数提取。"""

    def test_claude_betas_thinking(self):
        """Claude：betas: ["thinking-2024-01-01"] 应该被识别为思考开启"""
        payload = {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "betas": ["thinking-2024-01-01", "prompt-caching-2024-07-31"],
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)

    def test_claude_extra_body_thinking(self):
        """Claude：extra_body 中的 thinking 应该被提取"""
        payload = {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 8000}},
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.budget, 8000)

    def test_codex_reasoning_effort(self):
        """Codex：reasoning_effort 应该被识别"""
        payload = {
            "model": "o1-preview",
            "messages": [],
            "reasoning_effort": "high",
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.effort, "high")

    def test_dsh_nested_reasoning(self):
        """DSH：extra_body.openai.reasoning_effort 嵌套格式"""
        payload = {
            "model": "gpt-4",
            "messages": [],
            "extra_body": {"openai": {"reasoning_effort": "medium"}},
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.effort, "medium")

    def test_zcode_thinking_budget(self):
        """zcode：thinking_budget 应该被识别"""
        payload = {
            "model": "gpt-4",
            "messages": [],
            "thinking_budget": 16384,
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.budget, 16384)

    def test_grok_build_thinking(self):
        """Grok build：thinking 应该被识别（grok 常开模型）"""
        payload = {
            "model": "grok-4",
            "messages": [],
            "thinking": True,
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)

    def test_numeric_effort_values(self):
        """数值格式 effort：1=low, 2=medium, 3=high, 9=max"""
        for val, expected in [(1, "low"), (2, "medium"), (3, "high"), (9, "max"), (0, "off")]:
            payload = {"reasoning_effort": val, "model": "test", "messages": []}
            spec = thinking.parse(payload)
            if val == 0:
                self.assertFalse(spec.enabled)
            else:
                self.assertEqual(spec.effort, expected, f"val={val}")

    def test_reasoning_dict_effort(self):
        """reasoning: {effort: "high"} 格式"""
        payload = {
            "model": "test",
            "messages": [],
            "reasoning": {"effort": "high"},
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.effort, "high")
        self.assertIsNotNone(spec.raw_reasoning)

    def test_reasoning_dict_enabled(self):
        """reasoning: {enabled: True} 格式"""
        payload = {
            "model": "test",
            "messages": [],
            "reasoning": {"enabled": True},
        }
        spec = thinking.parse(payload)
        self.assertTrue(spec.enabled)


class MuseSparkDecryptionTests(TestCase):
    """测试 muse-spark 模型的思考链解密。"""

    def test_decrypt_chat_delta_reasoning_content(self):
        """解密 delta.reasoning_content"""
        delta = {"reasoning_content": "gAAAAtest_encrypted"}
        with patch("services.reasoning_decrypt.decrypt_token", return_value="decrypted thinking"):
            changed = decrypt_chat_delta(delta)
        self.assertTrue(changed)
        self.assertEqual(delta["reasoning_content"], "decrypted thinking")

    def test_decrypt_chat_delta_reasoning_string(self):
        """解密 delta.reasoning（字符串格式）"""
        delta = {"reasoning": "gAAAAtest_encrypted"}
        with patch("services.reasoning_decrypt.decrypt_token", return_value="decrypted"):
            changed = decrypt_chat_delta(delta)
        self.assertTrue(changed)
        self.assertEqual(delta["reasoning"], "decrypted")

    def test_decrypt_chat_delta_reasoning_nested(self):
        """解密 delta.reasoning（嵌套字典格式）"""
        delta = {"reasoning": {"effort": "gAAAAtest", "other": "keep"}}
        with patch("services.reasoning_decrypt.decrypt_token", return_value="decrypted"):
            changed = decrypt_chat_delta(delta)
        self.assertTrue(changed)
        self.assertEqual(delta["reasoning"]["effort"], "decrypted")
        self.assertEqual(delta["reasoning"]["other"], "keep")

    def test_decrypt_sse_chunk_reasoning_content(self):
        """解密 SSE chunk 中的 reasoning_content"""
        chunk = 'data: {"choices": [{"delta": {"reasoning_content": "gAAAAtest"}}]}'
        with patch("services.reasoning_decrypt.decrypt_token", return_value="decrypted"):
            result = decrypt_sse_chunk(chunk)
        self.assertIn("decrypted", result)
        self.assertNotIn("gAAAAtest", result)

    def test_decrypt_sse_chunk_reasoning_field(self):
        """解密 SSE chunk 中的 reasoning 字段"""
        chunk = 'data: {"choices": [{"delta": {"reasoning": "gAAAAtest"}}]}'
        with patch("services.reasoning_decrypt.decrypt_token", return_value="decrypted"):
            result = decrypt_sse_chunk(chunk)
        self.assertIn("decrypted", result)

    def test_decrypt_sse_chunk_done_unchanged(self):
        """[DONE] chunk 不应该被修改"""
        chunk = "data: [DONE]\n\n"
        result = decrypt_sse_chunk(chunk)
        self.assertEqual(result, chunk)

    def test_decrypt_sse_chunk_non_data_unchanged(self):
        """非 data: 开头的 chunk 不应该被修改"""
        chunk = ": keep-alive\n\n"
        result = decrypt_sse_chunk(chunk)
        self.assertEqual(result, chunk)


class NormalizeReasoningFormatTests(TestCase):
    """测试 Kilo/OpenRouter 渠道的思考内容格式归一化。"""

    def test_normalize_delta_reasoning_to_reasoning_content(self):
        """delta.reasoning 应该被转换为 delta.reasoning_content"""
        data = {"choices": [{"delta": {"reasoning": "思考内容..."}}]}
        changed = normalize_reasoning_format(data)
        self.assertTrue(changed)
        self.assertIn("reasoning_content", data["choices"][0]["delta"])
        self.assertNotIn("reasoning", data["choices"][0]["delta"])
        self.assertEqual(data["choices"][0]["delta"]["reasoning_content"], "思考内容...")

    def test_normalize_delta_reasoning_dict_to_reasoning_content(self):
        """delta.reasoning 嵌套字典格式应该被转换"""
        data = {"choices": [{"delta": {"reasoning": {"text": "思考内容..."}}}]}
        changed = normalize_reasoning_format(data)
        self.assertTrue(changed)
        self.assertIn("reasoning_content", data["choices"][0]["delta"])
        self.assertEqual(data["choices"][0]["delta"]["reasoning_content"], "思考内容...")

    def test_normalize_delta_reasoning_effort_to_reasoning_content(self):
        """delta.reasoning 嵌套 effort 格式应该被转换"""
        data = {"choices": [{"delta": {"reasoning": {"effort": "思考内容..."}}}]}
        changed = normalize_reasoning_format(data)
        self.assertTrue(changed)
        self.assertIn("reasoning_content", data["choices"][0]["delta"])
        self.assertEqual(data["choices"][0]["delta"]["reasoning_content"], "思考内容...")

    def test_no_normalize_when_reasoning_content_exists(self):
        """当 reasoning_content 已存在时不应该转换"""
        data = {"choices": [{"delta": {"reasoning_content": "已有内容..."}}]}
        changed = normalize_reasoning_format(data)
        self.assertFalse(changed)
        self.assertEqual(data["choices"][0]["delta"]["reasoning_content"], "已有内容...")

    def test_normalize_sse_chunk_kilo_openrouter(self):
        """Kilo/OpenRouter 渠道的 SSE chunk 应该被归一化"""
        chunk = 'data: {"choices": [{"delta": {"reasoning": "思考内容..."}}]}'
        result = decrypt_sse_chunk(chunk)
        self.assertIn("reasoning_content", result)
        self.assertNotIn('"reasoning":', result)
        self.assertEqual(result, 'data: {"choices": [{"delta": {"reasoning_content": "思考内容..."}}]}\n')

    def test_normalize_sse_chunk_with_encrypted_reasoning(self):
        """加密的 reasoning 应该先解密再归一化"""
        chunk = 'data: {"choices": [{"delta": {"reasoning": "gAAAA加密内容"}}]}'
        with patch("services.reasoning_decrypt.decrypt_token", return_value="解密后的思考"):
            result = decrypt_sse_chunk(chunk)
        self.assertIn("reasoning_content", result)
        self.assertIn("解密后的思考", result)

    def test_normalize_multiple_choices(self):
        """多个 choices 都应该被处理"""
        data = {"choices": [
            {"delta": {"reasoning": "思考1"}},
            {"delta": {"reasoning": "思考2"}},
        ]}
        changed = normalize_reasoning_format(data)
        self.assertTrue(changed)
        self.assertEqual(data["choices"][0]["delta"]["reasoning_content"], "思考1")
        self.assertEqual(data["choices"][1]["delta"]["reasoning_content"], "思考2")


class ExtraBodyFlattenTests(TestCase):
    """测试 extra_body 嵌套展开。"""

    def test_flatten_extra_body_openai(self):
        """extra_body.openai.reasoning_effort 应该被展开"""
        payload = {
            "extra_body": {"openai": {"reasoning_effort": "high"}},
        }
        src = thinking._flatten(payload)
        self.assertIn("reasoning_effort", src)
        self.assertEqual(src["reasoning_effort"], "high")

    def test_flatten_extra_body_deep_nesting(self):
        """多层嵌套应该被展开"""
        payload = {
            "extra_body": {
                "openai": {
                    "reasoning": {"effort": "max"}
                }
            }
        }
        src = thinking._flatten(payload)
        self.assertIn("reasoning", src)

    def test_flatten_betas_thinking(self):
        """betas 数组中的 thinking 应该被识别"""
        payload = {
            "betas": ["thinking-2024-01-01"],
        }
        src = thinking._flatten(payload)
        self.assertIn("enabled", src)
        self.assertTrue(src["enabled"])


class PassthroughGateTests(TestCase):
    """测试思考参数透传开关。"""

    def test_passthrough_disabled(self):
        """透传关闭时不输出任何思考参数"""
        with patch("services.thinking._passthrough_enabled", return_value=False):
            payload = {"reasoning_effort": "high", "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "test", make_channel())
        self.assertEqual(out, {})

    def test_strip_models(self):
        """strip_models 列表中的模型不输出思考参数"""
        ch = make_channel("integrate.api.nvidia.com")
        with patch("services.thinking._passthrough_enabled", return_value=True), \
             patch("services.thinking._is_stripped", return_value=True):
            payload = {"reasoning_effort": "high", "model": "test", "messages": []}
            out = thinking.build_upstream(payload, "stripped-model", ch)
        self.assertEqual(out, {})
