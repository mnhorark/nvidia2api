"""第二轮全面复审（19 项发现）修复的回归锚定。

覆盖：竞速 prelude 重放、并行工具槽位分配、refusal.delta 翻译、
双出口终结守卫、工具块重入无重复 stop、入口多模态/思考历史保真、
extra_body 平铺、tool_stream usage 双计。
"""
import asyncio
import json

from django.test import TestCase

from services import anthropic_api, responses_api


async def _achunks(gen):
    for c in gen:
        yield c


def _collect(agen) -> list:
    async def _gather():
        return [part async for part in agen]
    return asyncio.run(_gather())


def _events(lines, name: str) -> list[dict]:
    """抽取指定 event 名的 data 载荷（兼容事件串列表与 splitlines 行列表）。"""
    out = []
    prefix = f"event: {name}"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            _, _, rest = line.partition("\n")
            if rest.startswith("data: "):
                out.append(json.loads(rest.split("data: ", 1)[1].strip()))
                continue
            for j in (i + 1, i + 2):
                if j < len(lines) and lines[j].startswith("data: "):
                    out.append(json.loads(lines[j].split("data: ", 1)[1].strip()))
                    break
    return out


class ReviewFixRegressionTests(TestCase):
    """零丢失复审第二轮的修复锚定。"""

    def test_prelude_usage_frame_replayed(self):
        """竞速 prelude：判胜首帧前的 usage 预告帧/注释行不蒸发。"""
        from services.race_engine import StreamWinner
        from types import SimpleNamespace

        async def aiter():
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"
            yield ""

        route_ns = SimpleNamespace(
            key=SimpleNamespace(channel=None), url_override=None, proxy=None)
        w = StreamWinner(
            route=route_ns,
            cm=None, req_cm=None, aiter=aiter(),
            first_line='data: {"choices":[{"delta":{"role":"assistant"}}]}',
            prelude=['data: {"choices":[],"usage":{"prompt_tokens":9}}',
                     ": ping"])

        async def collect():
            return [c async for c in w.lines()]

        wire = "".join(asyncio.run(collect()))
        # prelude 字节原样重放（JSON 无空格序列化，键在即零丢失）
        self.assertIn('"prompt_tokens":9', wire)
        self.assertIn(": ping", wire)
        self.assertIn('"content":"hi"', wire)

    def test_parallel_function_calls_get_distinct_slots(self):
        """Responses 上游并行工具调用：翻译后各占一个 tool index，不互相拼接。"""
        state = {}
        out1 = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.output_item.added", "output_index": 0,
            "item": {"id": "fc_a", "type": "function_call", "call_id": "call_a",
                     "name": "fa", "arguments": ""}}), state))
        out2 = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.output_item.added", "output_index": 1,
            "item": {"id": "fc_b", "type": "function_call", "call_id": "call_b",
                     "name": "fb", "arguments": ""}}), state))
        self.assertEqual(out1["choices"][0]["delta"]["tool_calls"][0]["index"], 0)
        self.assertEqual(out2["choices"][0]["delta"]["tool_calls"][0]["index"], 1)
        d1 = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.function_call_arguments.delta",
            "item_id": "call_a", "delta": '{"a":1}'}), state))
        d2 = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.function_call_arguments.delta",
            "item_id": "call_b", "delta": '{"b":2}'}), state))
        self.assertEqual(d1["choices"][0]["delta"]["tool_calls"][0]["index"], 0)
        self.assertEqual(d2["choices"][0]["delta"]["tool_calls"][0]["index"], 1)

    def test_refusal_delta_translated_to_chat(self):
        """response.refusal.delta 翻译为 chat refusal 字段，不丢弃。"""
        out = json.loads(responses_api._translate_event("data: " + json.dumps({
            "type": "response.refusal.delta", "delta": "拒绝文本"}), {}))
        self.assertEqual(out["choices"][0]["delta"]["refusal"], "拒绝文本")

    def test_responses_exit_finish_guard(self):
        """出口终结守卫：重复 finish 帧不重发 done 组；finish 后增量原样透传。"""
        chunks = [
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"content": "hi"}, "finish_reason": None}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {}, "finish_reason": "stop"}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"content": "late"}, "finish_reason": None}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {}, "finish_reason": "stop"}]}) + "\n\n",
            "data: [DONE]\n\n",
        ]
        out = "".join(_collect(responses_api.iter_chat_sse_as_responses(
            _achunks(iter(chunks)))))
        done_events = out.count('"type": "response.output_item.done"')
        # msg_0 的 output_item.done 1 次（重复 finish 不重发）
        self.assertEqual(done_events, 1)
        # finish 后的迟到增量原样透传（零丢失），不再翻译成 delta 事件
        self.assertIn('"content": "late"', out)
        completed = _events(out.splitlines(), "response.completed")
        self.assertEqual(len(completed), 1)

    def test_anthropic_exit_finish_guard_no_double_stop(self):
        """anthropic 出口：重复 finish 帧不重复关块、不重复 message_delta。"""
        chunks = [
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"content": "hi"}, "finish_reason": None}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {}, "finish_reason": "stop"}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {}, "finish_reason": "stop"}]}) + "\n\n",
            "data: [DONE]\n\n",
        ]
        out = _collect(anthropic_api.iter_chat_sse_as_anthropic(
            _achunks(iter(chunks))))
        stops = [l for l in out if l.startswith("event: content_block_stop")]
        deltas = [l for l in out if l.startswith("event: message_delta")]
        self.assertEqual(len(stops), 1)
        self.assertEqual(len(deltas), 1)

    def test_anthropic_tool_text_interleave_no_double_stop(self):
        """工具块重入：text 插话后工具参数续帧不再产生重复 content_block_stop。"""
        chunks = [
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"tool_calls": [{"index": 0, "id": "t1",
                                           "function": {"name": "f", "arguments": '{"x":'}}]}}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"content": "插话"}}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"tool_calls": [{"index": 0,
                                           "function": {"name": None, "arguments": "1}"}}]}}]}) + "\n\n",
            "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {}, "finish_reason": "tool_calls"}]}) + "\n\n",
            "data: [DONE]\n\n",
        ]
        out = _collect(anthropic_api.iter_chat_sse_as_anthropic(
            _achunks(iter(chunks))))
        stops = [l for l in out if l.startswith("event: content_block_stop")]
        # 工具块 stop（插话开块时关）+ 插话 text 块 stop（finish 时关），无重复
        self.assertEqual(len(stops), 2)
        args = "".join(json.loads(l.split("data: ", 1)[1])["delta"]["partial_json"]
                       for l in out
                       if l.startswith("event: content_block_delta")
                       and "input_json_delta" in l)
        self.assertEqual(args, '{"x":1}')  # 参数零丢失

    def test_input_file_audio_refusal_parts_survive(self):
        """Responses 入口：input_file/input_audio/refusal 分片不蒸发。"""
        chat = responses_api.responses_to_chat_body({
            "model": "m",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "看这个"},
                {"type": "input_file", "filename": "a.pdf", "file_data": "data:..."},
                {"type": "input_audio", "input_audio": {"data": "xx", "format": "wav"}},
                {"type": "refusal", "refusal": "历史拒绝"},
            ]}]})
        parts = chat["messages"][0]["content"]
        types = [p.get("type") for p in parts]
        self.assertEqual(types, ["text", "file", "input_audio", "text"])
        self.assertEqual(parts[3]["text"], "历史拒绝")

    def test_anthropic_thinking_blocks_kept_in_history(self):
        """Anthropic 入口：thinking/redacted_thinking 历史块并入 reasoning_content。"""
        chat = anthropic_api.messages_to_chat_body({
            "model": "m", "max_tokens": 10,
            "messages": [{"role": "assistant", "content": [
                {"type": "thinking", "thinking": "上轮思考"},
                {"type": "text", "text": "上轮回答"},
            ]}]})
        # thinking 块提升到请求体级 reasoning_content（上游思考参数通道）
        self.assertEqual(chat["reasoning_content"], "上轮思考")
        self.assertEqual(chat["messages"][0]["content"], "上轮回答")

    def test_tool_choice_none_maps_to_none(self):
        """tool_choice type=none 不得兜底成 auto（反向违背禁工具意图）。"""
        chat = anthropic_api.messages_to_chat_body({
            "model": "m", "max_tokens": 10,
            "tool_choice": {"type": "none"}, "tools": []})
        self.assertEqual(chat["tool_choice"], "none")

    def test_extra_body_non_thinking_keys_flattened(self):
        """extra_body 平铺：非思考键保留（SDK 契约），思考键走归一化通道。"""
        from api.openai_views import _build_upstream_body
        body = {"model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "extra_body": {"top_k": 5, "custom_vendor_param": True,
                               "reasoning_effort": "low"}}
        up = _build_upstream_body(body, "real/m", None)
        self.assertEqual(up["top_k"], 5)
        self.assertIs(up["custom_vendor_param"], True)
        # 思考键：归一化产物覆盖平铺值（low 保留，不被全局默认顶掉）
        self.assertEqual(up.get("reasoning_effort"), "low")

    def test_tool_stream_split_usage_not_duplicated(self):
        """终结帧拆分：usage 只在追随帧，main 帧不双计。"""
        from services.tool_stream import ToolCallStreamNormalizer
        n = ToolCallStreamNormalizer()
        chunk = "data: " + json.dumps({"choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "id": "t", "function": {
                "name": "f", "arguments": '{"a":1}'}}]},
            "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3}}) + "\n\n"
        outs = n.feed(chunk)
        self.assertEqual(len(outs), 2)
        main = json.loads(outs[0][5:].strip())
        tail = json.loads(outs[1][5:].strip())
        self.assertNotIn("usage", main)
        self.assertEqual(tail["usage"]["prompt_tokens"], 3)
        self.assertEqual(tail["choices"][0]["finish_reason"], "tool_calls")

    def test_undecryptable_ciphertext_message_kept(self):
        """非流式 message：解不开的密文原样保留（不占位、不剥离）。"""
        from services.reasoning_decrypt import decrypt_chat_message
        blob = "43e3da0e" + "A" * 300
        msg = {"reasoning_content": blob, "content": "答"}
        decrypt_chat_message(msg)
        self.assertEqual(msg["reasoning_content"], blob)


