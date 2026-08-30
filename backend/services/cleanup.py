"""运行期数据维护：请求日志保留与清理。

个人长期运行的实例里，RequestLog 会随着每次请求不断增长，SQLite 单文件会越
来越大、查询越来越慢。`clean_old_logs` 按保留天数删除过期日志（保留天数取自
`log_retention_days` 系统参数，0 = 永不清理）。

供以下入口调用：
- 管理命令 `python manage.py cleanlogs [--days N]`
- 管理 API `POST /api/admin/logs/clean`
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.models import RequestLog
from services import sysconfig

logger = logging.getLogger("nvidia2api.cleanup")


def effective_retention_days(channel=None, explicit: int | None = None) -> int:
    """实际生效的保留天数：显式传入优先，否则读系统参数；0 表示不清理。"""
    if explicit is not None:
        return max(int(explicit), 0)
    return max(int(sysconfig.get("log_retention_days", channel) or 0), 0)


def clean_old_logs(days: int | None = None, channel=None, dry_run: bool = False) -> dict:
    """删除早于保留期限的请求日志，返回删除数量。

    `days` 显式覆盖系统参数；`channel` 为空时清理所有渠道。
    """
    retention = effective_retention_days(channel, days)
    if retention <= 0:
        return {"deleted": 0, "retention_days": retention, "dry_run": dry_run,
                "note": "retention disabled (0)"}
    cutoff = timezone.now() - timedelta(days=retention)
    qs = RequestLog.objects.filter(created_at__lt=cutoff)
    if channel is not None:
        qs = qs.filter(channel=channel)
    if dry_run:
        return {"deleted": qs.count(), "retention_days": retention, "dry_run": True,
                "cutoff": cutoff.isoformat()}
    with transaction.atomic():
        deleted, _ = qs.delete()
    logger.info("cleaned %d request logs older than %d days", deleted, retention)
    return {"deleted": deleted, "retention_days": retention, "dry_run": False,
            "cutoff": cutoff.isoformat()}
