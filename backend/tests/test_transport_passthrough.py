"""传输层透传重构回归测试（2026-09 审查修复）。

覆盖审查确认的五类数据丢失/时序 bug + 请求体透传语义：

- A1  /v1/responses 流式出口：标准增量工具流（id 仅首帧出现）的参数
      增量不再被静默丢弃；
- A2  /v1/messages 流式出口：工具块按槽位（tc.index）匹配，后续增量帧
      不再每帧新开一个空 id/空 name 的 tool_use 块；
- A3  /v1/messages 出口 usage 时序：message_delta 延迟到流尾，
      output_tokens 不再恒为 0；
- A4  /v1/responses 出口 usage 时序：response.completed 延迟到流尾，
      usage 不再恒为 {}；
- A5  responses_to_chat_body：Responses 会话态字段
      （previous_response_id/truncation/include）不再漏进 chat 上游；
- A7  未知 input 条目类型降级为规范 JSON 文本（而非 Python dict repr）；
- 请求体真透传：显式 null 保留、SDK 残留字段（extra_body 等）拦截；
- StreamTap 单点解析：转发行不再多次 json.loads、语义对齐旧探测器；
- iter_sse 逐行透传：不丢行、不伪造 [DONE]，截断如实上报。
"""
import asyncio
import json

from django.test import TestCase

from services import anthropic_api, responses_api
from services.stream_pipeline import StreamTap


async def _achunks(gen):
    for c in gen:
        yield c


def _collect(agen) -> list:
    async def _gather():
        return [part async for part in agen]
    return asyncio.run(_gather())


def _events(lines, name: str) -> list[dict]:
    """抽取指定 event 名的 data 载荷（Responses/Anthropic SSE 通用）。

    `lines` 兼容两种形态：
    - 整事件字符串列表（每元素含 "event: X\\ndata: {...}\\n\\n"）；
    - 已按行拆分（out.splitlines()，event 行与 data 行分离）。
    """
    out = []
    prefix = f"event: {name}"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            # 形态一：同一字符串内含 data 行
            _, _, rest = line.partition("\n")
            if rest.startswith("data: "):
                out.append(json.loads(rest.split("data: ", 1)[1].strip()))
                continue
            # 形态二：data 行在下一条（或下下条）
            for j in (i + 1, i + 2):
                if j < len(lines) and lines[j].startswith("data: "):
                    out.append(json.loads(
                        lines[j].split("data: ", 1)[1].strip()))
                    break
    return out


