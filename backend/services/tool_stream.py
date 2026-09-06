"""工具调用流规整器：把上游非标准的 tool_calls 增量流校正为 OpenAI 规范形态。

参考系（2026-09 调研结论）：
- new-api / one-api / LiteLLM 对"协议相同且行为良好"的上游做字节级透传
  （仅旁路解析计费），但对协议不同或行为破损的上游一律**重建流**
  （per-channel adapter / pass-through 端点明确声明为纯透传）；
- OpenRouter 明确运行归一化层（统一 finish_reason/schema，甚至故意偏离
  OpenAI spec——usage 块带一个空 delta 的 choice，因为"很多客户端遇到
  空 choices 数组会崩"）；
- NVIDIA NeMo Relay 的 codec 是"解析 → 累积 → 重发规范增量"三段式；
- vLLM issue 实证畸形是普遍病：GLM parser 终结帧重复元数据/与
  finish_reason 同帧（#44098，严格客户端会丢最后的参数字节）、arguments
  整段一块发（#43267）、stream-interval>1 参数变空（#31501）、同 chunk
  两个同 index 条目打挂 openai-python 累积器（#3203）；且 name 也会被
  分片流式发送（"get_weath"+"er"）。

本模块处理的方言（muse-spark 经 zen 网关实测）：
  帧1: {"id": "call_abc", "name": "bash", "arguments": ""}              # 开包
  帧2: {"id": "fc_abc",   "name": null,   "arguments": "<完整JSON>"}    # 换 id + 空名
  帧3: {"id": "call_abc", "name": "bash", "arguments": "<完整JSON>"}    # 整段重述
按 index 累积的 SDK（openai-python accumulate_delta 等，arguments 一律 +=）
会把帧2+帧3 拼成非法 JSON；按 id 分组的客户端会把 fc_ 帧当残缺的第二调用。

规整规则（槽位键 = (choice_index, tool_index)，对齐 new-api 复合键思路）：
- id：首次出现的记为 canonical；后续帧不再输出 id（OpenAI 规范形态）；
- name：与 arguments 同一套三态规则——精确重复丢弃 / 前缀重述只发差量 /
  其余按增量追加（兼容 vLLM GLM 式 name 分片）；
- arguments：N == A 丢弃；A 是 N 前缀只发 N[len(A):]；否则原样直传并累积；
- 规整后完全空的 tool_calls 条目整包丢弃；
- **终结帧拆分**：tool_calls 参数与 finish_reason 同帧时拆成两帧
  （vLLM #44098 的"更安全协议形态"），避免严格客户端丢弃末段参数。

与 tool_alias.py 的分工：tool_alias 处理"超长名工具的双向别名"，
本模块处理"流式增量帧的乱序/重述/分片"。两者串行工作（本模块在前）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("nvidia2api.tool_stream")


class _SlotState:
    __slots__ = ("id", "name", "args")

    def __init__(self) -> None:
        self.id: str | None = None
        self.name: str | None = None
        self.args: str = ""


def _merge_stream_field(state_val: str | None, incoming: str) -> tuple[str | None, bool]:
    """三态合并：返回 (要下发的差量或 None, 是否有新内容)。

    - incoming 与累计值完全相同 → 重述帧，丢弃（None, False）
    - 累计值是 incoming 的前缀   → 只发差量（delta, True）
    - 其余                       → 按增量追加（incoming, True）
    """
    prev = state_val or ""
    if prev and incoming == prev:
        return None, False
    if prev and incoming.startswith(prev):
        delta = incoming[len(prev):]
        return (delta, True) if delta else (None, False)
    return incoming, True


class ToolCallStreamNormalizer:
    """单条流用一个实例。"""

    def __init__(self) -> None:
        # 槽位键 (choice_index, tool_index) —— 对齐 OpenAI/new-api 的复合键语义
        self._slots: dict[tuple[int, int], _SlotState] = {}

    def _normalize_entry(self, entry: dict, state: _SlotState,
                         eidx: int | None = None) -> dict | None:
        # 发射 index 必须与槽位键同源（feed() 传入 eidx）：Gemini 式
        # 省略 index 的并行调用按列表位置分槽，若此处回落 0，两个并行
        # 调用都发 index 0——客户端把参数拼进同一个调用，JSON 损坏。
        idx = eidx if eidx is not None else entry.get("index", 0)
        if not isinstance(idx, int):
            idx = 0
        out: dict[str, Any] = {"index": idx}
        emitted_any = False

        # --- id：锁定首个出现的 id；后续帧不输出（OpenAI 只在首帧给 id）---
        raw_id = entry.get("id")
        if isinstance(raw_id, str) and raw_id and state.id is None:
            state.id = raw_id
            out["id"] = raw_id
            if isinstance(entry.get("type"), str):
                out["type"] = entry["type"]
            emitted_any = True

        # --- function.name / arguments：同一套三态规则 --------------------
        fn = entry.get("function")
        if isinstance(fn, dict):
            out_fn: dict[str, Any] = {}
            name = fn.get("name")
            if isinstance(name, str) and name:
                if state.name is None:
                    state.name = name
                    out_fn["name"] = name
                    emitted_any = True
                else:
                    delta, fresh = _merge_stream_field(state.name, name)
                    if fresh and delta:
                        # 分片（"get_weath"+"er"）或前缀重述的差量
                        state.name = state.name + delta if not state.name.startswith(name) else name
                        out_fn["name"] = delta
                        emitted_any = True
                    # 精确重述 → 丢弃
            args = fn.get("arguments")
            if isinstance(args, str) and args:
                delta, fresh = _merge_stream_field(state.args, args)
                if fresh and delta:
                    out_fn["arguments"] = delta
                    # 累计口径：incoming 是"已累计值的重述"（含前缀重述）时只补
                    # 差量，是纯增量时整段追加。`state.args` 恒为 str
                    # （_SlotState 初始化为 ""），不存在 None 分支。
                    if state.args and args.startswith(state.args):
                        state.args += delta
                    else:
                        state.args += args
                    emitted_any = True
                # 精确重述 → 丢弃
            elif isinstance(args, str) and args == "" and emitted_any:
                # 开包帧的 arguments: ""——保留（SDK 用空串初始化）
                out_fn["arguments"] = ""
            if out_fn:
                out["function"] = out_fn
        if not emitted_any:
            return None
        return out

    def feed(self, chunk: str) -> list[str]:
        """吃一条 chat 格式 SSE chunk，返回要下发的 0~2 条 chunk。

        2 条仅出现在终结帧拆分场景（tool_calls 参数帧 + finish 帧）。
        """
        if not chunk.startswith("data:") or "tool_calls" not in chunk:
            return [chunk]
        payload = chunk[5:].strip()
        if not payload or payload == "[DONE]":
            return [chunk]
        try:
            data = json.loads(payload)
        except Exception:  # noqa: BLE001
            return [chunk]
        if not isinstance(data, dict):
            return [chunk]
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return [chunk]

        any_emitted = False
        mutated = False
        finish_split_pending = False
        split_finishes: list[tuple[int, str]] = []  # (choice_index, finish_reason)
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            tcs = delta.get("tool_calls")
            if not isinstance(tcs, list) or not tcs:
                continue
            mutated = True  # 至少解析过 tool_calls 字段
            choice_idx = choice.get("index", 0)
            if not isinstance(choice_idx, int):
                choice_idx = 0

            out_entries: list[dict] = []
            emitted_args = False
            for pos, entry in enumerate(tcs):
                if not isinstance(entry, dict):
                    out_entries.append(entry)
                    continue
                # 槽位键用 entry 自带的 index（并行调用按语义分槽），
                # 缺失时才回落列表位置（Gemini 兼容层有省略 index 的情况）
                eidx = entry.get("index", pos)
                if not isinstance(eidx, int):
                    eidx = pos
                state = self._slots.setdefault((choice_idx, eidx), _SlotState())
                norm = self._normalize_entry(entry, state, eidx)
                if norm is not None:
                    out_entries.append(norm)
                    fn = norm.get("function") or {}
                    if fn.get("arguments"):
                        emitted_args = True

            if out_entries:
                delta["tool_calls"] = out_entries
                any_emitted = True
            else:
                # 全部条目都是冗余重述：剥掉 tool_calls，帧内别无他物才丢弃
                delta.pop("tool_calls", None)

            # 终结帧拆分（vLLM #44098）：参数与 finish_reason 同帧 → 拆两帧
            if choice.get("finish_reason") and emitted_args and any_emitted:
                finish_split_pending = True
                split_finishes.append((choice_idx, choice["finish_reason"]))
                choice["finish_reason"] = None

        if not mutated:
            return [chunk]  # 无 tool_calls 字段：字节级原样透传
        if not any_emitted:
            rest_present = any(
                (isinstance(c, dict) and isinstance(c.get("delta"), dict)
                 and any(v not in (None, "", [], {}) for v in c["delta"].values()))
                or (isinstance(c, dict) and c.get("finish_reason"))
                for c in choices if isinstance(c, dict)
            )
            if not rest_present and not data.get("usage"):
                return []

        main = "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        if not finish_split_pending:
            return [main]
        # finish 追随帧：空 delta + finish_reason + usage。
        # usage 只放追随帧（帧序语义：finish 之后才到 usage，与上游
        # 标准流一致）——main 帧若同时带 usage 会让按帧计费的客户端双计。
        usage = data.pop("usage", None)
        main = "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        tail_choices = [
            {"index": idx, "delta": {}, "finish_reason": fr}
            for idx, fr in split_finishes
        ]
        tail: dict[str, Any] = {"choices": tail_choices}
        if usage:
            tail["usage"] = usage
        return [main, "data: " + json.dumps(tail, ensure_ascii=False) + "\n\n"]
