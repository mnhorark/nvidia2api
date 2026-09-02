"""Anthropic Messages API（`/v1/messages`）与内部 Chat Completions 格式互转。

大量客户端（Claude Code / Cline / Roo / OpenAI 兼容工具的 Anthropic 分支等）
习惯以 Anthropic Messages 协议调用上游。本模块提供三个方向的转换，使这些
客户端可以直接以 `Authorization: Bearer sk-nvidia2api-xxx` 调用平台：

- 请求体：Anthropic Messages -> 内部 chat（system/messages/tools/thinking 等
  结构性映射）；
- 非流式响应：内部 chat.completion -> Anthropic Message 对象（text /
  thinking / tool_use 内容块 + stop_reason + usage）；
- 流式：内部 chat SSE -> Anthropic SSE 事件流（message_start /
  content_block_* / message_delta / message_stop）。

与 responses_api 一样，转换只发生在入口/出口，竞速、重试、日志、限流等
核心链路完全复用内部 chat 格式。
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Iterator
from uuid import uuid4

# 通用同名参数：忠实透传
_COMMON = frozenset({
    "model", "stream", "temperature", "top_p", "stop", "metadata", "user",
})

# Anthropic 默认 stop_reason 取值
_STOP_TO_ANTHROPIC = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    "stop_sequence": "stop_sequence",
}
_ANTHROPIC_TO_STOP = {v: k for k, v in _STOP_TO_ANTHROPIC.items()}
_ANTHROPIC_TO_STOP["refusal"] = "content_filter"


# ---------------------------------------------------------------------------
# 请求体：Anthropic Messages -> 内部 chat
# ---------------------------------------------------------------------------

def _blocks_to_content(blocks) -> tuple[str, list, list, list]:
    """把 Anthropic content 块解析成 (text, images, tool_calls, tool_results)。

    - text 块 -> 普通文本
    - image 块 -> chat image_url 列表项（转 data URI）
    - tool_use 块（assistant）-> chat tool_calls
    - tool_result 块（user）-> 单条 tool 消息
    """
    text_parts: list[str] = []
    images: list[dict] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    if isinstance(blocks, str):
        text_parts.append(blocks)
        return "".join(text_parts), images, tool_calls, tool_results
    if not isinstance(blocks, list):
        text_parts.append(str(blocks))
        return "".join(text_parts), images, tool_calls, tool_results
    for part in blocks:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text_parts.append(str(part.get("text") or ""))
        elif ptype == "image":
            src = part.get("source") or {}
            if src.get("type") == "base64":
                media = src.get("media_type") or "image/png"
                data = str(src.get("data") or "")
                images.append({"type": "image_url",
                               "image_url": {"url": f"data:{media};base64,{data}"}})
            elif src.get("type") == "url":
                images.append({"type": "image_url",
                               "image_url": {"url": str(src.get("url") or "")}})
        elif ptype == "tool_use":
            tool_calls.append({
                "id": str(part.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(part.get("name") or ""),
                    "arguments": json.dumps(part.get("input") or {}, ensure_ascii=False),
                },
            })
        elif ptype == "tool_result":
            tool_results.append({
                "tool_call_id": str(part.get("tool_use_id") or ""),
                "content": _tool_result_text(part.get("content")),
            })
    return "".join(text_parts), images, tool_calls, tool_results


def _tool_result_text(content) -> str:
    """tool_result.content 可为字符串或 {type: text} 块数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content or "")


def _tools_to_chat(tools) -> list | None:
    """Anthropic tools（name/input_schema 平铺）-> chat tools（function 包装）。"""
    if not isinstance(tools, list):
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        out.append({"type": "function", "function": {
            "name": str(t.get("name") or ""),
            "description": str(t.get("description") or ""),
            "parameters": t.get("input_schema") or {},
        }})
    return out or None


def _tool_choice_to_chat(tc) -> Any:
    """Anthropic tool_choice -> chat tool_choice。

    auto -> auto；any -> required；tool + name -> 指定函数。
    """
    if not isinstance(tc, dict):
        return tc
    ttype = tc.get("type")
    if ttype == "tool":
        return {"type": "function",
                "function": {"name": str(tc.get("name") or "")}}
    if ttype == "any":
        return "required"
    return "auto"