class ResponsesExitToolStreamTests(TestCase):
    """A1：标准增量工具流（OpenAI 规范形态，后续帧无 id）。"""

    @staticmethod
    def _chunks():
        # 首帧：id + name + index
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                       "function": {"name": "f", "arguments": ""}}]},
             "finish_reason": None}]}) + "\n\n"
        # 增量帧：只有 index + arguments（规范流 id 为空串）
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {"tool_calls": [{"index": 0, "id": "",
                                       "function": {"name": None, "arguments": '{"ci'}}]},
             "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {"tool_calls": [{"index": 0, "id": "",
                                       "function": {"name": None, "arguments": 'ty":1}'}}]},
             "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {}, "finish_reason": "tool_calls"}]}) + "\n\n"
        # usage 尾帧（OpenAI 语义：finish 之后的独立空 choices 帧）
        yield "data: " + json.dumps({"id": "c", "usage": {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}) + "\n\n"
        yield "data: [DONE]\n\n"

    def test_argument_deltas_not_dropped(self):
        out = "".join(_collect(responses_api.iter_chat_sse_as_responses(
            _achunks(self._chunks()))))
        deltas = [e["delta"] for e in _events(out.splitlines(),
                                              "response.function_call_arguments.delta")]
        self.assertEqual("".join(deltas), '{"city":1}')

    def test_done_item_carries_full_arguments(self):
        out = "".join(_collect(responses_api.iter_chat_sse_as_responses(
            _achunks(self._chunks()))))
        done = _events(out.splitlines(), "response.output_item.done")
        fc = [d["item"] for d in done if d["item"].get("type") == "function_call"]
        self.assertEqual(len(fc), 1)
        self.assertEqual(fc[0]["arguments"], '{"city":1}')
        self.assertEqual(fc[0]["name"], "f")
        self.assertEqual(fc[0]["call_id"], "call_1")

    def test_completed_carries_tail_usage(self):
        """A4：usage 尾帧（finish 之后、空 choices）必须进 response.completed，
        且键名转为 Responses 规范（input_tokens/output_tokens）。"""
        out = "".join(_collect(responses_api.iter_chat_sse_as_responses(
            _achunks(self._chunks()))))
        completed = _events(out.splitlines(), "response.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["response"]["usage"]["input_tokens"], 10)
        self.assertEqual(completed[0]["response"]["usage"]["output_tokens"], 5)

    def test_output_index_increments(self):
        out = "".join(_collect(responses_api.iter_chat_sse_as_responses(
            _achunks(self._chunks()))))
        added = _events(out.splitlines(), "response.output_item.added")
        idxs = [e["output_index"] for e in added]
        self.assertEqual(idxs, sorted(set(idxs)))


class AnthropicExitToolStreamTests(TestCase):
    """A2/A3：标准增量工具流按槽位合并 + usage 尾帧入账。"""

    @staticmethod
    def _chunks():
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                       "function": {"name": "f", "arguments": '{"x":'}}]},
             "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {"tool_calls": [{"index": 0, "id": "",
                                       "function": {"name": None, "arguments": '1}'}}]},
             "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"delta": {}, "finish_reason": "tool_calls"}]}) + "\n\n"
        yield "data: " + json.dumps({"id": "c", "usage": {
            "prompt_tokens": 7, "completion_tokens": 3}}) + "\n\n"
        yield "data: [DONE]\n\n"

    def test_single_tool_block_with_merged_args(self):
        out = _collect(anthropic_api.iter_chat_sse_as_anthropic(
            _achunks(self._chunks())))
        starts = [json.loads(l.split("data: ", 1)[1])
                  for l in out if l.startswith("event: content_block_start\n")]
        tool_starts = [s for s in starts if s["content_block"]["type"] == "tool_use"]
        # 旧 bug：每个增量帧新开一个空 id/空 name 的 tool_use 块
        self.assertEqual(len(tool_starts), 1)
        self.assertEqual(tool_starts[0]["content_block"]["id"], "call_1")
        self.assertEqual(tool_starts[0]["content_block"]["name"], "f")
        deltas = [json.loads(l.split("data: ", 1)[1])
                  for l in out if l.startswith("event: content_block_delta\n")]
        args = "".join(d["delta"]["partial_json"] for d in deltas
                       if d["delta"]["type"] == "input_json_delta")
        self.assertEqual(args, '{"x":1}')

    def test_message_delta_carries_tail_usage(self):
        """A3：usage 尾帧（finish 之后）必须进 message_delta.usage。"""
        out = _collect(anthropic_api.iter_chat_sse_as_anthropic(
            _achunks(self._chunks())))
        delta_events = _events(out, "message_delta")
        self.assertEqual(len(delta_events), 1)
        self.assertEqual(delta_events[0]["usage"]["output_tokens"], 3)
        self.assertEqual(delta_events[0]["delta"]["stop_reason"], "tool_use")
        # message_stop 必须是最后一个事件
        self.assertTrue(out[-1].startswith("event: message_stop"))

    def test_no_premature_termination(self):
        """finish 帧不得提前触发 message_delta/message_stop（否则 usage 帧无处可写）。"""
        out = _collect(anthropic_api.iter_chat_sse_as_anthropic(
            _achunks(self._chunks())))
        kinds = [l.split("event: ")[1].split("\n")[0]
                 for l in out if l.startswith("event: ")]
        # message_delta/message_stop 只能出现在所有 content_block_stop 之后
        # （即流尾），且各只出现一次
        self.assertEqual(kinds[-2:], ["message_delta", "message_stop"])
        self.assertEqual(kinds.count("message_delta"), 1)
        self.assertGreater(kinds.index("message_delta"),
                           kinds.index("content_block_stop"))


