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
import json
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
    for key in ("reasoning_content", "reasoning"):
        val = message.get(key)
        if isinstance(val, str):
            if val.startswith("gAAAA"):
                dec = decrypt_token(val)
                if dec is not None:
                    message[key] = dec
                    changed = True
                # gAAAA 但解不开 = 密钥不对，保持原样交给上层（可能仅作回传元数据）
                continue
            if _looks_like_opaque_blob(val):
                # 非 Fernet 形态的不透明密文：直接替换占位符，别把乱码怼给客户端
                message[key] = ENCRYPTED_PLACEHOLDER
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


def _looks_like_opaque_blob(v: str) -> bool:
    """启发式：长的纯 base64url 串（无空格、无自然语言标点）视为不可读密文。

    muse/部分网关的思考密文不一定带 gAAAA 前缀（比如切片到达后丢掉前缀、
    或上游采用非 Fernet 的不透明封装）。这种内容直接透给客户端就是一坨
    无法阅读的乱码——用占位符替换。
    """
    if len(v) < 200:
        return False
    import re
    if not re.fullmatch(r"[A-Za-z0-9_\-]+={0,2}", v):
        return False
    return True


ENCRYPTED_PLACEHOLDER = "[思考内容已加密，暂不可读]"


class StreamReasoningDecryptor:
    """流式思考内容的**有状态**解密器——解决逐 chunk 无状态解密的两个盲区：

    1. Fernet token 被上游切成多个 SSE 分片：首片带 gAAAA 前缀但 token 不完整
       解密失败，后续分片没有前缀被原样放行——客户端看到半解密+原始密文碎片。
       本类缓冲分片、凑齐后一次解密再下发。
    2. 密文根本没有 gAAAA 前缀（不透明 blob）：识别并替换为占位符，避免把
       数千字符的乱码怼到用户脸上。

    用法：每个流新建一个实例，对每个 chat 格式 SSE chunk 调 `feed()`，
    流结束（[DONE] / 异常）调 `finalize()` 冲刷残留缓冲。
    """

    # 缓冲上限：超出视为异常上游，冲刷为占位符，防内存被恶意流撑爆
    MAX_BUFFER = 64 * 1024

    def __init__(self) -> None:
        self._frags: list[str] = []
        self._field: str = "reasoning_content"

    # -- 内部工具 -----------------------------------------------------------

    def _flush_reason(self, sent_len: int | None = None) -> str:
        """把缓冲的分片拼起来尝试解密，返回要下发的文本。"""
        blob = "".join(self._frags)
        self._frags = []
        if not blob:
            return ""
        dec = decrypt_token(blob)
        if dec is not None:
            return dec
        # token 形态正确但密钥不对，或根本不是 Fernet：给占位符
        logger.warning("reasoning buffer undecryptable (%d chars), placeholder emitted",
                       len(blob))
        return ENCRYPTED_PLACEHOLDER

    @staticmethod
    def _mk_chunk(base: dict, field: str, text: str) -> str:
        """合成一条**最小**的推理 chunk——只携带解密后的思考文本。

        刻意不克隆模板里的其它字段（content/tool_calls 等）：否则占位符 chunk
        会重复携带正文，客户端把同一句话渲染两遍。顶层 id/model 之类保留无妨，
        但最小化最稳妥——下游只需推理文本本身。
        """
        out = {"choices": [{"delta": {field: text}}]}
        return "data: " + json.dumps(out, ensure_ascii=False) + "\n"

    # -- 主流程 -------------------------------------------------------------

    def feed(self, chunk: str) -> list[str]:
        """吃一条 chat 格式 SSE chunk，返回要下发的 chunk 列表（0~2 条）。"""
        if not chunk.startswith("data:"):
            return [chunk]
        payload = chunk[5:].strip()
        if payload == "[DONE]":
            return self._emit_pending_then(chunk)
        try:
            data = json.loads(payload)
        except Exception:  # noqa: BLE001
            return [chunk] if not self._frags else self._emit_pending_then(chunk)
        if not isinstance(data, dict):
            return [chunk]

        # 其它协议转换逻辑保持不变：先跑原有的就地解密/归一化（处理
        # 完整 token + Kilo/OpenRouter 形态），本类只负责"剩下搞不定的"。
        pre = decrypt_sse_chunk(chunk)
        try:
            data2 = json.loads(pre[5:].strip())
        except Exception:  # noqa: BLE001
            return [pre]
        chs = data2.get("choices") or []
        delta = chs[0].get("delta") if chs and isinstance(chs[0], dict) else {}
        if not isinstance(delta, dict):
            delta = {}

        frag = None
        field = "reasoning_content"
        for k in ("reasoning_content", "reasoning"):
            v = delta.get(k)
            if isinstance(v, str) and v:
                frag = v
                field = k
                break

        has_other_signal = any(
            delta.get(k) for k in ("content", "tool_calls")
        ) or data.get("usage") or (chs and chs[0].get("finish_reason"))

        if self._frags:
            # 正在缓冲密文分片
            if frag is not None and not has_other_signal:
                self._frags.append(frag)
                blob = "".join(self._frags)
                dec = decrypt_token(blob)
                if dec is not None:
                    return [self._mk_chunk(data2, self._field, dec)]
                if len(blob) > self.MAX_BUFFER:
                    return [self._mk_chunk(data2, self._field, self._flush_reason())]
                return []  # 还没凑齐，续等
            # 该 chunk 带正文/结束信号：先冲刷缓冲为占位符，再放行正文。
            # 注意：pre 里还带着这片的密文碎片，必须先剥掉再放行，
            # 否则一条密文碎片会混着正文泄漏给客户端。
            if frag is not None:
                delta.pop(field, None)
            # 占位符模板必须**干净**（不含本 chunk 的 content/tool_calls）：
            # 正文由 stripped 单独下发，否则占位符 chunk 里混带正文，客户端
            # 会看到先正文后思考的乱序。
            clean = {"choices": [{"delta": {}}]}
            stripped = "data: " + json.dumps(data2, ensure_ascii=False) + "\n"
            return self._emit_pending_then(stripped, template=clean)

        if frag is None:
            return [pre]

        # 新片段，当前没有缓冲
        if frag.startswith("gAAAA"):
            dec = decrypt_token(frag)
            if dec is not None:
                delta[field] = dec
                return ["data: " + json.dumps(data2, ensure_ascii=False) + "\n"]
            # 前缀对但解不开：大概率是分片的首片，开始缓冲
            self._frags = [frag]
            self._field = field
            if has_other_signal:
                # 同 chunk 里已有正文：立刻冲刷，再把原 chunk（扣掉密文）放行
                dec_text = self._flush_reason()
                delta.pop(field, None)
                rest = "data: " + json.dumps(data2, ensure_ascii=False) + "\n"
                if dec_text:
                    return [self._mk_chunk(data2, self._field, dec_text), rest]
                return [rest]
            return []
        if _looks_like_opaque_blob(frag):
            delta[field] = ENCRYPTED_PLACEHOLDER
            return ["data: " + json.dumps(data2, ensure_ascii=False) + "\n"]
        return [pre]

    def _emit_pending_then(self, tail: str, template: dict | None = None) -> list[str]:
        if not self._frags:
            return [tail]
        base = template
        if base is None:
            # [DONE] 或无模板可借：造一个最小 reasoning_content chunk
            base = {"choices": [{"delta": {}}]}
        text = self._flush_reason()
        if not text:
            return [tail]
        return [self._mk_chunk(base, self._field, text), tail]

    def finalize(self) -> list[str]:
        """流结束（未见 [DONE] 的异常收尾）：冲刷剩余缓冲。"""
        if not self._frags:
            return []
        return [self._mk_chunk({"choices": [{"delta": {}}]}, self._field,
                               self._flush_reason())]
