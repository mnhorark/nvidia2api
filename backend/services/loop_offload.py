"""把事件循环上的同步 DB 调用移到线程池执行（防"整站卡死"）。

背景（生产卡死主链）：本服务是单进程 uvicorn，流式响应是事件循环上的
async 生成器；其中的同步 DB 写（RequestLog.save / 渠道健康 / Key 与代理
统计）在 SQLite 写锁被占时会按 busy_timeout（默认 30s）**阻塞事件循环
线程**——期间所有在途流、新请求、/healthz 全部停摆；健康检查连续超时后
容器被 restart，用户看到的就是"经常卡死 + 流莫名断开"。

安全边界（为什么不能无条件移线程）：
- 当前连接处于原子事务块内（请求事务 / Django TestCase 的测试事务）时，
  **必须留在本线程执行**——另一个线程是独立连接，既看不到该事务的未提交
  写入，也无法参与回滚。生产请求路径的 async 生成器不在原子块内，可安全
  移出；测试（TestCase/django_db 自动包事务）自动回落同步路径，行为与
  优化前完全一致。
"""
from __future__ import annotations

import asyncio
import logging

from django.conf import settings

logger = logging.getLogger("nvidia2api.offload")


def _in_transaction() -> bool:
    try:
        from django.db import connection
        return bool(connection.in_atomic_block)
    except Exception:  # noqa: BLE001
        # DB 未就绪等极端场景按"不可移线程"处理（保守）
        return True


async def run_db(func, /, *args, **kwargs):
    """执行同步 DB 调用：事务块内同线程直调，否则挪到线程池（不阻塞事件循环）。

    测试模式（settings.TESTING）下永远同线程直调：Django TestCase 的事务
    绑定在主线程连接上，而流式生成器可能被 asgiref 调度到 executor 线程——
    那里的 thread-local 连接看不到原子块，`to_thread` 的写入会绕过测试事务
    直接提交，向复用的测试库泄漏脏行（曾污染 Dashboard 聚合断言）。
    """
    if _in_transaction() or getattr(settings, "TESTING", False):
        return func(*args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)
