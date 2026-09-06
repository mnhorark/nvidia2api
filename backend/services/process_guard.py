"""单进程契约：用文件锁保证同一数据目录上只有一个服务实例。

## 为什么需要这个

本项目的并发保护**全部是进程内状态**，没有任何跨进程协调：

| 状态 | 位置 | 多进程后果 |
|---|---|---|
| 全局并发请求闸门 `_active_count` | `api/openai_views.py` | `max_concurrent_requests` 被静默乘以 worker 数 |
| 上游 socket 阀门 `_upstream_active` | `api/openai_views.py` | 全局连接上限同样被稀释 |
| 登录失败限速桶 | `api/admin_views/common.py` | 爆破防护按 worker 数稀释 |
| `sysconfig` / `model_registry` 读缓存 | `services/` | 信号失效只在**本进程**生效，跨进程最长陈旧 = TTL |

也就是说：`uvicorn --workers 4` 或"两个容器挂同一个 data 卷"看起来能跑，
实际上所有闸门与缓存一致性都悄悄失效了——**不报错，只是保护消失**。
这类静默失效比崩溃危险得多，所以在启动阶段直接拒绝。

## 语义

- 锁是**建议锁 + 进程生命周期锁**：fd 保持打开，进程退出（含崩溃、被 kill）
  由操作系统释放，不存在需要清理的陈旧锁文件。
- 只在服务进程（uvicorn/gunicorn/runserver…）上强制，一次性管理命令
  （migrate / cleanlogs / shell）不受影响——见 `apps.core.apps.ready()` 的调用位置。
- 明确逃生阀：`ALLOW_MULTI_PROCESS=true` 跳过检查（自行承受闸门失效的后果）。
- 平台不支持文件锁时**失败开放**（记警告、照常启动），绝不因为守卫本身
  出问题而让服务起不来。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("nvidia2api.guard")

# 锁 fd 必须挂在模块级全局上存活整个进程生命周期：
# 局部变量返回后即被 GC，fd 关闭 = 锁立即释放，守卫形同虚设。
_LOCK_HANDLE = None

ENV_OVERRIDE = "ALLOW_MULTI_PROCESS"

# 被锁定的字节区间长度。PID 写在这段**之后**：Windows 的 LockFile 对锁定
# 区间连其它句柄的读操作都拒绝，若把 PID 写在 byte 0 就永远读不到诊断信息。
# POSIX 的 flock 是建议锁、不阻塞读写，同一布局在两个平台上语义一致。
_LOCK_BYTE_LEN = 1


class AlreadyRunning(RuntimeError):
    """同一 DATA_DIR 上已有另一个服务实例在跑。"""


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _lock_path() -> Path:
    from django.conf import settings
    return Path(settings.DATA_DIR) / ".gateway.lock"


def _try_lock(fd: int) -> bool:
    """非阻塞独占锁。成功 True，已被占用 False。"""
    try:
        import fcntl  # POSIX
    except ImportError:
        pass
    else:
        import errno
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise

    try:
        import msvcrt  # Windows
    except ImportError:
        return True  # 未知平台：失败开放，不阻塞启动
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTE_LEN)
        return True
    except OSError:
        return False


def _read_holder_pid(path: Path) -> str:
    """读锁文件里的持有者 PID（纯诊断，读不到就返回空）。

    必须从 `_LOCK_BYTE_LEN` 之后读：Windows 的 `LockFile` 锁定的字节区间
    对**其它句柄连读都拒绝**（同进程的第二个句柄也一样，实测
    PermissionError），从 0 开始读必然失败。
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return ""
    try:
        os.lseek(fd, _LOCK_BYTE_LEN, os.SEEK_SET)
        return os.read(fd, 64).decode("ascii", "ignore").strip()
    except OSError:
        return ""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_singleton_lock() -> bool:
    """获取数据目录的单实例锁。返回是否真正启用了守卫。

    已持有 / 显式放行 / 平台不支持 → True（放行）；
    检测到另一个实例 → 抛 `AlreadyRunning`。
    """
    global _LOCK_HANDLE

    if _LOCK_HANDLE is not None:      # 本进程已持有（ready() 可能被重复触发）
        return True
    if _truthy(os.environ.get(ENV_OVERRIDE)):
        logger.warning(
            "【单进程契约已被显式放行】%s=true：并发闸门"
            "（max_concurrent_requests / max_concurrent_upstream）、登录限速与"
            "运行时参数缓存均为**进程内状态**，多进程下会被按进程数静默稀释。"
            "如需水平扩展，请先把这些状态外置（Redis / PostgreSQL）。",
            ENV_OVERRIDE)
        return False

    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.warning("单实例锁文件无法创建（%s），跳过守卫", exc)
        return False

    try:
        if not _try_lock(fd):
            holder = _read_holder_pid(path)
            os.close(fd)
            raise AlreadyRunning(
                f"检测到另一个 nvidia2api 服务实例正在使用数据目录 "
                f"{path.parent}"
                + (f"（持有者 PID {holder}）" if holder else "")
                + "。本项目的并发闸门、登录限速与运行时参数缓存全部是**进程内**"
                "状态，多实例/多 worker 会让它们静默失效（不报错，只是保护消失）。"
                f"确需多进程请设置环境变量 {ENV_OVERRIDE}=true 并自行承担后果。")
    except AlreadyRunning:
        raise
    except OSError as exc:
        # 锁调用本身异常（平台语义差异等）：失败开放，绝不因守卫让服务起不来
        logger.warning("单实例锁获取异常（%s），跳过守卫", exc)
        os.close(fd)
        return False

    # 持有者 PID 写在被锁区间**之后**（见 _read_holder_pid 的说明）。
    # ftruncate 到 _LOCK_BYTE_LEN 保留锁字节，避免区间外残留旧 PID 尾巴。
    try:
        os.ftruncate(fd, _LOCK_BYTE_LEN)
        os.lseek(fd, _LOCK_BYTE_LEN, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
    except OSError:
        pass  # PID 只是诊断信息，写失败不影响锁的持有
    _LOCK_HANDLE = fd
    return True
