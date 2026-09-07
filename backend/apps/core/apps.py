"""apps.core 应用配置。

历史上 `ready()` 会对**任意**命令（含 cleanlogs、shell、一次性脚本）自动执行
migrate。这带来三个问题：
1. 启动即开库——不该碰数据库的命令也被迫连库；
2. 多进程/多 worker 并发启动时互相抢锁；
3. pytest 下的 "APPS_NOT_READY" 警告也源于此。

现在只在真正的**服务进程**入口（uvicorn / gunicorn / daphne / runserver /
Docker CMD 里显式调用的 `manage.py migrate` 之后）自动迁移；其余命令完全
不碰数据库。生产推荐的显式迁移路径（见 Dockerfile CMD）不受影响。
"""
import os
import sys
from pathlib import Path

from django.apps import AppConfig

# 明确不需要自动迁移的管理/离线命令
_SKIP_COMMANDS = frozenset({
    "migrate", "makemigrations", "squashmigrations", "showmigrations",
    "sqlmigrate", "test", "pytest", "collectstatic", "shell", "dbshell",
    "dumpdata", "loaddata", "flush", "check", "diffsettings", "inspectdb",
})

# 服务进程特征：可执行文件名或命令行片段命中即认为是常驻服务
_SERVER_MARKERS = ("uvicorn", "gunicorn", "daphne", "hypercorn", "waitress")


def _is_server_process() -> bool:
    """当前进程是否为常驻服务进程（而非一次性管理命令）。

    ⚠ 这里**只看命令行形态**，不看 `AUTO_MIGRATE`。

    旧实现把两件事揉进一个判断：`AUTO_MIGRATE=0` 时直接 `return False`，
    而 `ready()` 又拿这个返回值当**单进程守卫的前置门**——于是"我在 entrypoint
    里自己控制迁移时机"这个纯部署偏好，实际语义变成了"关掉迁移 **并且** 关掉
    单进程守卫"。守卫静默不生效和守卫不存在是同一件事：第二个实例会照常起来，
    并发闸门 / 登录限速 / 参数缓存全部静默失效。
    现在两者拆开：守卫只认"是不是服务进程"，迁移只认 `AUTO_MIGRATE`。
    """
    argv = sys.argv or [""]
    entry = Path(argv[0]).name.lower() if argv[0] else ""
    if any(marker in entry for marker in _SERVER_MARKERS):
        return True
    # `python -m uvicorn ...`：argv[0] 是 `.../uvicorn/__main__.py`，入口名
    # 只剩 `__main__.py`，光看它认不出来。**start.bat 的两个启动分支用的都是
    # `python -m uvicorn`**，漏掉这条就等于 Windows 部署上守卫与自动迁移双双失效。
    # 判据取被 `-m` 执行的模块名——即 `__main__.py` 的父目录名。
    if entry == "__main__.py":
        module = Path(argv[0]).parent.name.lower()
        if any(marker in module for marker in _SERVER_MARKERS):
            return True
    return "runserver" in argv


def _is_reloader_child() -> bool:
    """当前进程是否为 Django autoreloader 起的**子进程**。

    `manage.py runserver` 带 reloader 时是父子两个进程：父进程只做文件监视，
    但它同样会 `django.setup()` → 触发本 app 的 `ready()` → **先拿到
    `data/.gateway.lock`**；子进程带 `RUN_MAIN=true` 环境变量、argv 与父进程
    相同（仍是 `runserver ...`，没有 `--noreload`），它才是真正服务的那个。

    所以子进程必须跳过单进程守卫，否则它会与自己的父进程抢同一把锁 →
    `AlreadyRunning` → 子进程崩 → `manage.py runserver` 只剩一个不服务的
    文件监视器（README 与 docs/deployment.md 的文档化本地启动路径直接失效）。

    历史说明：旧实现里这条豁免是**间接**成立的——父进程迁移完把
    `AUTO_MIGRATE` 置 0，子进程继承后 `_is_server_process()` 返回 False，
    于是连守卫一起跳过。把守卫与迁移开关解耦之后必须显式补回来，
    否则"修一个耦合"会顺手弄坏开发模式。

    豁免不削弱契约：第二个**独立实例**的父进程仍会去抢锁并被拒。
    `uvicorn --reload` 不需要这条（其 supervisor 不 import 应用，只有子进程
    跑 `ready()`，正常拿锁即可）；gunicorn/uvicorn `--workers N` 也不该走这里
    —— 那正是要被守卫拒掉的形态。
    """
    return os.environ.get("RUN_MAIN", "").strip().lower() in ("1", "true", "yes", "on")


def _should_acquire_singleton_lock() -> bool:
    """本进程是否应当争夺单实例锁。抽成纯函数以便把这条不变量写成测试。"""
    if not _is_server_process():
        return False
    if _is_reloader_child():
        return False
    return True


def _auto_migrate_enabled() -> bool:
    """是否在本进程内自动执行 migrate。纯部署开关，**不影响单进程守卫**。

    默认开启；`AUTO_MIGRATE=0/false/no/off` 让部署方在 entrypoint 里自行迁移。
    """
    override = os.environ.get("AUTO_MIGRATE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return True


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        if any(cmd in sys.argv for cmd in _SKIP_COMMANDS):
            return
        if not _is_server_process():
            return
        # 单进程契约：并发闸门/登录限速/运行时参数缓存/仪表盘 usage 缓存都是
        # 进程内状态，多实例共享同一 DATA_DIR 时它们会**静默**失效。
        # 必须在碰数据库之前先拒绝第二个实例（放在 migrate 之前，避免半启动状态），
        # 且**不受 AUTO_MIGRATE 影响**——见 _is_server_process 的说明。
        # 唯一豁免是 Django autoreloader 的子进程（锁已被它的父进程持有），
        # 见 _is_reloader_child / _should_acquire_singleton_lock。
        from services.process_guard import AlreadyRunning, acquire_singleton_lock

        if _should_acquire_singleton_lock():
            try:
                acquire_singleton_lock()
            except AlreadyRunning as exc:
                import logging

                logging.getLogger("django").error("%s", exc)
                raise
        if not _auto_migrate_enabled():
            return
        from django.core.management import call_command

        try:
            call_command("migrate", run_syncdb=True, verbosity=0)
            # 已迁移过就不再重复：`uvicorn --reload` 会 fork/exec 子进程并继承
            # 环境变量，这里打个标记可避免父子进程同时跑迁移抢 SQLite 写锁。
            # 现在这个标记只抑制迁移，不再抑制守卫。
            os.environ["AUTO_MIGRATE"] = "0"
        except Exception as exc:  # noqa: BLE001
            # 启动阶段迁移失败不能静默吞掉：记录日志，避免运行期才报 "no such table"
            import logging

            logging.getLogger("django").error("启动时自动迁移失败: %s", exc)

