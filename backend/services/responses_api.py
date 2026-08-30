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

# 两协议同名的通用参数：忠实透传，不做白名单裁剪。
# 依据 OpenAI 官方迁移指南（chat↔responses 字段对照）：
# 仅当目标协议"明确不支持"的字段才不携带（避免 400），其余一律透传。
# chat 独有、responses 已移除的字段：n（多路生成）、frequency/presence_penalty。
# responses 独有、chat 无法表达的字段：instructions/reasoning/previous_response_id/
# truncation/store/include 等，在 responses->chat 方向做归一化或丢弃（见各函数）。
_RESPONSES_COMMON = frozenset({
    "model", "stream", "temperature", "top_p", "stop", "seed", "metadata",
    "user", "parallel_tool_calls", "store",
})
# 仅 responses->chat 方向保留的 chat 独有参数
_CHAT_ONLY = frozenset({"n", "frequency_penalty", "presence_penalty"})

# 推理档位映射：Responses 支持 none/low/medium/high/minimal，chat 侧归一化为
# off/low/medium/high/max
_EFFORT_TO_RESPONSES = {"off": "none", "max": "high"}
_EFFORT_FROM_RESPONSES = {"none": "off", "minimal": "low"}

# finish_reason <-> incomplete_details.reason 双向映射
_FINISH_FROM_RESPONSES = {
    "max_output_tokens": "length",
    "content_filter": "content_filter",
    "function_call": "function_call",
}
_FINISH_TO_RESPONSES = {
    "length": "max_output_tokens",
    "content_filter": "content_filter",
    "function_call": "function_call",
}


def is_responses_url(url: str) -> bool:
    """URL 是否为 Responses API 端点（路径以 /responses 结尾）。"""
    path = (url or "").split("?", 1)[0].rstrip("/")
    return path.endswith("/responses")


# ---------------------------------------------------------------------------
# 结构性字段的忠实映射（tools / tool_choice / response_format / usage）
# ---------------------------------------------------------------------------

def _tools_to_responses(tools) -> list | None:
    """chat tools（function 包在 function 里）-> responses tools（平铺 name）。"""
    if not isinstance(tools, list):
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = t.get("function") or {}
            item = {"type": "function",
                    "name": str(fn.get("name") or ""),
                    "description": str(fn.get("description") or ""),
                    "parameters": fn.get("parameters") or {}}
            if fn.get("strict") is not None:
                item["strict"] = fn["strict"]
            out.append(item)
        else:
            out.append(t)
    return out or None


def _tools_to_chat(tools) -> list | None:
    """responses tools（平铺 name）-> chat tools（function 包在 function 里）。"""
    if not isinstance(tools, list):
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = {"name": str(t.get("name") or ""),
                  "description": str(t.get("description") or ""),
                  "parameters": t.get("parameters") or {}}
            if t.get("strict") is not None:
                fn["strict"] = t["strict"]
            out.append({"type": "function", "function": fn})
        else:
            out.append(t)
    return out or None


def _tool_choice_to_responses(tc):
    """chat tool_choice（function 包在 function 里）-> responses（平铺 name）。"""
    if not isinstance(tc, dict):
        return tc  # "auto" / "none" / "required" 等字符串直接透传
    if tc.get("type") == "function":
        fn = tc.get("function") or {}
        return {"type": "function", "name": str(fn.get("name") or "")}
    return tc


def _tool_choice_to_chat(tc):
    """responses tool_choice（平铺 name）-> chat（function 包在 function 里）。"""
    if not isinstance(tc, dict):
        return tc
    if tc.get("type") == "function":
        return {"type": "function", "function": {"name": str(tc.get("name") or "")}}
    return tc


def _response_format_to_responses(rf):
    """chat response_format -> responses text.format。

    json_schema 细节（name/description/schema/strict）忠实保留：
    chat 的 `{"type":"json_schema","json_schema":{...}}` 对应 responses 的
    `text.format={"type":"json_schema","name":...,"schema":...,...}`（平铺）。
    """
    if not isinstance(rf, dict):
        return None
    ftype = rf.get("type")
    if ftype == "text":
        return None
    out = {"type": ftype or "text"}
    if ftype == "json_schema":
        js = rf.get("json_schema")
        if isinstance(js, dict):
            for key in ("name", "description", "schema", "strict"):
                if key in js:
                    out[key] = js[key]
    return out


