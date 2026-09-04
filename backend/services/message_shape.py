"""请求消息的 OpenAI 规范形态钳制（shape clamp）。

背景（2026-09-04 线上实证）：
  zcode/keysmith（AI SDK 系客户端）与对话页的请求差异不在思考参数
  （两者归一化产物完全一致），而在**消息形态**：

  - role=tool 的 content 可能是 [{type:"text","text":"..."}] 数组
    （AI SDK 内部形态），而非 OpenAI 规范的字符串；
  - assistant 消息的 content 同样可能是数组形态；
  - role=tool 消息可能携带上游不认识的附加键（name 等）。

  多数上游（vLLM / Anthropic 系 / 严格网关）对 tool.content 强校验
  string 类型——数组形态整包 400。这解释了三组现象：
  直连简单请求成功、对话页（无工具历史）成功、zcode（带工具历史
  的完整 agent 请求）全线路 400。

设计原则（对齐 new-api 的 per-channel adapter 哲学）：
  形态转换 ≠ 语义改写。字符串化拼接保留全部文本字节；未知附加键
  保留（上游不认会被拒，那是可见 400 而非静默丢失——但 tool 消息
  的结构性键白名单之外的字段剔除，因为 OpenAI 规范 tool 消息只有
  role/tool_call_id/content 三键，附加键是 SDK 泄漏）。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("nvidia2api.message_shape")

# role=tool 的规范键：附加键是 SDK 层泄漏，直接发给上游大概率 400
_TOOL_ALLOWED = frozenset({"role", "tool_call_id", "content"})


def _content_to_text(content) -> str:
    """把 OpenAI content 数组形态收敛为字符串（字节零丢失）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "output_text"):
                parts.append(str(p.get("text") or ""))
            elif isinstance(p, dict) and p.get("type") == "refusal":
                parts.append(str(p.get("refusal") or ""))
            elif isinstance(p, str):
                parts.append(p)
            # 图片/音频等非文本块不出现在 tool/纯文本消息里——若出现，
            # 保留 JSON 文本以防字节蒸发（上游不支持会显式 400 可诊断）
            elif isinstance(p, dict):
                parts.append(json.dumps(p, ensure_ascii=False))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def clamp_message_shapes(body: dict) -> int:
    """就地钳制 messages 的形态为 OpenAI 规范。返回改动消息数。

    规则：
    - role=tool：content 数组 -> 字符串；白名单外附加键剔除
    - assistant：content 数组 -> 字符串（有 tool_calls 时 content
      允许为 null/字符串；数组形态多数上游不认）
    - 其它角色的 content 数组含纯文本块时，同样收敛为字符串
      （多模态 image 块保留原数组——那是合法形态）
    """
    if not isinstance(body, dict):
        return 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    changed = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            mutated = False
            content = msg.get("content")
            if not isinstance(content, str):
                msg["content"] = _content_to_text(content)
                mutated = True
            extra = set(msg.keys()) - _TOOL_ALLOWED
            for k in extra:
                msg.pop(k, None)
                mutated = True
            if mutated:
                changed += 1
        elif role == "assistant":
            content = msg.get("content")
            if isinstance(content, list):
                # 只有当没有 image 等多模态块时才收敛为字符串
                has_multimodal = any(
                    isinstance(p, dict) and p.get("type") not in
                    ("text", "output_text", "refusal")
                    for p in content)
                if not has_multimodal:
                    msg["content"] = _content_to_text(content)
                    changed += 1
        # user/developer/system 的数组 content：合法形态（可含 image），
        # 仅在全部块均为文本/拒绝时收敛为字符串（多数上游同样接受
        # 字符串，收敛提高兼容面且不丢字节）
        elif role in ("user", "developer", "system"):
            content = msg.get("content")
            if isinstance(content, list) and content and all(
                    isinstance(p, dict) and p.get("type") in ("text", "refusal")
                    for p in content):
                msg["content"] = _content_to_text(content)
                changed += 1
    if changed:
        logger.info("clamp_message_shapes: normalized %d message(s)", changed)
    return changed
