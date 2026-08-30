"""清理过期请求日志。

用法：
    python manage.py cleanlogs                 # 按系统参数 log_retention_days 清理
    python manage.py cleanlogs --days 7        # 显式指定保留天数
    python manage.py cleanlogs --dry-run       # 只统计不删除
    python manage.py cleanlogs --channel zen   # 只清理指定渠道
"""
from django.core.management.base import BaseCommand

from apps.core.models import Channel
from services.cleanup import clean_old_logs


class Command(BaseCommand):
    help = "Delete request logs older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None,
                            help="Retention days (0 = never clean). Overrides system setting.")
        parser.add_argument("--channel", type=str, default=None,
                            help="Channel slug to restrict cleanup to.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report count without deleting.")

    def handle(self, *args, **options):
        channel = None
        slug = options.get("channel")
        if slug:
            channel = Channel.objects.filter(slug=slug).first()
            if channel is None:
                self.stderr.write(f"channel {slug!r} not found")
                return
        result = clean_old_logs(days=options.get("days"), channel=channel,
                                dry_run=bool(options.get("dry_run")))
        if result.get("dry_run"):
            self.stdout.write(
                f"would delete {result['deleted']} logs (retention={result['retention_days']}d)")
        else:
            self.stdout.write(
                f"deleted {result['deleted']} logs (retention={result['retention_days']}d)")
