"""OpenAI Responses API（`/v1/responses`）与 Chat Completions 格式互转。

某些模型（如 muse-spark-1.2-contributor-free）在上游走 Responses API 端点：
请求体用 `input` 而非 `messages`、`max_output_tokens` 而非 `max_tokens`；
流式事件是 `response.output_text.delta` 一类的对象事件，与 chat SSE 完全不同。

本模块同时提供两个方向的转换：

- 上游方向（平台内部 chat 格式 <-> 上游 Responses 端点）：竞速引擎统一以
  chat 格式处理，路由端点若为 /responses 则自动转换请求/响应/SSE 事件；
- 客户端方向（`/v1/responses` 入口）：把客户端 Responses 请求体转成内部
  chat 请求体，再把内部 chat 结果转回 Responses 响应/SSE 事件流。

这样上游差异与客户端协议差异对整个调用链（竞速、重试、日志）完全透明。
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Iterator

# Responses API 支持并需透传的顶层参数白名单（chat 独有参数不下发，避免 400；
# 实测某些上游会拒绝 seed/stop/tools 等，故只保留通用参数）
_RESPONSES_ALLOWED = frozenset({
    "model", "stream", "temperature", "top_p",
})


def is_responses_url(url: str) -> bool:
    """URL 是否为 Responses API 端点（路径以 /responses 结尾）。"""
    path = (url or "").split("?", 1)[0].rstrip("/")
    return path.endswith("/responses")


# ---------------------------------------------------------------------------
# 请求体：chat -> responses
# ---------------------------------------------------------------------------

def chat_to_responses_body(body: dict) -> dict:
    """把 chat/completions 请求体转成 Responses API 请求体。"""
    out: dict[str, Any] = {}
    for key in _RESPONSES_ALLOWED:
        if key in body and body[key] is not None:
            out[key] = body[key]
    if body.get("max_tokens") is not None:
        out["max_output_tokens"] = body["max_tokens"]
    messages = body.get("messages")
    if isinstance(messages, list):
        out["input"] = [_message_to_item(m) for m in messages]
    return out


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def _message_to_item(msg) -> dict:
    """把一条 chat 消息转成 Responses input 条目。"""
    if not isinstance(msg, dict):
        return {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": str(msg)}]}
    role = str(msg.get("role") or "user")
    content = msg.get("content")
    if role in ("tool", "function"):
        return {"type": "function_call_output",
                "call_id": str(msg.get("tool_call_id") or msg.get("name") or ""),
                "output": _content_to_text(content)}
    if role == "developer":
        role = "system"
    # 上游 Responses 端点只接受 assistant 消息使用 output_text 内容类型
    # （input_text 会 400：content type is not valid on assistant messages）
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return {"type": "message", "role": role,
                "content": [{"type": text_type, "text": content}]}
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "output_text", "input_text"):
                parts.append({"type": text_type, "text": str(part.get("text") or "")})
            elif ptype == "image_url":
                img = part.get("image_url") or {}
                parts.append({"type": "input_image",
                              "image_url": str(img.get("url") or "")})
        return {"type": "message", "role": role, "content": parts}
    return {"type": "message", "role": role,
            "content": [{"type": text_type, "text": _content_to_text(content)}]}


# ---------------------------------------------------------------------------
# 非流式响应：responses -> chat
# ---------------------------------------------------------------------------

def responses_payload_to_chat(payload: dict) -> dict:
    """把 Responses API 的非流式响应转成 chat.completion 结构。"""
    text = ""
    tool_calls: list[dict] = []
    output = payload.get("output")
    if not isinstance(output, list):
        output = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            text += _content_to_text(item.get("content"))
        elif item.get("type") == "function_call":
            tool_calls.append({
                "id": str(item.get("call_id") or ""),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or ""),
                },
            })
    # 部分实现提供顶层 output_text 快捷字段
    if not text and isinstance(payload.get("output_text"), str):
        text = payload["output_text"]
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = payload.get("usage") or {}
    return {
        "id": str(payload.get("id") or "resp_x"),
        "object": "chat.completion",
        "created": int(payload.get("created_at") or time.time()),
        "model": str(payload.get("model") or ""),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# 客户端方向：/v1/responses 入口
# ---------------------------------------------------------------------------

def responses_to_chat_body(rb: dict) -> dict:
    """把客户端 Responses API 请求体转成内部 chat/completions 请求体。

    `/v1/responses` 入口用它归一化后复用整套竞速/重试/日志链路。
    """
    out: dict[str, Any] = {}
    for key in ("model", "stream", "temperature", "top_p"):
        if key in rb and rb[key] is not None:
            out[key] = rb[key]
    if rb.get("max_output_tokens") is not None:
        out["max_tokens"] = rb["max_output_tokens"]
    inp = rb.get("input")
    if isinstance(inp, str):
        out["messages"] = [{"role": "user", "content": inp}]
    elif isinstance(inp, list):
        out["messages"] = [_input_item_to_message(it) for it in inp]
    return out


def _input_item_to_message(item) -> dict:
    """把一条 Responses input 条目转成 chat 消息。"""
    if not isinstance(item, dict):
        return {"role": "user", "content": str(item)}
    itype = item.get("type")
    if itype == "function_call_output":
        return {"role": "tool", "tool_call_id": str(item.get("call_id") or ""),
                "content": str(item.get("output") or "")}
    if itype == "message" or itype is None:
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") in ("input_text", "output_text"):
                    parts.append({"type": "text", "text": str(p.get("text") or "")})
                elif isinstance(p, dict) and p.get("type") == "input_image":
                    parts.append({"type": "image_url", "image_url": {
                        "url": str(p.get("image_url") or "")}})
            content = parts
        return {"role": role, "content": content}
    return {"role": "user", "content": str(item)}


def chat_to_responses_payload(chat: dict) -> dict:
    """把内部 chat.completion 非流式响应转成 Responses API 响应。"""
    choices = chat.get("choices") or []
    output: list[dict] = []
    if choices:
        ch = choices[0]
        msg = ch.get("message") or {}
        text = _content_to_text(msg.get("content"))
        tool_calls = msg.get("tool_calls") or []
        if text or not tool_calls:
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": text}]})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            output.append({"type": "function_call",
                           "call_id": str(tc.get("id") or ""),
                           "name": str(fn.get("name") or ""),
                           "arguments": str(fn.get("arguments") or "")})
    status = "incomplete"
    if choices and choices[0].get("finish_reason") == "stop":
        status = "completed"
    return {
        "id": str(chat.get("id") or "resp_x"),
        "object": "response",
        "created_at": int(chat.get("created", 0) or time.time()),
        "status": status,
        "model": str(chat.get("model") or ""),
        "output": output,
        "usage": chat.get("usage") or {},
    }


# ---------------------------------------------------------------------------
# 流式事件：responses SSE -> chat SSE
# ---------------------------------------------------------------------------

def parse_stream_event(line: str) -> dict | None:
    """解析 Responses 流式 SSE 事件；非法/错误事件返回 None。

    竞速阶段用它判断"第一条有效事件"：只要不是错误事件即视为有效输出。
    """
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return {}
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    if data.get("type") in ("error", "response.failed"):
        return None
    if data.get("error"):
        return None
    return data


def _translate_event(line: str) -> str | None:
    """把一条 Responses 流式事件转成 chat 格式 SSE data 内容；无关事件返回 None。"""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "[DONE]"
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    etype = data.get("type")
    if etype in ("error", "response.failed"):
        err = data.get("error") or {}
        message = err.get("message") if isinstance(err, dict) else str(err)
        return json.dumps({
            "error": {"message": message or "upstream error", "type": "api_error",
                      "param": None, "code": "upstream_error"},
        })
    if etype == "response.created":
        # 只发一次角色标记，不包含正文，避免干扰重试判断
        return json.dumps({"choices": [{"index": 0, "delta": {"role": "assistant",
                                                              "content": ""},
                                        "finish_reason": None}]})
    if etype == "response.output_text.delta":
        return json.dumps({"choices": [{"index": 0,
                                        "delta": {"content": str(data.get("delta") or "")},
                                        "finish_reason": None}]})
    if etype == "response.output_text.done":
        # 部分实现把 usage 挂在该事件上
        usage = data.get("usage")
        if usage:
            return json.dumps({"choices": [{"index": 0, "delta": {},
                                            "finish_reason": "stop"}], "usage": usage})
        return None
    if etype == "response.output_item.done":
        item = data.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            return json.dumps({"choices": [{"index": 0,
                                            "delta": {"tool_calls": [{
                                                "index": 0,
                                                "id": str(item.get("call_id") or ""),
                                                "type": "function",
                                                "function": {
                                                    "name": str(item.get("name") or ""),
                                                    "arguments": str(item.get("arguments") or ""),
                                                },
                                            }]},
                                            "finish_reason": None}]})
        return None
    if etype == "response.completed":
        usage = (data.get("response") or {}).get("usage") or {}
        return json.dumps({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": "stop"}], "usage": usage})
    return None


async def iter_responses_sse(first_line: str, aiter,
                             include_first: bool = True) -> AsyncIterator[str]:
    """把 Responses 流式事件流转成 chat 格式的 SSE 行序列（含结尾 [DONE]）。"""
    if include_first and first_line:
        translated = _translate_event(first_line)
        if translated == "[DONE]":
            yield "data: [DONE]\n\n"
            return
        if translated:
            yield "data: " + translated + "\n\n"
    saw_done = False
    async for line in aiter:
        if not line.strip():
            continue
        translated = _translate_event(line)
        if translated is None:
            continue
        if translated == "[DONE]":
            saw_done = True
            yield "data: [DONE]\n\n"
        else:
            yield "data: " + translated + "\n\n"
    if not saw_done:
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 流式事件：chat SSE -> responses SSE（/v1/responses 出口）
# ---------------------------------------------------------------------------

def _sse_event(name: str, obj: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


def iter_chat_sse_as_responses(chat_iter: Iterator[str]) -> Iterator[str]:
    """把内部 chat 格式的 SSE 行流转成 Responses API SSE 事件流。"""
    emitted_created = False
    done_sent = False
    usage: dict = {}
    for chunk in chat_iter:
        if not chunk.startswith("data:"):
            continue
        payload = chunk[5:].strip().rstrip("\n")
        if payload == "[DONE]":
            if not done_sent:
                yield "data: [DONE]\n\n"
                done_sent = True
            return
        try:
            data = json.loads(payload)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            yield _sse_event("response.failed",
                             {"type": "response.failed", "error": data["error"]})
            continue
        choices = data.get("choices") or []
        if not choices:
            continue
        ch = choices[0]
        if not emitted_created:
            emitted_created = True
            yield _sse_event("response.created", {
                "type": "response.created",
                "response": {"id": data.get("id") or "resp_x", "object": "response",
                             "status": "in_progress", "model": data.get("model") or ""},
            })
        delta = ch.get("delta") or {}
        if delta.get("content"):
            yield _sse_event("response.output_text.delta", {
                "type": "response.output_text.delta", "output_index": 0,
                "delta": str(delta["content"]),
            })
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                fn = tc.get("function") or {}
                yield _sse_event("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0, "item_id": str(tc.get("id") or ""),
                    "delta": str(fn.get("arguments") or ""),
                })
        if data.get("usage"):
            usage = data["usage"]
        finish = ch.get("finish_reason")
        if finish:
            status = "completed" if finish == "stop" else "incomplete"
            yield _sse_event("response.completed", {
                "type": "response.completed",
                "response": {"id": data.get("id") or "resp_x", "object": "response",
                             "status": status, "model": data.get("model") or "",
                             "usage": usage},
            })
    if not done_sent:
        yield "data: [DONE]\n\n"