def _response_format_to_chat(text):
    """responses text.format -> chat response_format。"""
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict) or fmt.get("type") == "text":
        return None
    if fmt.get("type") == "json_schema":
        return {"type": "json_schema", "json_schema": {
            key: fmt[key] for key in ("name", "description", "schema", "strict")
            if key in fmt}}
    return {"type": fmt.get("type") or "text"}


def _chat_reasoning_to_responses(body: dict) -> dict | None:
    """把 chat 侧思考参数映射为 Responses API 的 reasoning.effort。

    思考参数已由 thinking 服务归一化到顶层 reasoning_effort / chat_template_kwargs，
    这里把 intent 映射成 responses 的 `reasoning: {"effort": ...}`；客户端未表达
    任何思考意图时返回 None（不额外携带，避免改变上游行为）。
    """
    effort = body.get("reasoning_effort")
    thinking_flag = None
    kwargs = body.get("chat_template_kwargs")
    if isinstance(kwargs, dict):
        thinking_flag = kwargs.get("thinking")
        if thinking_flag is None:
            thinking_flag = kwargs.get("enable_thinking")
    if thinking_flag is None:
        thinking_flag = body.get("thinking")
    if thinking_flag is None:
        thinking_flag = body.get("enable_thinking")
    out: dict = {}
    if effort is not None:
        e = str(effort).strip().lower()
        out["effort"] = _EFFORT_TO_RESPONSES.get(e, e)
    if thinking_flag is False and "effort" not in out:
        out["effort"] = "none"
    return out or None


def _refusal_from_output(output) -> str:
    """从 responses output 的 message 条目中提取 refusal 文本。"""
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "refusal":
                    return str(part.get("refusal") or "")
    return ""


def _usage_to_responses(u: dict) -> dict:
    """chat usage（prompt/completion_tokens）-> responses（input/output_tokens）。"""
    out = dict(u)
    if "prompt_tokens" in out and "input_tokens" not in out:
        out["input_tokens"] = out.pop("prompt_tokens")
    if "completion_tokens" in out and "output_tokens" not in out:
        out["output_tokens"] = out.pop("completion_tokens")
    return out


def _usage_to_chat(u: dict) -> dict:
    """responses usage（input/output_tokens）-> chat（prompt/completion_tokens）。"""
    out = dict(u)
    if "input_tokens" in out and "prompt_tokens" not in out:
        out["prompt_tokens"] = out.pop("input_tokens")
    if "output_tokens" in out and "completion_tokens" not in out:
        out["completion_tokens"] = out.pop("output_tokens")
    return out


# ---------------------------------------------------------------------------
# 请求体：chat -> responses
# ---------------------------------------------------------------------------