class RequestBodyPassthroughTests(TestCase):
    """请求体真透传：null 保留 / SDK 残留拦截 / Responses 会话字段剔除。"""

    def test_explicit_null_is_forwarded(self):
        from api.openai_views import _build_upstream_body
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": None, "logit_bias": None}
        up = _build_upstream_body(body, "real/m", None)
        # 显式 null 是"显式未设置"，与缺省不同——旧实现静默剥掉属于改写请求
        self.assertIsNone(up["tool_choice"])
        self.assertIsNone(up["logit_bias"])

    def test_sdk_residue_fields_are_dropped(self):
        """extra_body / reasoning_effort_value 等思考族原始形态不得漏给上游。"""
        from api.openai_views import _build_upstream_body
        body = {"model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "extra_body": {"reasoning_effort": "high"},
                "reasoning_effort_value": "high",
                "reasoning": {"effort": "high"}}
        up = _build_upstream_body(body, "real/m", None)
        self.assertNotIn("extra_body", up)
        self.assertNotIn("reasoning_effort_value", up)
        self.assertNotIn("reasoning", up)
        # 归一化产物正常回填
        self.assertEqual(up.get("reasoning_effort"), "high")

    def test_responses_session_fields_are_dropped(self):
        chat = responses_api.responses_to_chat_body({
            "model": "m", "input": "hi",
            "previous_response_id": "resp_old", "truncation": "auto",
            "include": ["reasoning.encrypted_content"],
        })
        self.assertNotIn("previous_response_id", chat)
        self.assertNotIn("truncation", chat)
        self.assertNotIn("include", chat)

    def test_responses_unknown_field_still_passes(self):
        """会话字段剔除不扩大化：未知顶层字段仍保留（forward-compat）。"""
        chat = responses_api.responses_to_chat_body({
            "model": "m", "input": "hi", "some_future_field": {"a": 1}})
        self.assertEqual(chat["some_future_field"], {"a": 1})

    def test_unknown_input_item_becomes_json_text(self):
        """A7：未知 input 条目降级为规范 JSON 文本，而非 dict repr。"""
        chat = responses_api.responses_to_chat_body({
            "model": "m",
            "input": [{"type": "item_reference", "id": "it_1"}]})
        content = chat["messages"][0]["content"]
        self.assertEqual(json.loads(content), {"type": "item_reference", "id": "it_1"})