def messages_to_chat_body(body: dict) -> dict:
    """把 Anthropic Messages 请求体转成内部 chat/completions 请求体。

    无损原则：_COMMON 同名透传 + stop_sequences/max_tokens/thinking 结构性
    映射，未知顶层字段一律保留（forward-compat）。
    """
    out: dict[str, Any] = {}
    for key in _COMMON:
        if key in body and body[key] is not None:
            out[key] = body[key]
    # 保留未知顶层字段（Anthropic 未来新增字段无损透传）
    _ANTHROPIC_STRUCTURAL = frozenset({
        "system", "messages", "tools", "tool_choice", "thinking",
        "max_tokens", "stop_sequences",
    })
    for key, val in body.items():
        if key not in _COMMON and key not in _ANTHROPIC_STRUCTURAL and val is not None:
            out[key] = val
    # stop_sequences 是多值数组，chat 的 stop 接受字符串或字符串数组
    stop = body.get("stop_sequences")
    if stop is not None:
        out["stop"] = stop
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    tools = _tools_to_chat(body.get("tools"))
    if tools is not None:
        out["tools"] = tools
    tc = _tool_choice_to_chat(body.get("tool_choice"))
    if tc is not None:
        out["tool_choice"] = tc
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        out["thinking"] = True
        budget = thinking.get("budget_tokens")
        if budget is not None:
            out["reasoning_budget"] = budget

    messages: list[dict] = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        parts = [str(b.get("text") or "") for b in system
                 if isinstance(b, dict) and b.get("type") == "text"]
        joined = "".join(parts).strip()
        if joined:
            messages.append({"role": "system", "content": joined})

    raw_messages = body.get("messages")
    if isinstance(raw_messages, list):
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user")
            text, images, tool_calls, tool_results = _blocks_to_content(msg.get("content"))
            if role == "user" and tool_results:
                # 一条 user 消息可以同时带正文与 tool_result 块（Claude Code 常
                # 见：先说明意图再回传工具结果）。正文必须先落成一条 user 消息，
                # 否则会被整体丢弃、模型看不到用户的说明。
                if text.strip():
                    messages.append({"role": "user", "content": text})
                for tr in tool_results:
                    messages.append({"role": "tool", **tr})
                continue
            content: Any = text
            if images:
                content = []
                if text:
                    content.append({"type": "text", "text": text})
                content.extend(images)
            m: dict = {"role": role, "content": content}
            if role == "assistant" and tool_calls:
                m["tool_calls"] = tool_calls
            messages.append(m)
    if messages:
        out["messages"] = messages
    return out


# ---------------------------------------------------------------------------
# 非流式响应：内部 chat -> Anthropic Messages
# ---------------------------------------------------------------------------

def _parse_arguments(raw: str):
    """tool_calls.arguments 尽量解析为对象，失败回落为原字符串。

    保持无损：若上游已是对象形态（非字符串）直接返回。
    """
    if raw is None:
        return {}
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return raw


def chat_to_messages_payload(chat: dict) -> dict:
    """把内部 chat.completion 非流式响应转成 Anthropic Message 对象。

    内容块顺序：thinking（推理）-> text -> tool_use；stop_reason 按
    finish_reason 映射；usage 键名 prompt/completion -> input/output。
    """
    choices = chat.get("choices") or []
    msg = (choices[0] or {}).get("message") or {} if choices else {}
    content: list[dict] = []
    reasoning = msg.get("reasoning_content")
    if reasoning:
        content.append({"type": "thinking", "thinking": str(reasoning)})
    text = msg.get("content")
    if text:
        content.append({"type": "text", "text": str(text)})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        content.append({
            "type": "tool_use",
            "id": str(tc.get("id") or ""),
            "name": str(fn.get("name") or ""),
            "input": _parse_arguments(str(fn.get("arguments") or "")),
        })
    if not content:
        content.append({"type": "text", "text": ""})

    finish = (choices[0] or {}).get("finish_reason") if choices else None
    stop_reason = _STOP_TO_ANTHROPIC.get(str(finish), "end_turn")
    usage = chat.get("usage") or {}
    return {
        "id": str(chat.get("id") or "msg_x"),
        "type": "message",
        "role": "assistant",
        "model": str(chat.get("model") or ""),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or 0,
            "output_tokens": usage.get("completion_tokens", 0) or 0,
        },
    }