def chat_to_responses_body(body: dict) -> dict:
    """把 chat/completions 请求体转成 Responses API 请求体。

    同名通用参数直接透传；仅对协议结构不同的字段做映射
    （messages->input、max_tokens->max_output_tokens、tools/tool_choice/
    response_format/text.format、思考参数->reasoning.effort），
    并跳过 responses 明确移除的 chat 独有参数（n、penalties），避免 400。
    """
    out: dict[str, Any] = {}
    for key in _RESPONSES_COMMON:
        if key in body and body[key] is not None:
            out[key] = body[key]
    tools = _tools_to_responses(body.get("tools"))
    if tools is not None:
        out["tools"] = tools
    tool_choice = _tool_choice_to_responses(body.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice
    reasoning = _chat_reasoning_to_responses(body)
    if reasoning:
        out["reasoning"] = reasoning
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    if max_tokens is not None:
        out["max_output_tokens"] = max_tokens
    messages = body.get("messages")
    if isinstance(messages, list):
        items: list[dict] = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") in ("assistant", "function") \
                    and m.get("tool_calls"):
                items.extend(_message_with_tool_calls_to_items(m))
            else:
                items.append(_message_to_item(m))
        out["input"] = items
    rf = _response_format_to_responses(body.get("response_format"))
    if rf:
        out["text"] = {"format": rf}
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


def _reasoning_summary_text(item: dict) -> str:
    """从 Responses 推理条目中提取明文摘要文本（summary 字段）。"""
    summary = item.get("summary")
    if not isinstance(summary, list):
        return ""
    parts = []
    for s in summary:
        if isinstance(s, dict) and s.get("type") in ("summary_text", "text"):
            parts.append(str(s.get("text") or ""))
        elif isinstance(s, str):
            parts.append(s)
    return "".join(parts)


def _reasoning_text(item: dict) -> str:
    """提取推理条目的可透传内容：优先明文摘要，否则原样透传加密内容。

    上游对推理内容加密（encrypted_content）时，不替换、不解密，原样放在
    reasoning_content 中，由下游（拥有解密能力的一方）自行处理。
    """
    text = _reasoning_summary_text(item)
    if text:
        return text
    return str(item.get("encrypted_content") or "")


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
    # developer role 是 Responses API 原生角色，忠实透传，不改写为 system
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
                item = {"type": "input_image",
                        "image_url": str(img.get("url") or "")}
                # 忠实保留 detail（low/high/auto），不丢失图像分辨率意图
                if img.get("detail") is not None:
                    item["detail"] = img["detail"]
                parts.append(item)
        if not parts:
            parts = [{"type": text_type, "text": _content_to_text(content)}]
        result: dict = {"type": "message", "role": role, "content": parts}
        return result
    return {"type": "message", "role": role,
            "content": [{"type": text_type, "text": _content_to_text(content)}]}


def _message_with_tool_calls_to_items(msg: dict) -> list[dict]:
    """assistant 消息携带 tool_calls 时，转成 message + 逐个 function_call 条目。

    Responses API 的 input 里，assistant 的 function_call 必须是独立条目
    （type=function_call），否则后续 role=tool 的 function_call_output 引用的
    call_id 无对应定义，上游会报错或丢失工具结果。
    """
    role = str(msg.get("role") or "user")
    content = msg.get("content")
    text_type = "output_text" if role == "assistant" else "input_text"
    # 正文部分
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _content_to_text(content)
    else:
        text = str(content or "")
    items: list[dict] = []
    if text:
        items.append({"type": "message", "role": role,
                      "content": [{"type": text_type, "text": text}]})
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        items.append({
            "type": "function_call",
            "call_id": str(tc.get("id") or ""),
            "name": str(fn.get("name") or ""),
            "arguments": str(fn.get("arguments") or "{}"),
        })
    return items


# ---------------------------------------------------------------------------
# 非流式响应：responses -> chat
# ---------------------------------------------------------------------------

def responses_payload_to_chat(payload: dict) -> dict:
    """把 Responses API 的非流式响应转成 chat.completion 结构。

    忠实映射：
    - output message/reasoning/function_call 条目 -> message.content /
      reasoning_content / tool_calls；
    - incomplete_details.reason -> finish_reason（max_output_tokens->length 等）；
    - refusal 内容 -> message.refusal；
    - usage 键名 input/output_tokens -> prompt/completion_tokens。
    """
    text = ""
    reasoning = ""
    tool_calls: list[dict] = []
    output = payload.get("output")
    if not isinstance(output, list):
        output = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            text += _content_to_text(item.get("content"))
        elif item.get("type") == "reasoning":
            reasoning += _reasoning_text(item)
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
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    refusal = _refusal_from_output(output)
    if refusal:
        message["refusal"] = refusal
    inc = payload.get("incomplete_details") or {}
    reason = inc.get("reason")
    if tool_calls:
        finish_reason = "tool_calls"
    elif reason:
        finish_reason = _FINISH_FROM_RESPONSES.get(str(reason), "stop")
    else:
        finish_reason = "stop"
    usage = _usage_to_chat(payload.get("usage") or {})
    return {
        "id": str(payload.get("id") or "resp_x"),
        "object": "chat.completion",
        "created": int(payload.get("created_at") or time.time()),
        "model": str(payload.get("model") or ""),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# 客户端方向：/v1/responses 入口
# ---------------------------------------------------------------------------

def responses_to_chat_body(rb: dict) -> dict:
    """把客户端 Responses API 请求体转成内部 chat/completions 请求体。

    `/v1/responses` 入口用它归一化后复用整套竞速/重试/日志链路。
    同名通用参数直接透传；仅做结构性映射（input->messages、
    max_output_tokens->max_tokens、instructions->system 消息、
    reasoning.effort->reasoning_effort、tools/tool_choice/text.format 的
    包装差异），responses 独有而 chat 无法表达的字段（previous_response_id、
    truncation、include 等）按协议限制不携带。
    """
    out: dict[str, Any] = {}
    for key in _RESPONSES_COMMON | _CHAT_ONLY:
        if key in rb and rb[key] is not None:
            out[key] = rb[key]
    tools = _tools_to_chat(rb.get("tools"))
    if tools is not None:
        out["tools"] = tools
    tool_choice = _tool_choice_to_chat(rb.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice
    reasoning = rb.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort is not None:
            e = str(effort).strip().lower()
            out["reasoning_effort"] = _EFFORT_FROM_RESPONSES.get(e, e)
        # reasoning.summary / reasoning.budget 等 chat 无法表达的字段不携带
    if rb.get("max_output_tokens") is not None:
        out["max_tokens"] = rb["max_output_tokens"]
    messages: list = []
    instructions = rb.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = rb.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        messages.extend(m for m in (_input_item_to_message(it) for it in inp)
                        if m is not None)
    rf = _response_format_to_chat(rb.get("text"))
    if rf:
        out["response_format"] = rf
    if messages:
        out["messages"] = messages
    return out


def _input_item_to_message(item):
    """把一条 Responses input 条目转成 chat 消息；无需回传的条目返回 None。"""
    if not isinstance(item, dict):
        return {"role": "user", "content": str(item)}
    itype = item.get("type")
    if itype == "function_call_output":
        return {"role": "tool", "tool_call_id": str(item.get("call_id") or ""),
                "content": str(item.get("output") or "")}
    if itype == "function_call":
        # assistant 历史工具调用 -> chat assistant tool_calls（勿误转成 user）
        return {"role": "assistant", "content": "",
                "tool_calls": [{
                    "id": str(item.get("call_id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    },
                }]}
    if itype == "reasoning":
        # 推理条目是服务端产物，chat 历史无对应结构，不回传
        return None
    if itype == "message" or itype is None:
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") in ("input_text", "output_text"):
                    parts.append({"type": "text", "text": str(p.get("text") or "")})
                elif isinstance(p, dict) and p.get("type") == "input_image":
                    img = {"url": str(p.get("image_url") or "")}
                    if p.get("detail") is not None:
                        img["detail"] = p["detail"]
                    parts.append({"type": "image_url", "image_url": img})
            content = parts
        return {"role": role, "content": content}
    return {"role": "user", "content": str(item)}


def chat_to_responses_payload(chat: dict, echo: dict | None = None) -> dict:
    """把内部 chat.completion 非流式响应转成 Responses API 响应。

    `echo` 为原始 Responses 请求体：OpenAI 的 response 对象会回显请求参数
    （instructions/tools/parallel_tool_calls/reasoning 等），一并回写保证结构完整。
    status/incomplete_details 由 finish_reason 忠实映射（length->max_output_tokens 等）。
    """
    choices = chat.get("choices") or []
    output: list[dict] = []
    finish = choices[0].get("finish_reason") if choices else None
    if choices:
        ch = choices[0]
        msg = ch.get("message") or {}
        text = _content_to_text(msg.get("content"))
        reasoning = msg.get("reasoning_content")
        tool_calls = msg.get("tool_calls") or []
        if reasoning:
            output.append({"type": "reasoning",
                           "summary": [{"type": "summary_text",
                                        "text": str(reasoning)}]})
        refusal = msg.get("refusal")
        if text:
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": text}]})
        elif refusal:
            # 有拒绝内容时以 refusal 条目表达，不额外发空文本消息
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "refusal", "refusal": str(refusal)}]})
        elif not tool_calls:
            # 纯空回复（无正文/工具/拒绝）时保留 assistant 空消息占位
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": ""}]})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            output.append({"type": "function_call",
                           "call_id": str(tc.get("id") or ""),
                           "name": str(fn.get("name") or ""),
                           "arguments": str(fn.get("arguments") or "")})
    if finish == "stop" or finish == "tool_calls":
        status = "completed"
    else:
        status = "incomplete"
    resp: dict = {
        "id": str(chat.get("id") or "resp_x"),
        "object": "response",
        "created_at": int(chat.get("created", 0) or time.time()),
        "status": status,
        "model": str(chat.get("model") or ""),
        "output": output,
        "usage": _usage_to_responses(chat.get("usage") or {}),
    }
    if finish in _FINISH_TO_RESPONSES:
        resp["incomplete_details"] = {"reason": _FINISH_TO_RESPONSES[finish]}
    if echo and isinstance(echo, dict):
        # 回显请求参数，保持 response 对象与 OpenAI 结构一致
        for key in ("instructions", "previous_response_id", "tools",
                    "parallel_tool_calls", "truncation", "metadata", "store",
                    "user", "reasoning", "include", "max_output_tokens",
                    "temperature", "top_p"):
            if key in echo and echo[key] is not None:
                resp[key] = echo[key]
    return resp


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