class StreamTapTests(TestCase):
    """StreamTap：单点解析 + 与旧探测器语义对齐。"""

    def test_semantics_align_with_old_probes(self):
        tap = StreamTap()
        # role 空帧：旧 _chunk_has_content=False（不锁重试），any_signal=False
        info = tap.feed('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
        self.assertFalse(tap.sent_content)
        self.assertFalse(info.has_payload)
        # 思考帧：旧 any_signal=True（切超时档），sent_content 仍 False（可重试）
        info = tap.feed('data: {"choices":[{"delta":{"reasoning_content":"嗯"}}]}\n\n')
        self.assertTrue(info.has_reasoning)
        self.assertFalse(tap.sent_content)
        # 正文帧：两边都 True
        info = tap.feed('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
        self.assertTrue(info.has_payload)
        self.assertTrue(tap.sent_content)
        # usage 帧（累积替换）
        tap.feed('data: {"choices":[],"usage":{"prompt_tokens":3,'
                 '"completion_tokens":2}}\n\n')
        self.assertEqual(tap.usage["prompt_tokens"], 3)
        # [DONE]
        tap.feed("data: [DONE]\n\n")
        self.assertTrue(tap.saw_done)

    def test_finish_reason_captured(self):
        tap = StreamTap()
        tap.feed('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        self.assertEqual(tap.finish_reason, "stop")
        self.assertTrue(tap.sent_content)

    def test_comments_and_bad_json_ignored(self):
        tap = StreamTap()
        info = tap.feed(": keep-alive\n\n")
        self.assertFalse(info.has_signal)
        info = tap.feed("data: not-json\n\n")
        self.assertFalse(info.has_signal)
        self.assertEqual(tap.chunk_count, 2)

    def test_completion_text_accumulation(self):
        tap = StreamTap()
        for t in ("你", "好"):
            tap.feed("data: " + json.dumps(
                {"choices": [{"delta": {"content": t}}]}) + "\n\n")
        self.assertEqual(tap.completion_text, "你好")


class IterSsePassthroughTests(TestCase):
    """iter_sse 逐行透传：上游行不丢、不重排、不伪造终结帧。"""

    def test_lines_pass_through_verbatim(self):
        from services.race_engine import iter_sse

        async def upstream():
            yield 'data: {"a": 1}'
            yield ""  # 空行被跳过（事件分隔符不单独成帧）
            yield "data: [DONE]"

        async def run():
            state: dict = {}
            out = [chunk async for chunk in iter_sse(
                'data: {"first":true}', upstream(), state=state)]
            return out, state

        chunks, state = asyncio.run(run())
        self.assertEqual(chunks, ['data: {"first":true}\n\n',
                                  'data: {"a": 1}\n\n',
                                  'data: [DONE]\n\n'])
        self.assertIs(state["saw_done"], True)

    def test_stream_end_without_done_does_not_fabricate(self):
        """上游静默断流：绝不伪造 [DONE]，saw_done 如实上报 False。"""
        from services.race_engine import iter_sse

        async def upstream():
            yield 'data: {"choices":[{"delta":{"content":"半截"}}]}'
            # 无 [DONE] 直接结束

        async def run():
            state: dict = {}
            out = [chunk async for chunk in iter_sse(
                'data: {"first":true}', upstream(), state=state)]
            return out, state

        chunks, state = asyncio.run(run())
        self.assertNotIn("data: [DONE]", "".join(chunks))
        self.assertIs(state["saw_done"], False)


class InlineErrorAccountingTests(TestCase):
    """上游流内 error 帧（muse 超窗口等）：透传零丢失 + 记账如实。"""

    def test_tap_captures_inline_error(self):
        from services.stream_pipeline import StreamTap
        tap = StreamTap()
        info = tap.feed('data: {"error":{"message":"context window '
                        'exceeded","code":400}}\n\n')
        self.assertTrue(info.has_error)
        self.assertTrue(tap.saw_error)
        self.assertEqual(tap.error_detail["message"], "context window exceeded")

    def test_plain_error_frame_not_a_truncation_signal(self):
        """error 帧 + [DONE]：不得误判静默截断（流已如实收尾）。"""
        from services.stream_pipeline import StreamTap
        tap = StreamTap()
        tap.feed('data: {"error":{"message":"boom"}}\n\n')
        tap.feed("data: [DONE]\n\n")
        # error 也应锁定重试闸门（已交付内容）
        self.assertTrue(tap.sent_content)
        self.assertTrue(tap.saw_done)


class ZeroLossContractTests(TestCase):
    """零丢失契约：上游发出的任何字节都不允许被静默丢弃。

    盘查过的历史丢弃点（2026-09 零丢失审查全部修复）：
    - iter_sse: 不丢行、不伪造 [DONE]
    - iter_responses_sse: 未知事件/注释行原样透传
    - 密文思考：解得开给明文，解不开原样透传（绝不占位符/丢弃）
    """

    def test_unrecognized_response_event_passes_through(self):
        """Responses 上游的未来新事件类型 / 自定义事件不丢失。"""

        async def src():
            yield 'data: {"type":"response.custom.future_event","x":1}\n\n'
            yield 'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            'data: {"type":"response.created","response":{}}', src())))
        # 未识别事件原样透传（保真字节），被识别事件照常翻译成 chat 格式
        self.assertIn("response.custom.future_event", out)
        self.assertIn('"content": "hi"', out)

    def test_response_comment_line_passes_through(self):
        """Responses 上游的 SSE 注释行（保活）不丢失。"""

        async def src():
            yield ": upstream-keepalive\n\n"
            yield 'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse("", src(),
                                                                include_first=False)))
        self.assertIn(": upstream-keepalive", out)

    def test_undecryptable_ciphertext_never_dropped(self):
        """解不开的思考密文：非流式 message 原样保留（不占位、不剥离）。"""
        from services.reasoning_decrypt import decrypt_chat_message
        blob = "43e3da0e" + "A" * 300   # 非 Fernet 不透明密文
        msg = {"reasoning_content": blob, "content": "答"}
        decrypt_chat_message(msg)
        self.assertEqual(msg["reasoning_content"], blob)


