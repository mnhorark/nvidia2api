"""敏感操作审计。

设计约束（按重要性排序）：

1. **绝不阻塞主链路**。审计写库失败只能记日志，不能让"回看 Key"因为
   审计表写不进去就 500——那会把安全功能变成可用性故障。
2. **绝不记录敏感值本身**。记的是"谁在什么时候看了哪条记录"，
   不是"看到了什么"。被回看的明文一旦进审计表，审计表就成了新的泄漏面。
3. 保留 `forwarded_for` 原文与 `remote_addr` 两列：反代后面的部署里
   `REMOTE_ADDR` 恒为网关地址，只有 XFF 能定位真实来源；但 XFF 可伪造，
   所以两份都存，取证时对照。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nvidia2api.audit")


def _client_addr(request) -> tuple[str, str]:
    """返回 (remote_addr, x_forwarded_for 原文)。"""
    if request is None:
        return "", ""
    meta = getattr(request, "META", None) or {}
    remote = str(meta.get("REMOTE_ADDR") or "")[:64]
    xff = str(request.headers.get("X-Forwarded-For", "") or "")[:256]
    return remote, xff


def log_secret_access(action: str, *, request=None, channel=None,
                      target=None) -> bool:
    """写一条敏感操作审计。返回是否写入成功（调用方通常忽略）。

    `target` 是被访问的对象（如 ChannelKey 实例）；只取 id 与 name。
    """
    from apps.core.models import SecretAccessLog

    remote, xff = _client_addr(request)
    try:
        SecretAccessLog.objects.create(
            action=action,
            channel=channel if channel is not None else getattr(target, "channel", None),
            target_id=getattr(target, "pk", None),
            target_name=str(getattr(target, "name", "") or "")[:128],
            remote_addr=remote,
            forwarded_for=xff,
            user_agent=str(
                (getattr(request, "headers", None) or {}).get("User-Agent", "")
                or "")[:256],
        )
        return True
    except Exception:  # noqa: BLE001
        # 审计失败必须可见（否则安全功能静默失效），但绝不影响本次请求
        logger.exception("敏感操作审计写入失败 action=%s target=%s",
                         action, getattr(target, "pk", None))
        return False