def _translate_event(line: str, state: dict | None = None) -> str | None:
    """把一条 Responses 流式事件转成 chat 格式 SSE data 内容；无关事件返回 None。

    `state` 为跨事件的可变字典（`args_seen` 记录已按增量透传过参数的工具
    call_id），用于避免 output_item.done 重复追加完整参数。
    """
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
    if state is None:
        state = {}
    args_seen = state.setdefault("args_seen", set())
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
    # 推理内容：OpenAI 标准摘要增量事件
    if etype == "response.reasoning_summary_text.delta":
        return json.dumps({"choices": [{"index": 0,
                                        "delta": {"reasoning_content": str(data.get("delta") or "")},
                                        "finish_reason": None}]})
    if etype == "response.output_item.added":
        item = data.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("type") == "function_call":
            # 工具开始：先发 id + name，参数随后按增量透传
            return json.dumps({"choices": [{"index": 0,
                                            "delta": {"tool_calls": [{
                                                "index": 0,
                                                "id": str(item.get("call_id") or ""),
                                                "type": "function",
                                                "function": {
                                                    "name": str(item.get("name") or ""),
                                                    "arguments": "",
                                                },
                                            }]},
                                            "finish_reason": None}]})
        # 推理条目开始：此时通常只有空 summary，不产生内容；待 done 时汇总
        return None
    if etype == "response.output_text.delta":
        return json.dumps({"choices": [{"index": 0,
                                        "delta": {"content": str(data.get("delta") or "")},
                                        "finish_reason": None}]})
    if etype == "response.function_call_arguments.delta":
        call_id = str(data.get("item_id") or "")
        args_seen.add(call_id)
        return json.dumps({"choices": [{"index": 0,
                                        "delta": {"tool_calls": [{
                                            "index": 0,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {
                                                "name": None,
                                                "arguments": str(data.get("delta") or ""),
                                            },
                                        }]},
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
        if not isinstance(item, dict):
            return None
        if item.get("type") == "reasoning":
            text = _reasoning_text(item)
            if text:
                return json.dumps({"choices": [{"index": 0,
                                                "delta": {"reasoning_content": text},
                                                "finish_reason": None}]})
            return None
        if item.get("type") == "function_call":
            call_id = str(item.get("call_id") or "")
            # 参数已按增量透传（标准实现），done 不再重复追加完整参数；
            # 若上游只发 done 未发 delta（非标准），则在此兜底补发一次完整参数
            if call_id in args_seen:
                return None
            return json.dumps({"choices": [{"index": 0,
                                            "delta": {"tool_calls": [{
                                                "index": 0,
                                                "id": call_id,
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
        inc = (data.get("response") or {}).get("incomplete_details") or {}
        reason = inc.get("reason")
        finish = _FINISH_FROM_RESPONSES.get(str(reason), "stop") if reason else "stop"
        return json.dumps({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": finish}], "usage": usage})
    return None


async def iter_responses_sse(first_line: str, aiter,
                             include_first: bool = True) -> AsyncIterator[str]:
    """把 Responses 流式事件流转成 chat 格式的 SSE 行序列（含结尾 [DONE]）。"""
    state: dict = {"args_seen": set()}
    if include_first and first_line:
        translated = _translate_event(first_line, state)
        if translated == "[DONE]":
            yield "data: [DONE]\n\n"
            return
        if translated:
            yield "data: " + translated + "\n\n"
    saw_done = False
    async for line in aiter:
        if not line.strip():
            continue
        translated = _translate_event(line, state)
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
    """把内部 chat 格式的 SSE 行流转成 Responses API SSE 事件流。

    忠实补全 Responses 生命周期：created/in_progress、各 output_item 的
    added/done（reasoning / message / function_call）、增量 delta 事件，
    最后 completed + [DONE]。
    """
    emitted_created = False
    done_sent = False
    usage: dict = {}
    reasoning_acc = ""
    content_acc = ""
    tool_args: dict[str, str] = {}
    tool_names: dict[str, str] = {}
    # 已宣告过的条目 id（用于 output_item.added 去重）
    announced: set[str] = set()
    message_item_id = "msg_0"

    def announce(item: dict) -> Iterator[str]:
        iid = item.get("id")
        if iid in announced:
            return
        announced.add(iid)
        yield _sse_event("response.output_item.added",
                         {"type": "response.output_item.added",
                          "output_index": 0, "item": item})

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
        rid = data.get("id") or "resp_x"
        if not emitted_created:
            emitted_created = True
            yield _sse_event("response.created", {
                "type": "response.created",
                "response": {"id": rid, "object": "response",
                             "status": "in_progress", "model": data.get("model") or ""},
            })
            yield _sse_event("response.in_progress", {
                "type": "response.in_progress",
                "response": {"id": rid, "object": "response",
                             "status": "in_progress", "model": data.get("model") or ""},
            })
        delta = ch.get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if reasoning:
            reasoning_acc += str(reasoning)
            yield from announce({"id": "rs_0", "type": "reasoning",
                                 "status": "in_progress", "summary": []})
            yield _sse_event("response.reasoning_summary_text.delta", {
                "type": "response.reasoning_summary_text.delta", "output_index": 0,
                "delta": str(reasoning),
            })
        content = delta.get("content")
        if content:
            content_acc += str(content)
            yield from announce({"id": message_item_id, "type": "message",
                                 "role": "assistant", "status": "in_progress",
                                 "content": []})
            yield _sse_event("response.output_text.delta", {
                "type": "response.output_text.delta", "output_index": 0,
                "delta": str(content),
            })
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                iid = str(tc.get("id") or "")
                fn = tc.get("function") or {}
                if iid and iid not in announced:
                    yield from announce({"id": iid, "type": "function_call",
                                         "status": "in_progress", "call_id": iid,
                                         "name": str(fn.get("name") or ""),
                                         "arguments": ""})
                if iid:
                    if fn.get("name"):
                        tool_names[iid] = str(fn["name"])
                    args = str(fn.get("arguments") or "")
                    tool_args[iid] = tool_args.get(iid, "") + args
                    yield _sse_event("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0, "item_id": iid,
                        "delta": args,
                    })
        if data.get("usage"):
            usage = data["usage"]
        finish = ch.get("finish_reason")
        if finish:
            if "rs_0" in announced:
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": "rs_0", "type": "reasoning",
                             "status": "completed", "summary": []},
                })
            if message_item_id in announced:
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "output_index": 0,
                    "text": content_acc, "item_id": message_item_id,
                })
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": message_item_id, "type": "message",
                             "role": "assistant", "status": "completed",
                             "content": [{"type": "output_text", "text": content_acc}]},
                })
            for iid in announced:
                if iid not in ("rs_0", message_item_id):
                    yield _sse_event("response.output_item.done", {
                        "type": "response.output_item.done", "output_index": 0,
                        "item": {"id": iid, "type": "function_call",
                                 "status": "completed", "call_id": iid,
                                 "name": tool_names.get(iid, ""),
                                 "arguments": tool_args.get(iid, "")},
                    })
            if finish == "stop" or finish == "tool_calls":
                status = "completed"
            else:
                status = "incomplete"
            completed: dict = {
                "type": "response.completed",
                "response": {"id": rid, "object": "response",
                             "status": status, "model": data.get("model") or "",
                             "usage": usage},
            }
            if finish in _FINISH_TO_RESPONSES:
                completed["response"]["incomplete_details"] = {
                    "reason": _FINISH_TO_RESPONSES[finish]}
            yield _sse_event("response.completed", completed)
    if not done_sent:
        yield "data: [DONE]\n\n"