class ChatExitProtocolMetaLineTests(TestCase):
    """chat 出口的 SSE 元行过滤（req_df22e4f muse 案回归）。

    zen muse 上游每帧携带 `event: response.*` 元行。零丢失透传重构把
    未识别行原样下发后，元行与翻译后的 chat chunk 混流，客户端按
    chat 逐帧解析即崩（ZCode TerminalStreamChunkError / context_exceeded
    误报——完整 Responses 载荷被当正文注入对话上下文）。
    """

    def test_event_lines_never_reach_chat_exit(self):
        """主循环里的 event: 元行被过滤，data 翻译与注释照常。"""

        async def src():
            yield 'event: response.created\n\n'
            yield 'data: {"type":"response.created","response":{}}\n\n'
            yield 'event: response.output_text.delta\n\n'
            yield 'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            yield ': ping\n\n'
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            'event: response.created\n\ndata: {"type":"response.created","response":{}}\n\n',
            src())))
        self.assertNotIn("event:", out)
        self.assertIn('"content": "hi"', out)
        self.assertIn(": ping", out)
        self.assertIn("[DONE]", out)

    def test_meta_lines_filtered_in_prelude(self):
        """竞速 prelude 重放同样过滤元行（无 chat 内容的 data 帧保留）。"""

        async def src():
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            "", src(), include_first=False,
            prelude=['event: response.created\n\n',
                     'data: {"type":"response.in_progress","sequence_number":1}\n\n'])))
        self.assertNotIn("event:", out)
        self.assertNotIn("in_progress", out)

    def test_id_retry_lines_filtered(self):
        """id:/retry: 元行同样不允许到达 chat 出口。"""

        async def src():
            yield 'id: 42\n\n'
            yield 'retry: 3000\n\n'
            yield 'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            "", src(), include_first=False)))
        self.assertNotIn("id:", out)
        self.assertNotIn("retry:", out)
        self.assertIn('"content": "x"', out)


