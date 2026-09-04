"""SSE 转发管线的单点解析观察器（StreamTap）。

重构背景（2026-09 传输层审查）：
  旧实现里同一条流上的每个 chunk 会被 json.loads 最多 6 次：
  竞速判胜 1 次、解密器 2 次、工具流规整 1 次、_chunk_has_content 1 次、
  _stream_response 记账循环 1 次（_drain 里还有一次 signal 探测）。
  其中 3 次发生在纯转发路径（_drain 探测 + sent_content 判定 + 记账），
  且两个探测器（_chunk_has_content / _chunk_has_any_signal）语义纠缠、
  各自维护一份字段清单，行为已经分叉。

本模块把"转发路径上的观察"收敛为**单次解析**：

  - `StreamTap.feed(chunk)` 对每个 chunk 只做一次 json.loads（失败按
    非 JSON 处理，绝不舍弃/改写 chunk——观察者无权动数据流）；
  - 产物 TapInfo 是只读快照，供 _drain（signal 判定切换超时档）与
    _stream_response（sent_content / usage / finish / 截断检测 / token
    估算）共享；
  - 语义与旧探测器逐一对齐：
      sent_content（换线重试闸门）   = _chunk_has_content 累积
      seen_signal（超时档位切换）   = _chunk_has_any_signal 单帧
    两者的差异被显式表达为两个字段（has_payload vs has_reasoning），
    不再隐式分叉。

透传纯度保证：feed() 永远返回原 chunk，不改一个字节。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class TapInfo:
    """单个 chunk 的观察快照（只读语义，字段全部一次性生成）。"""

    # data: 前缀的结构合法 JSON 帧（[DONE] 与注释行均为 False）
    is_data: bool = False
    # 上游显式终结帧 [DONE]
    saw_done: bool = False
    # 任何"流还活着"的信号：数据帧/[DONE]（比旧探测器口径宽一档，
    # 仅作通用参考；精确判定用下面的分项字段）
    has_signal: bool = False
    # 用户可见负载：正文/补全 text/工具调用增量
    # （旧 _chunk_has_content 的内容项；reasoning 不在此列——仅吐过
    # role/思考帧的流在旧语义下仍允许透明换线重试）
    has_payload: bool = False
    # 思考增量（参与超时档位切换，但不参与重试闸门）
    has_reasoning: bool = False
    # usage 出现（capture 到 self.usage）
    has_usage: bool = False
    # finish_reason 出现（记录到 self.finish_reason）
    has_finish: bool = False


@dataclass
class StreamTap:
    """跨 chunk 有状态观察器。每个转发流一个实例。

    用法：
        tap = StreamTap()
        # _drain 内每收到一个 chunk：
        info = tap.feed(chunk)      # 单次解析，返回该 chunk 快照
        # _stream_response 在 yield 同一 chunk 后读 tap 累积态。
    """

    # 累积态 ---------------------------------------------------------------
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""
    completion_parts: list = field(default_factory=list)   # 正文碎片
    reasoning_parts: list = field(default_factory=list)    # 思考碎片（估算用）
    chunk_count: int = 0
    # 累积：重试闸门（对齐旧 _chunk_has_content：
    # usage/finish/[DONE]/content/text/tool_calls 之一出现过）
    sent_signal: bool = False
    # 累积：上游是否发过显式 [DONE]（截断检测的另一半依据）
    saw_done: bool = False
    # 最近一次 feed 的快照（_drain 与消费方按 yield 顺序共享）
    last: TapInfo = field(default_factory=TapInfo)

    # 汇总读取 -------------------------------------------------------------

    @property
    def sent_content(self) -> bool:
        """已向客户端交付过任何内容/信号（决定能否透明换线重试）。

        语义严格对齐旧 _chunk_has_content 的累积：usage/finish/[DONE]/
        正文/工具帧都已到达客户端，换线重发会造成重复交付。纯思考/纯
        role 标记帧不算——与旧口径一致（那时换线重试仍然安全）。
        """
        return self.sent_signal

    @property
    def completion_text(self) -> str:
        return "".join(self.completion_parts)

    @property
    def reasoning_text(self) -> str:
        return "".join(self.reasoning_parts)

    # 主流程 ---------------------------------------------------------------

    def feed(self, chunk: str) -> TapInfo:
        """观察一个转发中的 chunk。**绝不修改 chunk 本身**。"""
        info = TapInfo()
        self.chunk_count += 1
        if not chunk.startswith("data:"):
            # 上游注释行（`: keep-alive`）等：不解析、不算信号
            self.last = info
            return info
        payload = chunk[5:].strip()
        if payload == "[DONE]":
            info.saw_done = True
            info.has_signal = True
            self.sent_signal = True
            self.saw_done = True
            self.last = info
            return info
        try:
            data = json.loads(payload)
        except Exception:  # noqa: BLE001
            # 非 JSON data 帧：不算信号（与旧探测器口径一致），透传不管
            self.last = info
            return info
        if not isinstance(data, dict):
            self.last = info
            return info
        info.is_data = True
        info.has_signal = True
        if data.get("usage"):
            info.has_usage = True
            self.usage = data["usage"]
            self.sent_signal = True
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                if first.get("finish_reason"):
                    info.has_finish = True
                    self.finish_reason = str(first["finish_reason"])
                    self.sent_signal = True
                delta = first.get("delta")
                if isinstance(delta, dict):
                    for key, bucket in (
                            ("content", self.completion_parts),
                            ("text", self.completion_parts)):
                        v = delta.get(key)
                        if isinstance(v, str) and v:
                            info.has_payload = True
                            bucket.append(v)
                    for key in ("reasoning_content", "reasoning"):
                        v = delta.get(key)
                        if isinstance(v, str) and v:
                            info.has_reasoning = True
                            self.reasoning_parts.append(v)
                    if delta.get("tool_calls"):
                        info.has_payload = True
                msg = first.get("message")
                # 非流式兼容（防御性）：竞速外转发一般不会有 message
                if isinstance(msg, dict) and isinstance(msg.get("content"), str) \
                        and msg["content"]:
                    info.has_payload = True
                    self.completion_parts.append(msg["content"])
        if info.has_payload:
            self.sent_signal = True
        self.last = info
        return info