# ---------------------------------------------------------------------------
# 流式：内部 chat SSE -> Anthropic SSE
# ---------------------------------------------------------------------------

def _sse(name: str, obj: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def iter_chat_sse_as_anthropic(chat_iter: AsyncIterator[str]) -> AsyncIterator[str]:
    """把内部 chat 格式的 SSE 行流转成 Anthropic Messages SSE 事件流。

    事件生命周期：message_start -> content_block_start -> content_block_delta
    xN -> content_block_stop -> message_delta -> message_stop。推理增量映射为
    thinking 块，正文映射为 text 块，工具调用映射为 tool_use 块。
    异步生成器以保持真正的逐块流式。
    """
    seen_start = False
    done_sent = False
    # 已开启的内容块：index -> 类型；块号从 0 递增
    block_index = 0
    opened: list[tuple[int, str]] = []  # (index, type)
    text_buf = ""
    thinking_buf = ""
    # 工具块暂存：index -> {id, name, args}
    tool_blocks: dict[int, dict] = {}
    usage: dict = {}
    model = ""
    # 每次请求唯一：客户端（含 Claude Code）会用 message.id 做去重与日志关联，
    # 恒定值会让不同轮次的响应互相混淆。
    rid = "msg_" + uuid4().hex[:24]

    async def emit_start() -> AsyncIterator[str]:
        nonlocal seen_start
        if seen_start:
            return
        seen_start = True
        yield _sse("message_start", {
            "type": "message_start",
            "message": {"id": rid, "type": "message", "role": "assistant",
                        "model": model, "content": [], "stop_reason": None,
                        "stop_sequence": None, "usage": {"input_tokens": 0,
                                                          "output_tokens": 0}},
        })

    async def open_block(btype: str) -> AsyncIterator[str]:
        nonlocal block_index
        # Anthropic 要求内容块严格串行：开新块前必须先关闭当前块，
        # 否则 thinking/text/tool_use 会同时处于打开状态，违反协议且
        # 后续 delta 会用最后一个打开的块索引发送（正文被归到工具块）。
        if opened:
            async for _e in close_block(*opened[-1]):
                yield _e
            opened.clear()
        idx = block_index
        block_index += 1
        opened.append((idx, btype))
        if btype == "text":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""}})
        elif btype == "thinking":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "thinking", "thinking": ""}})
        elif btype == "tool_use":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": "", "name": "", "input": {}}})

    async def close_block(idx: int, btype: str) -> AsyncIterator[str]:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    try:
        async for chunk in chat_iter:
            # SSE 心跳注释（`: keep-alive`）原样透传：/v1/messages 出口同样保活
            if chunk.startswith(":"):
                yield chunk
                continue
            if not chunk.startswith("data:"):
                continue
            payload = chunk[5:].strip().rstrip("\n")
            if payload == "[DONE]":
                if not done_sent:
                    done_sent = True
                    async for _e in emit_start():
                        yield _e
                    for idx, btype in opened:
                        async for _e in close_block(idx, btype):
                            yield _e
                    stop = "end_turn"
                    yield _sse("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": usage.get("completion_tokens", 0) or 0}})
                    yield _sse("message_stop", {"type": "message_stop"})
                return
            try:
                data = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                # 严格客户端（Claude Code 等）要求 error 事件也必须处在完整的消息
                # 生命周期内：先 message_start、最后 message_stop。缺失 start 会让
                # 客户端在未收到 message_start 的情况下收到 error 而直接抛错。
                async for _e in emit_start():
                    yield _e
                yield _sse("error", {"type": "error", "error": data["error"]})
                yield _sse("message_stop", {"type": "message_stop"})
                done_sent = True
                return
            model = model or str(data.get("model") or "")
            choices = data.get("choices") or []
            if not choices:
                if data.get("usage"):
                    usage = data["usage"]
                continue
            ch = choices[0]
            async for _e in emit_start():
                yield _e
            delta = ch.get("delta") or {}
            finish = ch.get("finish_reason")

            reasoning = delta.get("reasoning_content")
            if reasoning:
                if not opened or opened[-1][1] != "thinking":
                    async for _e in open_block("thinking"):
                        yield _e
                thinking_buf += str(reasoning)
                idx = opened[-1][0]
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "thinking_delta", "thinking": str(reasoning)}})
            content = delta.get("content")
            if content:
                if not opened or opened[-1][1] != "text":
                    async for _e in open_block("text"):
                        yield _e
                text_buf += str(content)
                idx = opened[-1][0]
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": str(content)}})
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    iid = str(tc.get("id") or "")
                    name = str(fn.get("name") or "")
                    args = str(fn.get("arguments") or "")
                    # 新工具：id 或 name 首次出现即开块
                    target = None
                    for idx, tb in tool_blocks.items():
                        if tb.get("id") == iid or (iid and tb.get("id", "").startswith(iid[:8])):
                            target = idx
                            break
                    if target is None:
                        # 新工具开块前同样先关闭当前块，保持块严格串行
                        if opened:
                            async for _e in close_block(*opened[-1]):
                                yield _e
                            opened.clear()
                        target = block_index
                        block_index += 1
                        tool_blocks[target] = {"id": iid, "name": name, "args": ""}
                        opened.append((target, "tool_use"))
                        yield _sse("content_block_start", {
                            "type": "content_block_start", "index": target,
                            "content_block": {"type": "tool_use",
                                              "id": iid, "name": name, "input": {}}})
                    if target is not None:
                        # 若已有其他块（text/thinking）打开，先关闭再回到工具块
                        if opened and opened[-1][0] != target:
                            async for _e in close_block(*opened[-1]):
                                yield _e
                            opened.clear()
                            opened.append((target, "tool_use"))
                        if name:
                            tool_blocks[target]["name"] = name
                        tool_blocks[target]["args"] += args
                        yield _sse("content_block_delta", {
                            "type": "content_block_delta", "index": target,
                            "delta": {"type": "input_json_delta", "partial_json": args}})
            if data.get("usage"):
                usage = data["usage"]
            if finish:
                # 结束当前所有块
                for idx, btype in opened:
                    async for _e in close_block(idx, btype):
                        yield _e
                opened.clear()
                stop = _STOP_TO_ANTHROPIC.get(str(finish), "end_turn")
                yield _sse("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": usage.get("completion_tokens", 0) or 0}})
                yield _sse("message_stop", {"type": "message_stop"})
                done_sent = True
    finally:
        # 客户端断开 / 外层 aclose 时，内层 chat 生成器（_stream_response）必须
        # 被显式关闭——否则其 finally（含并发闸门 _bump_active(-1)）要等 GC 触发，
        # 高压下表现为假 server_overloaded。见审查条目 R2-M2-6。
        try:
            await chat_iter.aclose()
        except Exception:  # noqa: BLE001
            pass
    if not done_sent:
        # 上游可能一条内容都没吐就结束（空流/立即失败）。此时仍需补一条
        # message_start，否则客户端拿到 message_delta 时还没有 message 对象。
        async for _e in emit_start():
            yield _e
        for idx, btype in opened:
            async for _e in close_block(idx, btype):
                yield _e
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": usage.get("completion_tokens", 0) or 0}})
        yield _sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# /v1/messages/count_tokens：token 估算
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def count_tokens(body: dict) -> int:
    """估算 Anthropic 请求的 input token 数。

    平台没有 Anthropic 官方 tokenizer 依赖，这里用启发式估算：
    - 文本按「4 字符/token + 单词数」近似；
    - 系统提示 + 全部消息内容 + 工具定义都计入。
    估算值仅供计费参考，不影响协议兼容性。
    """
    total = 0

    def _add_text(text: str):
        nonlocal total
        total += len(text) // 4 + len(_WORD_RE.findall(text or ""))

    system = body.get("system")
    if isinstance(system, str):
        _add_text(system)
    elif isinstance(system, list):
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                _add_text(str(b.get("text") or ""))
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            _add_text(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    _add_text(str(part.get("text") or ""))
                elif part.get("type") == "tool_result":
                    _add_text(_tool_result_text(part.get("content")))
    for tool in body.get("tools") or []:
        if isinstance(tool, dict):
            _add_text(str(tool.get("description") or ""))
            _add_text(json.dumps(tool.get("input_schema") or {}, ensure_ascii=False))
    return max(total, 1)