class ChatExitUntranslatedResponseEventTests(TestCase):
    """第二层过滤：未翻译的 Responses 协议 data 帧不出 chat 出口。

    _translate_event 对已识别但 chat 无对应帧的事件（in_progress /
    output_item.added 非 function_call / content_part.* / output_item.done
    的 message 条目）返回 None，零丢失透传会把整包 Response 对象漏进
    chat 流——客户端解析即崩，大段载荷被注入对话上下文（req_df22e4f 案
    第二轮 76k 输入的直接来源）。协议内事件（response.*）滤除；
    协议外自定义事件照旧透传（零丢失契约不破）。
    """

    def test_recognized_contentless_events_filtered(self):
        """in_progress / content_part / message done 全族不出口。"""

        async def src():
            yield 'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"r1"}}\n\n'
            yield 'data: {"type":"response.output_item.added","item":{"type":"reasoning","summary":[]}}\n\n'
            yield 'data: {"type":"response.content_part.added","part":{"type":"output_text","text":""}}\n\n'
            yield 'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"FULL"}]}}\n\n'
            yield 'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            "", src(), include_first=False)))
        self.assertNotIn("response.in_progress", out)
        self.assertNotIn("output_item.added", out)
        self.assertNotIn("content_part.added", out)
        self.assertNotIn('"FULL"', out)          # message done 载荷不入流
        self.assertIn('"content": "hi"', out)    # 翻译帧照常

    def test_ping_data_frame_filtered(self):
        """ping data 帧（muse 保活）属协议噪声，不出 chat 出口。"""

        async def src():
            yield 'data: {"type":"ping","cost":"0"}\n\n'
            yield 'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            "", src(), include_first=False)))
        self.assertNotIn('"ping"', out)
        self.assertIn('"content": "x"', out)

    def test_truly_custom_event_still_passes_through(self):
        """协议外自定义事件照旧透传（零丢失契约不收缩）。"""

        async def src():
            yield 'data: {"type":"upstream.custom.metric","v":1}\n\n'
            yield 'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            "", src(), include_first=False)))
        self.assertIn("upstream.custom.metric", out)
        self.assertIn('"content": "x"', out)

    def test_prelude_untranslated_events_filtered(self):
        """竞速 prelude 里的协议内事件同样滤除，正文帧从 role 标记起步。"""

        async def src():
            yield 'data: [DONE]\n\n'

        out = "".join(_collect(responses_api.iter_responses_sse(
            "", src(), include_first=False,
            prelude=[
                'event: response.created\n\n',
                'data: {"type":"response.created","response":{}}\n\n',
                'data: {"type":"response.in_progress","sequence_number":1}\n\n',
            ])))
        self.assertNotIn("event:", out)
        self.assertNotIn("in_progress", out)
        # prelude 不经翻译器：协议 data 帧整帧截留，正文从首帧翻译起步
        self.assertIn("data: [DONE]", out)


