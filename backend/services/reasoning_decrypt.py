"""Reasoning 思考内容解密与格式归一化 —— 参考 RikkaHub 的 Responses reasoning 处理。

RikkaHub 的 Responses 协议把推理拆成两段：
- `summary` 明文摘要：可直接展示（summary_text 数组）
- `encrypted_content` 密文原文：Fernet 形态 gAAAA…，用于多轮历史原样回传
  （RikkaHub 仅保存为 OpenAIReasoningMetadata，不做解密；本模块在有密钥时
  尝试还原，便于 muse-spark-1.2 等将完整思考透传给下游）。

流式响应中的思考解密与归一化：
- Kilo/OpenRouter 网关：reasoning 字段可能是 Fernet 密文或纯文本
- muse-spark：reasoning_content 字段可能是 Fernet 密文
- 支持 delta.reasoning 和 delta.reasoning_content 两种格式
- 归一化：将 delta.reasoning 转换为 delta.reasoning_content（OpenAI 标准格式）
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger("nvidia2api.reasoning_decrypt")

_FERNET_CACHE: dict[str, Fernet] = {}


def _fernet_for(raw: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    cached = _FERNET_CACHE.get(raw)
    if cached is not None:
        return cached
    f = Fernet(key)
    _FERNET_CACHE[raw] = f
    return f


def _candidate_keys() -> list[str]:
    keys: list[str] = []
    rk = str(getattr(settings, "REASONING_DECRYPT_KEY", "") or "").strip()
    if rk:
        keys.append(rk)
    ek = str(getattr(settings, "ENCRYPTION_KEY", "") or "").strip()
    if ek and ek not in keys:
        keys.append(ek)
    sk = str(getattr(settings, "SECRET_KEY", "") or "").strip()
    if sk and sk not in keys and sk != "dev-insecure-secret-change-me":
        keys.append(sk)
    # 兜底：与 crypto._fernet() 一致的默认派生键
    fallback = "nvidia2api"
    if fallback not in keys:
        keys.append(fallback)
    return keys


def decrypt_token(token: str) -> str | None:
    """尝试用候选密钥解密 Fernet token，成功返回明文，失败返回 None。"""
    if not token or not isinstance(token, str):
        return None
    # 非 Fernet 形态直接视为非密文
    if not token.startswith("gAAAA"):
        return None
    for raw in _candidate_keys():
        try:
            f = _fernet_for(raw)
            plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
            return plain
        except (InvalidToken, ValueError, Exception):
            continue
    return None


def decrypt_reasoning_item(item: dict) -> str:
    """从 Responses reasoning 条目提取可展示文本：优先 summary，明文不存在则尝试解密密文。

    与 RikkaHub 逻辑对齐：summary 可直接展示；encrypted_content 需解密后才可读。
    解密失败时返回原密文占位（下游可提示"思考内容已加密"），不抛异常。
    """
    if not isinstance(item, dict):
        return ""
    # 1) 明文摘要优先（RikkaHub 的 _reasoning_summary_text）
    summary = item.get("summary")
    if isinstance(summary, list):
        parts: list[str] = []
        for s in summary:
            if isinstance(s, dict) and s.get("type") in ("summary_text", "text"):
                parts.append(str(s.get("text") or ""))
            elif isinstance(s, str):
                parts.append(s)
        txt = "".join(parts)
        if txt:
            return txt
    # 2) 无明文则尝试解密密文
    enc = item.get("encrypted_content")
    if isinstance(enc, str) and enc:
        dec = decrypt_token(enc)
        if dec is not None:
            return dec
        return enc
    return ""


def decrypt_chat_delta(delta: dict) -> bool:
    """对 chat SSE 的 delta 就地解密 reasoning_content/reasoning，返回是否发生替换。

    支持格式：
    - delta.reasoning_content: "gAAAA..." (muse-spark)
    - delta.reasoning: "gAAAA..." (Kilo/OpenRouter)
    - delta.reasoning: {"effort": "gAAAA..."} (嵌套格式)
    """
    if not isinstance(delta, dict):
        return False
    changed = False
    for key in ("reasoning_content", "reasoning"):
        val = delta.get(key)
        if isinstance(val, str) and val.startswith("gAAAA"):
            dec = decrypt_token(val)
            if dec is not None:
                delta[key] = dec
                changed = True
        elif isinstance(val, dict):
            # 嵌套格式：{"effort": "gAAAA...", ...}
            for nested_key in ("effort", "content", "text"):
                nested_val = val.get(nested_key)
                if isinstance(nested_val, str) and nested_val.startswith("gAAAA"):
                    dec = decrypt_token(nested_val)
                    if dec is not None:
                        val[nested_key] = dec
                        changed = True
    return changed


def decrypt_chat_message(message: dict) -> bool:
    """对 chat 非流式 message 的 reasoning_content 就地解密。"""
    if not isinstance(message, dict):
        return False
    changed = False
    val = message.get("reasoning_content")
    if isinstance(val, str) and val.startswith("gAAAA"):
        dec = decrypt_token(val)
        if dec is not None:
            message["reasoning_content"] = dec
            changed = True
    # 也处理 reasoning 字段
    val = message.get("reasoning")
    if isinstance(val, str) and val.startswith("gAAAA"):
        dec = decrypt_token(val)
        if dec is not None:
            message["reasoning"] = dec
            changed = True
    elif isinstance(val, dict):
        for nested_key in ("effort", "content", "text"):
            nested_val = val.get(nested_key)
            if isinstance(nested_val, str) and nested_val.startswith("gAAAA"):
                dec = decrypt_token(nested_val)
                if dec is not None:
                    val[nested_key] = dec
                    changed = True
    return changed


def normalize_reasoning_format(data: dict) -> bool:
    """归一化思考内容格式：将 delta.reasoning 转换为 delta.reasoning_content。

    Kilo/OpenRouter 渠道返回的思考内容格式是 delta.reasoning（纯文本或密文），
    而 OpenAI 标准格式是 delta.reasoning_content。
    此函数将 reasoning 字段重命名为 reasoning_content，确保客户端能正确解析。

    返回是否发生了格式转换。
    """
    if not isinstance(data, dict):
        return False
    changed = False
    # 处理 choices[0].delta
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            # 如果存在 reasoning 但不存在 reasoning_content，则转换
            if "reasoning" in delta and "reasoning_content" not in delta:
                val = delta.pop("reasoning")
                # 如果是字符串，直接作为 reasoning_content
                if isinstance(val, str):
                    delta["reasoning_content"] = val
                    changed = True
                elif isinstance(val, dict):
                    # 嵌套格式，尝试提取文本
                    text = val.get("text") or val.get("content") or val.get("effort")
                    if isinstance(text, str):
                        delta["reasoning_content"] = text
                        changed = True
    # 处理顶层 reasoning（Responses 风格）
    if "reasoning" in data and "reasoning_content" not in data:
        val = data.pop("reasoning")
        if isinstance(val, str):
            data["reasoning_content"] = val
            changed = True
        elif isinstance(val, dict):
            text = val.get("text") or val.get("content") or val.get("effort")
            if isinstance(text, str):
                data["reasoning_content"] = text
                changed = True
    return changed


def decrypt_sse_chunk(chunk: str) -> str:
    """解密并归一化 SSE chunk 中的思考内容，返回处理后的 chunk。

    处理以下格式：
    - data: {"choices": [{"delta": {"reasoning_content": "gAAAA..."}}]}
    - data: {"choices": [{"delta": {"reasoning": "gAAAA..."}}]}
    - data: {"choices": [{"delta": {"reasoning": "纯文本思考..."}}]} -> 归一化为 reasoning_content
    - data: {"reasoning": "gAAAA..."} (Responses 风格)
    """
    if not chunk.startswith("data:"):
        return chunk
    payload = chunk[5:].strip()
    if payload == "[DONE]":
        return chunk
    try:
        import json
        data = json.loads(payload)
        if not isinstance(data, dict):
            return chunk
        changed = False
        # 1. 解密加密的思考内容
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta")
            if isinstance(delta, dict):
                if decrypt_chat_delta(delta):
                    changed = True
        # 处理顶层 reasoning (Responses 风格)
        reasoning = data.get("reasoning")
        if isinstance(reasoning, str) and reasoning.startswith("gAAAA"):
            dec = decrypt_token(reasoning)
            if dec is not None:
                data["reasoning"] = dec
                changed = True
        elif isinstance(reasoning, dict):
            for key in ("effort", "content", "text"):
                val = reasoning.get(key)
                if isinstance(val, str) and val.startswith("gAAAA"):
                    dec = decrypt_token(val)
                    if dec is not None:
                        reasoning[key] = dec
                        changed = True
        # 2. 归一化格式：将 reasoning 转换为 reasoning_content
        if normalize_reasoning_format(data):
            changed = True
        if changed:
            return "data: " + json.dumps(data, ensure_ascii=False) + "\n"
    except Exception:
        pass
    return chunk


def clear_cache() -> None:
    _FERNET_CACHE.clear()
