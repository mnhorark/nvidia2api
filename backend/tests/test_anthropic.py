"""Anthropic Messages API 双向转换测试。"""
import json

from django.test import TestCase

from services import anthropic_api


class RequestConversionTests(TestCase):
    def test_basic_messages_and_system(self):
        body = {
            "model": "m",
            "max_tokens": 1024,
            "system": "你是助手",
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
                {"role": "user", "content": [{"type": "text", "text": "再见"}]},
            ],
        }
        chat = anthropic_api.messages_to_chat_body(body)
        self.assertEqual(chat["model"], "m")
        self.assertEqual(chat["max_tokens"], 1024)
        self.assertNotIn("stop", chat)  # 未传 stop_sequences 时不带 stop
        self.assertEqual(chat["messages"][0], {"role": "system", "content": "你是助手"})
        self.assertEqual(chat["messages"][1], {"role": "user", "content": "你好"})
        self.assertEqual(chat["messages"][2], {"role": "assistant", "content": "你好！"})
        self.assertEqual(chat["messages"][3]["content"], "再见")

    def test_tool_use_and_result(self):
        body = {
            "model": "m",
            "max_tokens": 100,
            "tools": [{
                "name": "get_weather",
                "description": "查询天气",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }],
            "tool_choice": {"type": "tool", "name": "get_weather"},
            "messages": [
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1",
                     "content": "sunny"},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "get_weather",
                     "input": {"city": "北京"}},
                ]},
            ],
        }
        chat = anthropic_api.messages_to_chat_body(body)
        self.assertEqual(chat["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(chat["tools"][0]["function"]["parameters"]["type"], "object")
        self.assertEqual(chat["tool_choice"],
                         {"type": "function", "function": {"name": "get_weather"}})
        self.assertEqual(chat["messages"][0]["role"], "tool")
        self.assertEqual(chat["messages"][0]["tool_call_id"], "tu_1")
        self.assertEqual(chat["messages"][0]["content"], "sunny")
        self.assertEqual(chat["messages"][1]["role"], "assistant")
        self.assertEqual(chat["messages"][1]["tool_calls"][0]["function"]["name"],
                         "get_weather")
        args = json.loads(chat["messages"][1]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["city"], "北京")

    def test_thinking_mapping(self):
        body = {
            "model": "m",
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 512},
            "messages": [{"role": "user", "content": "hi"}],
        }
        chat = anthropic_api.messages_to_chat_body(body)
        self.assertIs(chat.get("thinking"), True)
        self.assertEqual(chat.get("reasoning_budget"), 512)

    def test_stop_sequences(self):
        chat = anthropic_api.messages_to_chat_body({
            "model": "m", "max_tokens": 1,
            "stop_sequences": ["\n\n", "END"],
            "messages": [{"role": "user", "content": "x"}],
        })
        self.assertEqual(chat["stop"], ["\n\n", "END"])


class ResponseConversionTests(TestCase):
    def test_text_and_reasoning(self):
        chat = {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "答案是 42",
                            "reasoning_content": "先思考"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        msg = anthropic_api.chat_to_messages_payload(chat)
        self.assertEqual(msg["type"], "message")
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["stop_reason"], "end_turn")
        self.assertEqual(msg["usage"]["input_tokens"], 10)
        self.assertEqual(msg["usage"]["output_tokens"], 5)
        types = [c["type"] for c in msg["content"]]
        self.assertEqual(types, ["thinking", "text"])

    def test_tool_use(self):
        chat = {
            "id": "chatcmpl-2",
            "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "",
                            "tool_calls": [{
                                "id": "call_1", "type": "function",
                                "function": {"name": "f", "arguments": '{"a":1}'},
                            }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }
        msg = anthropic_api.chat_to_messages_payload(chat)
        self.assertEqual(msg["stop_reason"], "tool_use")
        block = msg["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["name"], "f")
        self.assertEqual(block["input"], {"a": 1})

    def test_length_finish_maps_to_max_tokens(self):
        chat = {"model": "m",
                "choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}
        self.assertEqual(anthropic_api.chat_to_messages_payload(chat)["stop_reason"],
                         "max_tokens")


class StreamConversionTests(TestCase):
    def _chunks(self):
        yield "data: " + json.dumps({"id": "c", "model": "m",
                                     "choices": [{"delta": {"role": "assistant",
                                                            "content": "Hello"},
                                                  "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m",
                                     "choices": [{"delta": {"reasoning_content": "思考"},
                                                  "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m",
                                     "choices": [{"delta": {"tool_calls": [{
                                         "index": 0, "id": "call_1",
                                         "function": {"name": "f", "arguments": '{"x":1}'}}]},
                                                  "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m",
                                     "choices": [{"delta": {},
                                                  "finish_reason": "tool_calls"}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    def test_event_lifecycle(self):
        events = []
        for line in anthropic_api.iter_chat_sse_as_anthropic(self._chunks()):
            if line.startswith("event: "):
                events.append(line.split("\n")[0].split(": ", 1)[1])
        # 必须包含完整生命周期
        for expected in ("message_start", "content_block_start",
                         "content_block_delta", "content_block_stop",
                         "message_delta", "message_stop"):
            self.assertIn(expected, events)

    def test_text_block_accumulates(self):
        data_events = []
        for line in anthropic_api.iter_chat_sse_as_anthropic(self._chunks()):
            if line.startswith("event: content_block_delta"):
                payload = json.loads(line.split("data: ", 1)[1].strip())
                data_events.append(payload["delta"])
        texts = [d["text"] for d in data_events if d.get("type") == "text_delta"]
        self.assertEqual("".join(texts), "Hello")

    def test_stream_ends_with_message_stop(self):
        out = list(anthropic_api.iter_chat_sse_as_anthropic(self._chunks()))
        self.assertTrue(out[-1].startswith("event: message_stop"))

    def test_blocks_are_serialized_not_overlapping(self):
        # 复现真实顺序：先 reasoning 再 text（带思考模型的常态），
        # 内容块必须严格串行 start/stop，不能两个块同时打开。
        def chunks():
            yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"reasoning_content": "思考"}, "finish_reason": None}]}) + "\n\n"
            yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {"content": "答案"}, "finish_reason": None}]}) + "\n\n"
            yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
                {"delta": {}, "finish_reason": "stop"}]}) + "\n\n"
            yield "data: [DONE]\n\n"

        starts, stops = [], []
        for line in anthropic_api.iter_chat_sse_as_anthropic(chunks()):
            if line.startswith("event: content_block_start"):
                idx = json.loads(line.split("data: ", 1)[1].strip())["index"]
                starts.append(idx)
            elif line.startswith("event: content_block_stop"):
                idx = json.loads(line.split("data: ", 1)[1].strip())["index"]
                stops.append(idx)
        # 串行：start 必须与 stop 一一配对、索引依次递增、不允许重叠打开
        self.assertEqual(starts, [0, 1])
        self.assertEqual(stops, [0, 1])


class CountTokensTests(TestCase):
    def test_count_tokens_positive(self):
        n = anthropic_api.count_tokens({
            "model": "m",
            "messages": [{"role": "user", "content": "hello world"}],
        })
        self.assertGreaterEqual(n, 1)
