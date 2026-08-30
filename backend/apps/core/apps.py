from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        # Auto-apply migrations at server start so a fresh database is usable
        # immediately (idempotent; harmless if tables already exist).
        import sys
        from django.core.management import call_command

        if any(cmd in sys.argv for cmd in ("migrate", "makemigrations", "test", "pytest")):
            return
        try:
            call_command("migrate", run_syncdb=True, verbosity=0)
        except Exception as exc:  # noqa: BLE001
            # 启动阶段迁移失败不能静默吞掉：记录日志，避免运行期才报 "no such table"
            import logging
            logging.getLogger("django").error("启动时自动迁移失败: %s", exc)

