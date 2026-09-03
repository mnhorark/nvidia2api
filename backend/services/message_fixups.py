"""请求历史消息的兼容性修复（fixups）。

当前唯一修复：跨轮重复 tool_call id 唯一化。

背景（2026-09-03 zen 线上 400 实证）：
  zcode 等 agent 以"工具名:本轮序号"作 call id（Bash:0 = 本轮第一个
  Bash 调用），多轮对话后历史里天然存在多个同名 id（每轮都从 0 编号）。
  zen 的 Console 上游（Anthropic 系后端）强校验"每个 function_call
  必须恰好一个匹配的 function_call_output"——跨轮同名 id 整包 400：
  "Duplicate function_call_output for call_id 'Bash:0'"。
  NVIDIA 等后端对重复 id 宽容，因此只有走 zen/Anthropic 系渠道的
  请求暴露此问题。

规则（按出现顺序重写，保证 call 与 output 的配对关系不变）：
- assistant.tool_calls[].id 第 n>1 次出现 → 重写为 "<id>__dup<n>"
- 紧随其后的 role=tool 消息按"最近一个 assistant 轮"的映射表同步改写
- 无重复时零改动（透传纯度：一个字节都不碰）

id 只是与 output 关联的键，重写不改变语义；模型侧生成的 id 本就唯一，
历史 id 唯一化对模型理解无影响。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nvidia2api.message_fixups")


def dedupe_tool_call_ids(body: dict) -> int:
    """跨轮重复的 tool_call id 唯一化（就地改写）。返回改动条目数。"""
    if not isinstance(body, dict):
        return 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0

    seen: dict[str, int] = {}
    changed = 0
    # 当前 assistant 轮的 id 映射：old_id -> new_id（供随后的 tool 消息用）
    pending_map: dict[str, str] = {}

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            pending_map = {}
            tcs = msg.get("tool_calls")
            if not isinstance(tcs, list):
                continue
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                cid = tc.get("id")
                if not isinstance(cid, str) or not cid:
                    continue
                n = seen.get(cid, 0)
                seen[cid] = n + 1
                if n > 0:
                    new_id = f"{cid}__dup{n + 1}"
                    tc["id"] = new_id
                    pending_map[cid] = new_id
                    changed += 1
                else:
                    pending_map[cid] = cid
        elif role == "tool":
            tcid = msg.get("tool_call_id")
            if isinstance(tcid, str) and tcid in pending_map:
                new_id = pending_map[tcid]
                if new_id != tcid:
                    msg["tool_call_id"] = new_id
                    changed += 1
    if changed:
        logger.info("dedupe_tool_call_ids: rewrote %d duplicate ids", changed)
    return changed