class ToolCallDeltaFrameShapeTests(TestCase):
    """tool_call 增量帧合法性（ZCode AI_InvalidResponseDataError 案）。

    Vercel AI SDK 的 zod schema 逐字段校验 function.name 必须 string，
    翻译层发 "name": null 即抛 AI_InvalidResponseDataError（网关侧字节
    合法、日志成功，客户端解析崩溃）。另修复 item.id/call_id 双标识
    槽位分裂：arguments.delta 只带 item.id，added/done 只带 call_id，
    旧实现按两个 key 各自分槽 → 参数流进空槽 + done 兜底重复发参数。
    """

    def _stream_out(self, lines: list) -> list[dict]:
        import asyncio

        async def src():
            for l in lines:
                yield l

        async def _gather():
            parts = []
            async for c in responses_api.iter_responses_sse(
                    "", src(), include_first=False):
                parts.append(c)
            return "".join(parts)

        out = asyncio.run(_gather())
        return [json.loads(l[5:]) for l in out.splitlines()
                if l.startswith("data: ") and l[5:].strip() != "[DONE]"]

    def _fc_added(self, item_id="fc_abc123", call_id="call_001"):
        return ('data: {"type":"response.output_item.added",'
                '"item":{"id":"%s","call_id":"%s","type":"function_call",'
                '"name":"get_weather","arguments":""}}\n\n'
                % (item_id, call_id))

    def _args_delta(self, frag: str, item_id="fc_abc123"):
        return ('data: {"type":"response.function_call_arguments.delta",'
                '"item_id":"%s","delta":%s}\n\n'
                % (item_id, json.dumps(frag)))

    def _tc_of(self, frames):
        """抽出全部 tool_call 增量对象。"""
        out = []
        for f in frames:
            for tc in (f.get("choices") or [{}])[0].get("delta", {}).get(
                    "tool_calls") or []:
                out.append(tc)
        return out

    def test_no_null_name_in_tool_call_deltas(self):
        """任何 tool_call 帧都不允许出现 "name": null。

        added 声明帧必须带 name（string）；参数增量帧的 function 键集
        里 name 必须整体省略（null 是 AI SDK zod 校验的非法值）。
        """
        frames = self._stream_out([
            self._fc_added(),
            self._args_delta('{"city":'),
            self._args_delta('"北京"}'),
            'data: {"type":"response.output_item.done",'
            '"item":{"id":"fc_abc123","call_id":"call_001",'
            '"type":"function_call","name":"get_weather",'
            '"arguments":"{\\"city\\":\\"北京\\"}"}}\n\n',
            'data: [DONE]\n\n',
        ])
        for tc in self._tc_of(frames):
            fn = tc["function"]
            if "name" in fn:
                self.assertIsInstance(fn["name"], str,
                                      f"name 出现时必须是 string: {tc}")

    def test_slot_unified_across_item_id_and_call_id(self):
        """added/args-delta/done 三阶段同一 call_id 必须同一槽位。"""
        frames = self._stream_out([
            self._fc_added(),
            self._args_delta('{"city":'),
            self._args_delta('"北京"}'),
            'data: [DONE]\n\n',
        ])
        slots = [tc["index"] for tc in self._tc_of(frames)]
        self.assertEqual(slots, [0, 0, 0])  # 全部同一槽位（旧实现 [0,1,1]）

    def test_done_not_duplicate_after_deltas(self):
        """delta 已透传参数时，done 不再兜底补发完整参数（防双倍拼接）。"""
        frames = self._stream_out([
            self._fc_added(),
            self._args_delta('{"city":'),
            self._args_delta('"北京"}'),
            'data: {"type":"response.output_item.done",'
            '"item":{"id":"fc_abc123","call_id":"call_001",'
            '"type":"function_call","name":"get_weather",'
            '"arguments":"{\\"city\\":\\"北京\\"}"}}\n\n',
            'data: [DONE]\n\n',
        ])
        args_chunks = [tc["function"].get("arguments")
                       for tc in self._tc_of(frames)
                       if tc["function"].get("arguments")]
        joined = "".join(args_chunks)
        self.assertEqual(joined, '{"city":"北京"}')  # 恰好一次拼接，合法 JSON

    def test_id_only_on_first_delta_frame(self):
        """id 仅在参数增量首帧携带，后续帧省略（重复 id 视同新调用）。"""
        frames = self._stream_out([
            self._fc_added(),
            self._args_delta('{"a"'),
            self._args_delta(':1}'),
            'data: [DONE]\n\n',
        ])
        ids = [tc.get("id") for tc in self._tc_of(frames)]
        # added 帧、参数首帧携带 id；参数后续帧省略
        self.assertEqual(ids[0], "call_001")
        self.assertEqual(ids[1], "call_001")
        self.assertIsNone(ids[2])

    def test_parallel_calls_get_distinct_slots(self):
        """并行双工具：item.id 各自归一，两个槽位不串流。"""
        frames = self._stream_out([
            self._fc_added("fc_1", "call_A"),
            self._fc_added("fc_2", "call_B"),
            self._args_delta('{"x"', "fc_1"),
            self._args_delta('{"y"', "fc_2"),
            'data: [DONE]\n\n',
        ])
        by_slot: dict = {}
        for tc in self._tc_of(frames):
            if tc["function"].get("arguments"):
                by_slot.setdefault(tc["index"], []).append(
                    tc["function"]["arguments"])
        self.assertEqual(len(by_slot), 2)
        self.assertEqual(by_slot[0], ['{"x"'])
        self.assertEqual(by_slot[1], ['{"y"'])