class MessageShapeClampTests(TestCase):
    """消息形态钳制（AI SDK 方言 -> OpenAI 规范形态）。

    根因背景：zcode/keysmith（AI SDK 系）与对话页的请求差异不在思考
    参数，而在消息形态——role=tool 的 content 数组形态被多数上游强校验
    拒绝（string 类型校验），表现为 agent 请求全线路 400 而对话页成功。
    """

    def test_tool_content_array_clamped_to_string(self):
        from services.message_shape import clamp_message_shapes
        body = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1",
             "content": [{"type": "text", "text": "结果42"}]},
        ]}
        changed = clamp_message_shapes(body)
        self.assertEqual(changed, 1)
        self.assertEqual(body["messages"][2]["content"], "结果42")
        self.assertIsInstance(body["messages"][2]["content"], str)

    def test_tool_extra_keys_stripped(self):
        """role=tool 的规范外附加键（SDK 泄漏）剔除——上游强校验三键。"""
        from services.message_shape import clamp_message_shapes
        body = {"messages": [
            {"role": "tool", "tool_call_id": "t1", "content": "ok",
             "name": "f", "some_sdk_leak": {"x": 1}},
        ]}
        clamp_message_shapes(body)
        self.assertEqual(set(body["messages"][0].keys()),
                         {"role", "tool_call_id", "content"})

    def test_assistant_array_content_clamped(self):
        from services.message_shape import clamp_message_shapes
        body = {"messages": [
            {"role": "assistant",
             "content": [{"type": "text", "text": "上轮回答"}]},
        ]}
        clamp_message_shapes(body)
        self.assertEqual(body["messages"][0]["content"], "上轮回答")

    def test_multimodal_user_content_preserved(self):
        """user 消息含 image 块：数组形态合法，保留不收敛。"""
        from services.message_shape import clamp_message_shapes
        original = [
            {"type": "text", "text": "看"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
        ]
        body = {"messages": [{"role": "user", "content": original}]}
        clamp_message_shapes(body)
        self.assertEqual(body["messages"][0]["content"], original)

    def test_user_pure_text_array_clamped(self):
        from services.message_shape import clamp_message_shapes
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]}
        clamp_message_shapes(body)
        self.assertEqual(body["messages"][0]["content"], "ab")

    def test_assistant_reasoning_blocks_stripped_not_passthrough(self):
        """assistant 历史 reasoning 块剥离，不因它豁免收敛（req_155f7e78 案）。

        AI SDK 系客户端把上轮思考作为 {"type":"reasoning"} 块回传；该类型
        不在 OpenAI 规范词汇表里，严格上游整包 400
        （"messages[4].content[0].type类型错误"）。旧实现把 reasoning 块
        当"多模态"跳过收敛 → 方言原样透传。
        """
        from services.message_shape import clamp_message_shapes
        body = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "reasoning", "text": "[P] 思考中……"},
                {"type": "text", "text": "回答正文"},
            ]},
            {"role": "user", "content": "继续"},
        ]}
        changed = clamp_message_shapes(body)
        # 2 次：剥 reasoning 块 + 剩余文本块收敛为字符串（同一消息计两次）
        self.assertEqual(changed, 2)
        self.assertEqual(body["messages"][1]["content"], "回答正文")

    def test_assistant_pure_reasoning_array_collapses_to_empty(self):
        """纯 reasoning 数组：剥离后收敛为空字符串（不给上游留数组方言）。"""
        from services.message_shape import clamp_message_shapes
        body = {"messages": [
            {"role": "assistant", "content": [
                {"type": "reasoning", "text": "只有思考没有正文"},
            ]},
        ]}
        changed = clamp_message_shapes(body)
        # 2 次：剥 reasoning 块 + 空数组收敛为 ""（同一消息计两次）
        self.assertEqual(changed, 2)
        self.assertEqual(body["messages"][0]["content"], "")

    def test_multimodal_assistant_content_keeps_image_but_strips_reasoning(self):
        """assistant 数组混有 image 与 reasoning：剥 reasoning、保 image 数组。"""
        from services.message_shape import clamp_message_shapes
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "reasoning", "text": "想"},
            image,
        ]}]}
        clamp_message_shapes(body)
        self.assertEqual(body["messages"][0]["content"], [image])

    def test_request_summary_populated(self):
        """诊断摘要：tools 数量/工具名/改写映射入日志。"""
        from api.openai_views import _request_summary
        body = {"model": "m", "stream": True,
                "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "function", "function": {
                    "name": "mcp__serena__replace_symbol_body",
                    "parameters": {}}}],
                "tool_choice": "auto"}
        summary = _request_summary(body, {"fn_a": "very_long_name"})
        self.assertEqual(summary["tools_count"], 1)
        self.assertEqual(summary["tool_names"], ["mcp__serena__replace_symbol_body"])
        self.assertEqual(summary["top_keys"],
                         ["model", "stream", "tool_choice", "tools"])
        self.assertEqual(summary["tool_alias_rewritten"], 1)
