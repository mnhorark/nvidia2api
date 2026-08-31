"""Token 估算：上游未返回 usage 时本地兜底统计。

部分上游（如 NVIDIA DeepSeek）流式请求不返回 usage（即使传
stream_options.include_usage）。此时用 tiktoken（cl100k_base，OpenAI 通用
编码）估算 prompt / completion token；tiktoken 不可用时退化为字符启发式。
估算只用于日志/额度统计，不影响请求本身。
"""
from __future__ import annotations

_enc: object | None | bool = None  # None=未初始化, False=不可用


def _encoder():
    global _enc
    if _enc is None:
        try:
            import tiktoken
            _enc = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            _enc = False
    return _enc if _enc else None


def estimate_tokens(text: str | None) -> int:
    """估算一段文本的 token 数。"""
    text = text or ""
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:  # noqa: BLE001
            pass
    # 字符启发式：中文约 1 token/字，英文约 4 字符/token
    cjk = sum(1 for ch in text
              if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
    ascii_chars = max(len(text) - cjk, 0)
    return cjk + ascii_chars // 4 + 1


def estimate_messages_tokens(messages) -> int:
    """估算 chat messages 的总 token（含每条消息的结构开销）。"""
    if not isinstance(messages, list):
        return 0
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += estimate_tokens(part["text"])
        # 每条消息的 role/结构开销约 4 token
        total += 4
    return total
