"""把滞留的 `pending` 请求日志归一为 `failed`。

用法：
    python manage.py settle_stale_pending              # dry-run，只报告
    python manage.py settle_stale_pending --apply      # 真正写库
    python manage.py settle_stale_pending --older-than 60 --apply
    python manage.py settle_stale_pending --channel zen --apply

为什么需要它（B5 的存量部分）：
非流式路径此前只在 `finally` 里释放信号量与上游额度，**不结算日志**。
于是 `RequestLog.objects.create` 之后、`_finish_log` 之前抛出的任何未捕获异常
都会留下一行永久 `status="pending"` 的记录。代码侧的兜底已经补上
（`openai_views._force_settle_non_stream`），但**存量行不会自己消失**：
实测库里 630 条 pending、最早 9 天前，而仪表盘的成功率与平均延迟
分母都含 pending —— 统计被系统性拉低，且和真实故障混在一起看不出区别。

`--older-than` 的默认值（60 分钟）远大于任何合法请求时长：
一条正在流式输出的请求其日志也是 pending，阈值太短会把活请求改掉。
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import Channel, RequestLog


class Command(BaseCommand):
    help = "Normalize stale pending request logs to failed (unhandled_error)."

    def add_arguments(self, parser):
        parser.add_argument("--older-than", type=int, default=60,
                            help="Only touch pending rows older than N minutes "
                                 "(default 60 — must exceed any real request).")
        parser.add_argument("--channel", type=str, default=None,
                            help="Channel slug to restrict to.")
        parser.add_argument("--apply", action="store_true",
                            help="Actually write. Without it this is a dry run.")
        parser.add_argument("--batch", type=int, default=500,
                            help="Rows per UPDATE, to avoid holding the SQLite "
                                 "write lock too long.")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=options["older_than"])
        qs = RequestLog.objects.filter(
            status="pending", created_at__lt=cutoff).order_by("id")

        slug = options.get("channel")
        if slug:
            channel = Channel.objects.filter(slug=slug).first()
            if channel is None:
                self.stderr.write(f"channel {slug!r} not found")
                return
            qs = qs.filter(channel=channel)

        total = qs.count()
        if not options["apply"]:
            self.stdout.write(
                f"[dry-run] 会归一 {total} 条 pending 日志"
                f"（早于 {options['older_than']} 分钟，error_type=unhandled_error）"
                f"；加 --apply 才真正写库")
            return

        if total == 0:
            self.stdout.write("没有需要归一的 pending 日志")
            return

        batch = max(1, int(options["batch"]))
        changed = 0
        # 分批 + 每批一个事务：SQLite 长事务会持写锁过久，拖垮在线请求
        ids = list(qs.values_list("id", flat=True))
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            with transaction.atomic():
                # RequestLog 不继承 Timestamped，没有 updated_at 字段
                changed += RequestLog.objects.filter(
                    pk__in=chunk, status="pending",
                ).update(
                    status="failed",
                    http_status=500,
                    error_type="unhandled_error",
                )
        self.stdout.write(
            f"已归一 {changed} 条 pending 日志为 failed/unhandled_error")
