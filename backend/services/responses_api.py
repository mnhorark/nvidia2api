"""OpenAI Responses API（`/v1/responses`）与 Chat Completions 格式互转。

某些模型（如 muse-spark-1.2-contributor-free）在上游走 Responses API 端点：
请求体用 `input` 而非 `messages`、`max_output_tokens` 而非 `max_tokens`；
流式事件是 `response.output_text.delta` 一类的对象事件，与 chat SSE 完全不同。

本模块把平台内部的 chat 格式转成 Responses 请求，再把 Responses 的响应/流式
事件转回 chat 格式，使上游差异对整个调用链（竞速、重试、日志）完全透明。
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

# Responses API 支持并需透传的顶层参数白名单（chat 独有参数不下发，避免 400）
_RESPONSES_ALLOWED = frozenset({
    "model", "stream", "temperature", "top_p", "stop", "seed",
    "tools", "tool_choice", "response_format", "reasoning",
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
    if isinstance(content, str):
        return {"type": "message", "role": role,
                "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                parts.append({"type": "input_text", "text": str(part.get("text") or "")})
            elif ptype == "image_url":
                img = part.get("image_url") or {}
                parts.append({"type": "input_image",
                              "image_url": str(img.get("url") or "")})
        return {"type": "message", "role": role, "content": parts}
    return {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": _content_to_text(content)}]}


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
