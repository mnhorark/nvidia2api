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
import logging
from typing import Any, AsyncIterator, Iterator

logger = logging.getLogger("nvidia2api.responses")

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

    使用 thinking.parse() 归一化思考意图，支持所有客户端格式：
    - reasoning_effort: "high"
    - reasoning: {effort: "high"}
    - thinking: {type: "enabled", budget_tokens: 8000}
    - extra_body.openai.reasoning_effort: "high"
    等任意嵌套格式。
    """
    from services import thinking
    spec = thinking.parse(body)
    if not spec.is_set():
        return None
    out: dict = {}
    if spec.enabled is False:
        out["effort"] = "none"
    elif spec.enabled is True:
        if spec.effort:
            e = str(spec.effort).strip().lower()
            out["effort"] = _EFFORT_TO_RESPONSES.get(e, e)
        else:
            out["enabled"] = True
    elif spec.effort:
        e = str(spec.effort).strip().lower()
        out["effort"] = _EFFORT_TO_RESPONSES.get(e, e)
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
    """responses usage（input/output_tokens）-> chat（prompt/completion_tokens）。

    同时把明细字段换成 chat 侧的键名（input_tokens_details.cached_tokens ->
    prompt_tokens_details.cached_tokens），让缓存命中统计在两条协议下一致。
    `total_tokens` 缺失时按输入输出求和补齐。
    """
    out = dict(u or {})
    if "input_tokens" in out and "prompt_tokens" not in out:
        out["prompt_tokens"] = out.pop("input_tokens")
    if "output_tokens" in out and "completion_tokens" not in out:
        out["completion_tokens"] = out.pop("output_tokens")
    details = out.pop("input_tokens_details", None)
    if isinstance(details, dict) and "prompt_tokens_details" not in out:
        out["prompt_tokens_details"] = details
    if not out.get("total_tokens"):
        out["total_tokens"] = (out.get("prompt_tokens") or 0) + (
            out.get("completion_tokens") or 0)
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
    if reasoning is None:
        # 实测（zen/muse-spark 矩阵回归）：思考型模型走 Responses 端点时，
        # 若请求完全不带 reasoning 字段，上游只回 encrypted_content 密文块、
        # 不发明文 summary——客户端看到的就只剩一坨解不开的乱码。参照
        # RikkaHub/opencode 的默认行为：凡识别为思考能力的模型，自动补
        # {"summary": "auto"}，上游即会流出明文 summary 事件。
        from services import thinking as _thinking
        if _thinking.is_known_thinking_model(body.get("model") or ""):
            reasoning = {"summary": "auto"}
    if reasoning:
        out["reasoning"] = reasoning
        # 参考 RikkaHub：推理模型默认 summary auto，muse-spark-1.2 等 Responses 端点
        # 需同时携带 include，服务端才会返回 reasoning.encrypted_content 与 summary
        if "summary" not in out["reasoning"]:
            out["reasoning"]["summary"] = "auto"
        # 参考 RikkaHub：推理模型默认 summary auto，muse-spark-1.2 等 Responses 端点
        # 需同时携带 include，服务端才会返回 reasoning.encrypted_content 与 summary
        if "include" not in body and "include" not in out:
            out["include"] = ["reasoning.encrypted_content"]
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


def _reasoning_text(item: dict, decrypt_reasoning: bool = False) -> str:
    """提取推理条目的可透传内容：优先明文摘要，无明文时按开关尝试解密密文。

    参考 RikkaHub 的 Responses 协议处理：
    - summary 为明文摘要（summary_text 数组），可直接展示
    - encrypted_content 为 Fernet 密文（gAAAA…）。**默认不解密**：上游
      自有密钥加密时本端无钥可解，解密只会空转；密文原样透传（RikkaHub
      保存为 OpenAIReasoningMetadata 原样回传），供多轮续写。仅当
      stream_reasoning_decrypt 开启（上游用本端已知密钥加密）才尝试还原。
    """
    text = _reasoning_summary_text(item)
    if text:
        return text
    enc = item.get("encrypted_content")
    if isinstance(enc, str) and enc:
        if decrypt_reasoning:
            try:
                from services.reasoning_decrypt import decrypt_token
                dec = decrypt_token(enc)
                if dec is not None:
                    return dec
            except Exception:
                pass
        # 零丢失原则：解不开（密钥不在本端 / 非 Fernet 自有封装）也
        # **原样透传**——它仅供多轮回传（RikkaHub 的
        # OpenAIReasoningMetadata 同样原样保存），丢弃会破坏上游会话
        # 续写回路。展示价值交给客户端自行裁决。
        return enc
    return ""


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

def responses_payload_to_chat(payload: dict, decrypt_reasoning: bool = False) -> dict:
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
            # 参考 RikkaHub 的 parseResponseOutput：优先 summary 明文；
            # 密文按 decrypt_reasoning 开关决定是否尝试解密（默认原样透传）
            reasoning += _reasoning_text(item, decrypt_reasoning)
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

    无损原则：同名透传 + 结构性映射（input->messages、max_output_tokens->
    max_tokens、instructions->system、reasoning.effort->reasoning_effort、
    tools/text 包装），其余未知顶层字段一律保留（forward-compat）。

    例外——Responses 会话态字段必须剔除（docstring 声明与实现曾互相矛盾，
    旧实现的"保留未知字段"循环把这些字段原样漏进 chat 上游）：
    - previous_response_id：服务端会话存储引用，本平台无状态代理不实现
      （每轮全量重放 input），带着它只会造成 400 或静默错误语义；
    - truncation / include：Responses 独有参数，chat 协议无法表达。
    """
    out: dict[str, Any] = {}
    for key in _RESPONSES_COMMON | _CHAT_ONLY:
        if key in rb and rb[key] is not None:
            out[key] = rb[key]
    # 保留未知顶层字段（Responses 未来新增字段无损透传）
    _RESPONSES_STRUCTURAL = frozenset({
        "tools", "tool_choice", "reasoning", "max_output_tokens",
        "instructions", "input", "text",
    })
    # Responses 会话态/独有字段：进入 chat 上游前剔除（见 docstring）
    _RESPONSES_SESSION_ONLY = frozenset({
        "previous_response_id", "truncation", "include",
    })
    for key, val in rb.items():
        if key in _RESPONSES_SESSION_ONLY:
            continue
        if key not in _RESPONSES_COMMON and key not in _CHAT_ONLY \
                and key not in _RESPONSES_STRUCTURAL and val is not None:
            out[key] = val
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
                if not isinstance(p, dict):
                    parts.append({"type": "text", "text": str(p)})
                    continue
                ptype = p.get("type")
                if ptype in ("input_text", "output_text"):
                    parts.append({"type": "text", "text": str(p.get("text") or "")})
                elif ptype == "input_image":
                    img = {"url": str(p.get("image_url") or "")}
                    if p.get("detail") is not None:
                        img["detail"] = p["detail"]
                    parts.append({"type": "image_url", "image_url": img})
                elif ptype == "input_file":
                    # 零丢失：文件分片保留（chat 侧以 file 分片形态透传，
                    # 上游是否支持由上游裁决，不再静默剥离）
                    f = {"type": "file",
                         "file": {k: p[k] for k in
                                  ("filename", "file_data", "file_id")
                                  if k in p}}
                    parts.append(f)
                elif ptype == "input_audio":
                    # 零丢失：音频分片保留（chat 侧 input_audio 同构）
                    parts.append({"type": "input_audio",
                                  "input_audio": p.get("input_audio") or {}})
                elif ptype == "refusal":
                    # 客户端回传的历史 refusal 文本，保留为文本
                    parts.append({"type": "text", "text": str(p.get("refusal") or "")})
                else:
                    # 未知分片类型：以规范 JSON 文本降级保留，不蒸发
                    parts.append({"type": "text",
                                  "text": json.dumps(p, ensure_ascii=False)})
            content = parts
        return {"role": role, "content": content}
    # 未知 input 条目类型（item_reference / 未来新增）：以规范 JSON 文本
    # 降级为 user 内容。旧实现 str(item) 会产出 Python dict repr（单引号
    # 伪 JSON），模型侧完全不可解析。
    return {"role": "user",
            "content": json.dumps(item, ensure_ascii=False)}


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
        # 参考 RikkaHub 的 chat_to_responses_payload：回显 reasoning 保证结构完整
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
    # 并行工具调用的 chat 侧槽位分配表：item_id/call_id -> tool index。
    # Responses 上游按 item_id 区分多个并行 function_call，翻译成 chat
    # delta 必须各占一个 index——旧实现硬编码 index 0，openai-python 等
    # 按 index 累积的 SDK 会把第二个调用的参数追加到第一个上（参数损坏）。
    tool_index: dict = state.setdefault("tool_index_by_id", {})

    def _slot_for(call_id: str) -> int:
        if call_id not in tool_index:
            tool_index[call_id] = len(tool_index)
        return tool_index[call_id]

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
        delta_text = str(data.get("delta") or "")
        state["reasoning_summary_streamed"] = True
        if delta_text.startswith("gAAAA") and state.get("decrypt_reasoning"):
            # 默认不解密（stream_reasoning_decrypt=False）：密文原样透传
            try:
                from services.reasoning_decrypt import decrypt_token
                dec = decrypt_token(delta_text)
                if dec is not None:
                    delta_text = dec
            except Exception:
                pass
        return json.dumps({"choices": [{"index": 0,
                                        "delta": {"reasoning_content": delta_text},
                                        "finish_reason": None}]})
    if etype == "response.output_item.added":
        item = data.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("type") == "function_call":
            # 工具开始：先发 id + name，参数随后按增量透传
            call_id = str(item.get("call_id") or "")
            slot = _slot_for(call_id)
            return json.dumps({"choices": [{"index": 0,
                                            "delta": {"tool_calls": [{
                                                "index": slot,
                                                "id": call_id,
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
    if etype == "response.refusal.delta":
        # [OI] refusal 流式增量：内容性字节，翻译为 chat 的 refusal 字段，
        # 不得丢弃（旧实现无此分支 → 透传到下游出口后被 choices 判空吞掉）
        return json.dumps({"choices": [{"index": 0,
                                        "delta": {"refusal": str(data.get("delta") or "")},
                                        "finish_reason": None}]})
    if etype == "response.function_call_arguments.delta":
        call_id = str(data.get("item_id") or "")
        args_seen.add(call_id)
        slot = _slot_for(call_id)
        return json.dumps({"choices": [{"index": 0,
                                        "delta": {"tool_calls": [{
                                            "index": slot,
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
                                            "finish_reason": "stop"}],
                               "usage": _usage_to_chat(usage)})
        return None
    if etype == "response.output_item.done":
        item = data.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("type") == "reasoning":
            # 增量 summary 已经作为 reasoning_content 流过了——done 事件里的
            # item 会再携带一遍完整 summary，原样下发就是全量重复（双倍思考）。
            # 因此：只要之前见过 summary 增量，done 一律不再补发。
            if state.get("reasoning_summary_streamed"):
                return None
            text = _reasoning_text(item, bool(state.get("decrypt_reasoning")))
            if text:
                if text.startswith("gAAAA") and state.get("decrypt_reasoning"):
                    try:
                        from services.reasoning_decrypt import decrypt_token
                        dec = decrypt_token(text)
                        if dec is not None:
                            text = dec
                    except Exception:
                        pass
                # 零丢失原则：解不开的思考密文原样透传，绝不静默丢弃——
                # 客户端（RikkaHub/opencode）可原样保存并在下轮回传，
                # 上游会话续写依赖它。
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
            slot = _slot_for(call_id)
            return json.dumps({"choices": [{"index": 0,
                                            "delta": {"tool_calls": [{
                                                "index": slot,
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
        # usage 走 responses 键名（input/output_tokens），必须归一成 chat 键名，
        # 否则下游按 prompt_tokens/completion_tokens 取值全部落空，日志与额度
        # 统计会静默丢弃上游真实数值、退回本地估算。
        usage = _usage_to_chat((data.get("response") or {}).get("usage") or {})
        inc = (data.get("response") or {}).get("incomplete_details") or {}
        reason = inc.get("reason")
        finish = _FINISH_FROM_RESPONSES.get(str(reason), "stop") if reason else "stop"
        return json.dumps({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": finish}], "usage": usage})
    return None


def _is_protocol_meta_line(line: str) -> bool:
    """chat SSE 出口不允许出现的 SSE 元行：`event:` / `id:` / `retry:`。

    历史背景（req_df22e4f muse 案）：零丢失透传重构后，未识别行原样下发
    的策略是保守正确的——但它把 Responses 协议的 `event:` 行也一并漏出。
    zen muse 上游每帧都带 `event: response.created` 等元行，翻译后的
    chat chunk 与裸 Responses 元行/载荷混在一条流里，客户端（ZCode 等）
    按 chat chunk 逐帧解析即崩（TerminalStreamChunkError），且完整
    Responses 对象载荷被当成正文注入对话上下文。chat SSE 的帧语法只有
    `data:` 与注释（`:` 前缀保活），元行在此出口永远无意义。
    """
    s = line.lstrip()
    return (s.startswith("event:") or s.startswith("id:")
            or s.startswith("retry:"))


def _is_untranslated_response_event(line: str) -> bool:
    """chat 出口不允许出现的第二类帧：**未翻译的 Responses 协议 data 帧**。

    `_translate_event` 对部分已识别协议事件（response.in_progress、
    response.output_item.added 非 function_call、response.content_part.*、
    response.output_item.done 的 message 条目等）返回 None——它们在 chat
    格式里没有对应帧，语义已由其他翻译帧承载。零丢失透传会把这整包
    Response 对象漏进 chat 流：客户端按 chat chunk 解析即崩，且其大段
    载荷被当成正文注入对话上下文（req_df22e4f 案第二轮 76k 输入的直接
    来源——"context window"误报的真正推手）。

    判定：JSON 可解析 + dict + `type` 命中 Responses 协议内事件 =
    一律不出 chat 出口。协议边界取 `response.` 前缀，**但**
    `response.custom.*` 子命名空间除外——零丢失契约测试以
    `response.custom.future_event` 占位"上游未来新事件类型"，其载荷对
    客户端可能有意义，照旧透传。ping/保活 data 帧同理属协议噪声。
    """
    s = line.lstrip()
    if not s.startswith("data:"):
        return False
    payload = s[5:].strip()
    if not payload or payload == "[DONE]":
        return False
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    etype = data.get("type")
    if not isinstance(etype, str):
        return False
    if etype == "ping":
        return True
    return etype.startswith("response.") \
        and not etype.startswith("response.custom.")


async def iter_responses_sse(first_line: str, aiter,
                             include_first: bool = True,
                             done_state: dict | None = None,
                             prelude: list[str] | None = None,
                             decrypt_reasoning: bool = False) -> AsyncIterator[str]:
    """把 Responses 流式事件流转成 chat 格式的 SSE 行序列。

    **不再伪造结尾 [DONE]**：上游未发 [DONE] 即结束 = 静默截断，
    如实通过 `done_state["saw_done"]` 上报，由调用方决定重试/报错。

    `prelude`：竞速窗口期（判胜首帧之前）上游已发出的行——零丢失原则
    下必须先于 first_line 重放，否则 usage 预告帧/自定义事件会蒸发。

    `decrypt_reasoning`：流内思考密文（gAAAA Fernet）是否尝试解密。
    默认 False——密文原样透传，仅当 stream_reasoning_decrypt 开启时
    由调用方（race_engine）传入 True。

    三条路径（prelude 重放 / 首帧 / 主循环）的未识别行透传均先经两层
    过滤：`_is_protocol_meta_line` 丢弃 `event:` 等元行；
    `_is_untranslated_response_event` 丢弃未翻译的 Responses 协议 data
    帧（response.* / ping）。data 行与保活注释中仅真正的协议外内容照旧
    透传。
    """
    state: dict = {"args_seen": set(), "decrypt_reasoning": decrypt_reasoning}
    if prelude:
        for line in prelude:
            if not line.strip():
                continue
            if _is_protocol_meta_line(line):
                continue
            if _is_untranslated_response_event(line):
                continue
            if line.strip() == "data: [DONE]" or (
                    line.startswith("data:") and line[5:].strip() == "[DONE]"):
                if done_state is not None:
                    done_state["saw_done"] = True
            yield line + "\n\n"
    if include_first and first_line:
        translated = _translate_event(first_line, state)
        if translated == "[DONE]":
            if done_state is not None:
                done_state["saw_done"] = True
            yield "data: [DONE]\n\n"
            return
        if translated:
            yield "data: " + translated + "\n\n"
        elif not _is_protocol_meta_line(first_line) \
                and not _is_untranslated_response_event(first_line):
            # 零丢失原则：首帧是未知/无关 data 行时原样透传，不静默丢弃
            # （自定义事件载荷对客户端仍可能有意义）；两层过滤器见各函数注释。
            yield first_line + "\n\n"
    saw_done = False
    async for line in aiter:
        if not line.strip():
            continue
        if _is_protocol_meta_line(line):
            continue
        if line.strip() == "data: [DONE]":
            saw_done = True
            yield "data: [DONE]\n\n"
            continue
        translated = _translate_event(line, state)
        if translated is not None:
            yield "data: " + translated + "\n\n"
            continue
        # 翻译器不认识/无对应 chat 帧，才进入透传过滤：协议内事件
        # （response.* / ping）整包截留，绝不混流出 chat 出口；
        # 协议外自定义事件与保活注释照旧透传（零丢失契约）。
        if _is_untranslated_response_event(line):
            continue
        yield line + "\n\n"
    if done_state is not None:
        done_state["saw_done"] = saw_done


# ---------------------------------------------------------------------------
# 流式事件：chat SSE -> responses SSE（/v1/responses 出口）
# ---------------------------------------------------------------------------

def _sse_event(name: str, obj: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def iter_chat_sse_as_responses(chat_iter: AsyncIterator[str]) -> AsyncIterator[str]:
    """把内部 chat 格式的 SSE 行流转成 Responses API SSE 事件流。

    忠实补全 Responses 生命周期：created/in_progress、各 output_item 的
    added/done（reasoning / message / function_call）、增量 delta 事件，
    最后 completed + [DONE]。异步生成器以保持真正的逐块流式。

    传输层审查修复（2026-09）：
    - **工具槽位按 tc.index 跟踪**：OpenAI 规范流里 id 只出现在首帧，后续
      参数增量帧 id 为空串。旧实现 `if iid:` 才累积/下发参数 → 所有后续
      增量被静默丢弃，客户端拿到 arguments 为空的 function_call。
    - **usage 时序**：usage 在独立空 choices 尾帧且位于 finish 帧之后；
      response.completed 延迟到 [DONE]/流尾才发，usage 不再恒为 {}。
    - output_index 按条目宣告顺序递增（旧实现恒为 0）。
    """
    emitted_created = False
    done_sent = False
    error_seen = False
    usage: dict = {}
    reasoning_acc = ""
    content_acc = ""
    # 工具槽位注册表：slot_key -> {item_id, name, args, output_index}
    tool_slots: dict = {}
    # 已宣告过的条目 id（用于 output_item.added 去重）及其 output_index
    announced: set = set()
    output_indices: dict = {}
    next_output_index = 0
    message_item_id = "msg_0"
    # 终结延迟态：finish 帧记录 (rid, model, finish)，response.completed
    # 等到 [DONE]/流尾再发——usage 尾帧在 finish 之后才到
    pending_finish: tuple | None = None

    async def announce(item: dict) -> AsyncIterator[str]:
        nonlocal next_output_index
        iid = item.get("id")
        if iid in announced:
            return
        announced.add(iid)
        output_indices[iid] = next_output_index
        yield _sse_event("response.output_item.added",
                         {"type": "response.output_item.added",
                          "output_index": next_output_index, "item": item})
        next_output_index += 1

    def _completed_event(rid: str, model: str, finish: str) -> str:
        if finish == "stop" or finish == "tool_calls":
            status = "completed"
        else:
            status = "incomplete"
        completed: dict = {
            "type": "response.completed",
            "response": {"id": rid, "object": "response",
                         "status": status, "model": model,
                         # chat 键名 -> Responses 键名（input/output_tokens）
                         "usage": _usage_to_responses(usage)},
        }
        if finish in _FINISH_TO_RESPONSES:
            completed["response"]["incomplete_details"] = {
                "reason": _FINISH_TO_RESPONSES[finish]}
        return _sse_event("response.completed", completed)

    async def emit_terminator(rid: str, model: str, finish) -> AsyncIterator[str]:
        """流尾收尾：completed（finish 已见且无 error）+ [DONE]。"""
        nonlocal done_sent
        if finish and not error_seen:
            yield _completed_event(rid, model, str(finish))
        yield "data: [DONE]\n\n"
        done_sent = True

    try:
        async for chunk in chat_iter:
            # SSE 心跳注释（`: keep-alive`）原样透传：/v1/responses 出口同样保活
            if chunk.startswith(":"):
                yield chunk
                continue
            if not chunk.startswith("data:"):
                continue
            payload = chunk[5:].strip().rstrip("\n")
            if payload == "[DONE]":
                if pending_finish:
                    rid, model, finish = pending_finish
                    async for _e in emit_terminator(rid, model, finish):
                        yield _e
                else:
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
                error_seen = True
                # error 即返（与 anthropic 出口同语义）：error 后继续翻译
                # 会产出 response.failed 之后的 delta/done 矛盾序列；
                # created 前置保证 response.failed 不悬空。
                if not emitted_created:
                    emitted_created = True
                    yield _sse_event("response.created", {
                        "type": "response.created",
                        "response": {"id": data.get("id") or "resp_x",
                                     "object": "response",
                                     "status": "in_progress", "model": ""},
                    })
                yield _sse_event("response.failed",
                                 {"type": "response.failed", "error": data["error"]})
                yield "data: [DONE]\n\n"
                done_sent = True
                return
            # usage 捕获先于 choices 判空：usage 常在独立空 choices 尾帧
            if data.get("usage"):
                usage = data["usage"]
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
            if pending_finish is not None:
                # 终结守卫生效期：条目已全部 output_item.done，此帧的增量
                # 不再翻译成 delta（delta-after-done 非法）。零丢失：原样
                # 透传给客户端自行裁决（bytes 不蒸发的口径）。
                yield "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
                continue
            reasoning = delta.get("reasoning_content")
            if reasoning:
                reasoning_acc += str(reasoning)
                async for _ev in announce({"id": "rs_0", "type": "reasoning",
                                           "status": "in_progress", "summary": []}):
                    yield _ev
                yield _sse_event("response.reasoning_summary_text.delta", {
                    "type": "response.reasoning_summary_text.delta",
                    "output_index": output_indices.get("rs_0", 0),
                    "delta": str(reasoning),
                })
            content = delta.get("content")
            if content:
                content_acc += str(content)
                async for _ev in announce({"id": message_item_id, "type": "message",
                                           "role": "assistant", "status": "in_progress",
                                           "content": []}):
                    yield _ev
                yield _sse_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "output_index": output_indices.get(message_item_id, 0),
                    "delta": str(content),
                })
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    iid = str(tc.get("id") or "")
                    name = str(fn.get("name") or "")
                    args = str(fn.get("arguments") or "")
                    # 槽位键：OpenAI 规范流按 index 累积（后续帧 id 为空串）；
                    # 无 index 的方言帧按 id 精确匹配兜底
                    slot = tc.get("index")
                    if isinstance(slot, int) and slot >= 0:
                        key = ("slot", slot)
                    elif iid:
                        key = ("id", iid)
                    else:
                        key = None
                    entry = tool_slots.get(key) if key is not None else None
                    if entry is None and key is None and tool_slots:
                        # 无 index 无 id：归并到最近槽（防御性）
                        entry = list(tool_slots.values())[-1]
                    if entry is None:
                        item_id = iid or f"fc_{len(tool_slots)}"
                        entry = {"item_id": item_id, "name": name,
                                 "args": "", "output_index": None}
                        if key is not None:
                            tool_slots[key] = entry
                    else:
                        if name and not entry["name"]:
                            entry["name"] = name
                    if entry["output_index"] is None:
                        # 该槽位首帧：宣告 function_call 条目
                        entry["output_index"] = next_output_index
                        next_output_index += 1
                        announced.add(entry["item_id"])
                        output_indices[entry["item_id"]] = entry["output_index"]
                        yield _sse_event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": entry["output_index"],
                            "item": {"id": entry["item_id"], "type": "function_call",
                                     "status": "in_progress",
                                     "call_id": entry["item_id"],
                                     "name": entry["name"], "arguments": ""}})
                    if args:
                        entry["args"] += args
                        yield _sse_event("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "output_index": entry["output_index"],
                            "item_id": entry["item_id"],
                            "delta": args,
                        })
            finish = ch.get("finish_reason")
            if finish and pending_finish is None:
                # 终结守卫：首个 finish 帧才关条目（重复 finish 帧不再重发
                # 全部 done 事件、不再覆盖 pending_finish）；finish 之后
                # 到达的增量帧也不再翻译（条目已 done，delta-after-done
                # 是非法序列）。异常上游的残余字节按零丢失原样透传。
                # 结束各 output_item（内容已定）；response.completed 延迟到
                # [DONE]/流尾——usage 尾帧在 finish 之后才到
                if "rs_0" in announced:
                    yield _sse_event("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": output_indices.get("rs_0", 0),
                        "item": {"id": "rs_0", "type": "reasoning",
                                 "status": "completed", "summary": []},
                    })
                if message_item_id in announced:
                    yield _sse_event("response.output_text.done", {
                        "type": "response.output_text.done",
                        "output_index": output_indices.get(message_item_id, 0),
                        "text": content_acc, "item_id": message_item_id,
                    })
                    yield _sse_event("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": output_indices.get(message_item_id, 0),
                        "item": {"id": message_item_id, "type": "message",
                                 "role": "assistant", "status": "completed",
                                 "content": [{"type": "output_text", "text": content_acc}]},
                    })
                for entry in tool_slots.values():
                    yield _sse_event("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": entry["output_index"],
                        "item": {"id": entry["item_id"], "type": "function_call",
                                 "status": "completed",
                                 "call_id": entry["item_id"],
                                 "name": entry["name"],
                                 "arguments": entry["args"]},
                    })
                pending_finish = (rid, data.get("model") or "", str(finish))
    finally:
        # 客户端断开 / 外层 aclose 时，内层 chat 生成器（_stream_response）必须
        # 被显式关闭——否则其 finally（含并发闸门 _bump_active(-1)）要等 GC 触发，
        # 高压下表现为假 server_overloaded。见审查条目 R2-M2-6。
        try:
            await chat_iter.aclose()
        except Exception:  # noqa: BLE001
            pass
    if not done_sent:
        # 上游流关闭却未见 [DONE]：补终结（completed 仅在 finish 已见时发）
        if pending_finish:
            rid, model, finish = pending_finish
            async for _e in emit_terminator(rid, model, finish):
                yield _e
        else:
            yield "data: [DONE]\n\n"
