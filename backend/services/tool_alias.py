"""超长工具名（function name）的双向别名映射。

背景：上游（Zen 等）对 tools / tool_calls 里的 function.name 有 64 字符硬上限
（`name` must be at most 64 characters），而客户端（尤其 MCP 命名空间工具，
如 mcp__server__tool__...）经常突破该限制，导致全部线路 400。

策略：
- 入境：上游请求体中所有 function name 超过 60 字符的，替换为
  确定性短别名 fn_<sha1 前 12 位>；保留映射表
- 出境：把模型响应里出现的别名逐一还原为原始长名，客户端无感知

注意：处理模型必须能"叫回"工具——所以替换必须双向且确定。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("nvidia2api.tool_alias")

MAX_NAME_LEN = 60  # 上游硬上限 64，留余量


def _alias_for(name: str) -> str:
    """前缀保留别名：保留原名的可识别前缀，仅折叠超长尾部为短哈希。

    旧形态 `fn_<sha1-12>` 与原文提到的原名完全无交集——muse 等模型在
    "文本里说要用 mcp__plugin_...，工具列表却是 fn_a1b2c3" 时产生
    白名单困惑（实测思考原话："Verifying tool allowlist against the
    requested MCP name ... deciding not to invoke the unavailable
    function"），不确定性地吐出空参数退化工具调用。
    前缀保留形态 `mcp__plugin_android-emulator_and_a1b2c3` 让模型能
    识别"这就是文本里那个工具的变体"，困惑面大幅缩小。
    结果按字节计长 ≤ MAX_NAME_LEN，确定性（同名恒得同别名）。
    """
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    keep = MAX_NAME_LEN - len(digest) - 1
    out: list[str] = []
    used = 0
    for ch in name:
        b = len(ch.encode("utf-8"))
        if used + b > keep:
            break
        out.append(ch)
        used += b
    return "".join(out) + "_" + digest


def shorten_function_names(body: dict) -> dict[str, str]:
    """就地改写请求体里的超长工具名，返回 {别名: 原名} 映射表。

    覆盖范围（chat 格式或其内部中间形态）：
    - tools[*].function.name
    - tool_choice.function.name
    - messages[*].tool_calls[*].function.name（多轮回显的 assistant 消息）
    """
    mapping: dict[str, str] = {}

    def short(n):
        if isinstance(n, str) and len(n.encode("utf-8")) > MAX_NAME_LEN:
            a = _alias_for(n)
            mapping[a] = n
            return a
        return n

    tools = body.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    fn["name"] = short(fn["name"])
                # responses 风格平铺 tools: {"type": "function", "name": ...}
                elif t.get("type") == "function" and isinstance(t.get("name"), str):
                    t["name"] = short(t["name"])

    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        if isinstance(tc.get("function"), dict):
            fn = tc["function"]
            if isinstance(fn.get("name"), str):
                fn["name"] = short(fn["name"])
        if isinstance(tc.get("name"), str) and tc.get("type") == "function":
            tc["name"] = short(tc["name"])
        if tc.get("type") == "tool" and isinstance(tc.get("name"), str):  # anthropic
            tc["name"] = short(tc["name"])

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    fn["name"] = short(fn["name"])

    return mapping


def _restore_name(name: Any, mapping: dict[str, str]) -> Any:
    if isinstance(name, str) and name in mapping:
        return mapping[name]
    # muse 等模型会给"文本中提到但列表中不存在"的名字加内部命名空间前缀
    # （实测 default.<别名>）：剥掉已知前缀后再查一次映射
    if isinstance(name, str) and name.startswith("default.") and name[8:] in mapping:
        return mapping[name[8:]]
    return name


def restore_stream_chunk(chunk: str, mapping: dict[str, str]) -> str:
    """把 SSE chunk 里的工具别名还原为原始长名。无别名命中时原样返回。

    快路径用 `"name"` 字段字面量做门控（别名只可能出现在 name 字段里），
    不再依赖旧 `fn_` 前缀——前缀保留别名没有统一前缀。
    """
    if not mapping or not chunk.startswith("data:") or '"name"' not in chunk:
        return chunk
    payload = chunk[5:].strip()
    if payload == "[DONE]":
        return chunk
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return chunk
    changed = [False]

    def visit(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "name" and isinstance(v, str):
                    restored = _restore_name(v, mapping)
                    if restored != v:
                        obj[k] = restored
                        changed[0] = True
                else:
                    visit(v)
        elif isinstance(obj, list):
            for it in obj:
                visit(it)

    visit(data)
    if not changed[0]:
        return chunk
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def restore_payload(payload: Any, mapping: dict[str, str]) -> Any:
    """非流式响应：还原 message/tool_calls 里的工具别名（就地修改并返回）。"""
    if not mapping or not isinstance(payload, dict):
        return payload
    changed = [False]

    def visit(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "name" and isinstance(v, str):
                    restored = _restore_name(v, mapping)
                    if restored != v:
                        obj[k] = restored
                        changed[0] = True
                else:
                    visit(v)
        elif isinstance(obj, list):
            for it in obj:
                visit(it)

    visit(payload)
    return payload
