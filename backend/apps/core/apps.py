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
    """当前进程是否为常驻服务进程（而非一次性管理命令）。"""
    # 允许部署方显式开关，便于在 entrypoint 里自行控制迁移时机
    override = os.environ.get("AUTO_MIGRATE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    argv = sys.argv or [""]
    entry = Path(argv[0]).name.lower() if argv[0] else ""
    if any(marker in entry for marker in _SERVER_MARKERS):
        return True
    return "runserver" in argv


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        if any(cmd in sys.argv for cmd in _SKIP_COMMANDS):
            return
        if not _is_server_process():
            return
        from django.core.management import call_command

        try:
            call_command("migrate", run_syncdb=True, verbosity=0)
            # 已迁移过就不再重复：`uvicorn --reload` 会 fork/exec 子进程并继承
            # 环境变量，这里打个标记可避免父子进程同时跑迁移抢 SQLite 写锁。
            os.environ["AUTO_MIGRATE"] = "0"
        except Exception as exc:  # noqa: BLE001
            # 启动阶段迁移失败不能静默吞掉：记录日志，避免运行期才报 "no such table"
            import logging

            logging.getLogger("django").error("启动时自动迁移失败: %s", exc)
