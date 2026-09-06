"""request_log 索引重排：为管理端读路径建复合索引，换掉被前缀覆盖的旧索引。

**为什么用 SeparateDatabaseAndState**：四个字段只是 `db_index` 由 True 变 False，
列定义本身没变。若直接写 AlterField，SQLite 会走"建新表 → 整表拷贝 → 删旧表 →
改名"的重建路径（实测 500MB / 2.4 万行耗时 ~25s）。本表**没有周期清理、只增不
减**，容器启动时又会自动 migrate，重建时间会随数据量线性恶化，最终把启动拖成
分钟级。这里把"模型状态"与"数据库动作"分开：状态侧照常 AlterField，数据库侧只
做毫秒级的 DROP INDEX / CREATE INDEX，语义完全等价。

净效果：索引数 8 → 7（与本项目原始索引条数持平，写热表的维护成本不升），
读路径实测（500MB / 2.4 万行生产库副本）：
  - 仪表盘 usage 7~30 天：681ms → 82ms（0024 的 usage_cover）
  - DashboardView 整体：34ms → 2.2ms（channel_created 修掉今日聚合的 58ms）
  - 日志页默认翻页 limit=100：82ms → 5.7ms（defer + channel_id）
  - 日志页 limit=500：208ms → 25ms
  - 日志页按状态筛选：87ms → 19ms
  - 日志页按模型筛选：44ms → 5.4ms

**服务列表的三条索引都以 `id` 收尾**：日志页恒 `ORDER BY -id` + LIMIT，若索引
少了 id，planner 会先用 channel 前缀捞出该渠道全部 rowid 再建 TEMP B-TREE 排序
——实测比不加索引还慢（7.4ms → 35ms）。这是本轮踩过的坑，写在这里防止回退。
"""
import django.db.models.deletion
from django.db import migrations, models


# 被新复合索引前缀覆盖的自动索引（字段 db_index=True 生成），逐条显式删除。
# 名字来自 Django 的自动命名规则，与 sqlite_master 实测一致。
# reverse_sql 留空：回滚这条迁移的正确做法是重新跑 0024 之前的模型状态，
# 手工重建自动命名索引极易与 Django 的命名规则漂移，反而制造"库里有、状态里
# 没有"的脏索引。需要回滚请走备份恢复。
_DROPPED_AUTO_INDEXES = (
    "request_log_channel_id_b4e0f3c5",   # channel FK（被三条 (channel, …) 前缀覆盖）
    "request_log_created_at_f5fdd6ba",   # created_at（被 usage_cover 前缀覆盖）
    "request_log_model_f221ff88",        # model（被 channel_model 覆盖：唯一的
                                         # model 过滤来自日志页，恒带 channel）
    "request_log_status_0bcb2528",       # status（同理，被 channel_status 覆盖）
)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_requestlog_usage_cover"),
    ]

    operations = [
        # Meta 里定义的 (created_at, status) 复合索引：usage_cover 的
        # (created_at, status, …) 前缀完全覆盖它，走常规 RemoveIndex（本就很快）。
        migrations.RemoveIndex(
            model_name="requestlog",
            name="request_log_created_f29d6d_idx",
        ),
        # 四个字段仅 db_index 变化：状态侧记为 AlterField，数据库侧只删索引，
        # 避免 SQLite 整表重建。
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="requestlog",
                    name="channel",
                    field=models.ForeignKey(
                        blank=True, db_index=False, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="logs", to="core.channel",
                    ),
                ),
                migrations.AlterField(
                    model_name="requestlog",
                    name="created_at",
                    field=models.DateTimeField(auto_now_add=True, db_index=False),
                ),
                migrations.AlterField(
                    model_name="requestlog",
                    name="model",
                    field=models.CharField(db_index=False, max_length=256),
                ),
                migrations.AlterField(
                    model_name="requestlog",
                    name="status",
                    field=models.CharField(db_index=False, default="pending",
                                           max_length=16),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=f'DROP INDEX IF EXISTS "{name}"',
                    reverse_sql=migrations.RunSQL.noop,
                )
                for name in _DROPPED_AUTO_INDEXES
            ],
        ),
        # 新的渠道内复合索引（常规 AddIndex，建索引本身是毫秒级）。
        # 服务日志列表的三条都以 `id` 收尾：该页恒 `ORDER BY -id` + LIMIT，
        # 少了 id 就会退化成"捞出渠道全部 rowid 再 TEMP B-TREE 排序"。
        migrations.AddIndex(
            model_name="requestlog",
            index=models.Index(
                fields=["channel", "created_at", "status", "duration_ms"],
                name="request_log_channel_created",
            ),
        ),
        migrations.AddIndex(
            model_name="requestlog",
            index=models.Index(
                fields=["channel", "id"],
                name="request_log_channel_id",
            ),
        ),
        migrations.AddIndex(
            model_name="requestlog",
            index=models.Index(
                fields=["channel", "status", "id"],
                name="request_log_channel_status",
            ),
        ),
        migrations.AddIndex(
            model_name="requestlog",
            index=models.Index(
                fields=["channel", "model", "id"],
                name="request_log_channel_model",
            ),
        ),
    ]
