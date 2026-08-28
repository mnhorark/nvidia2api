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
        except Exception:  # noqa: BLE001
            pass

