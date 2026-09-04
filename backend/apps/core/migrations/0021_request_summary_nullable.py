# 修正 0020：request_summary 改为可空（诊断数据绝不阻塞主链路）。
# 0020 以 NOT NULL 建列，在"迁移已应用但服务进程仍持旧模型代码"的
# 部署时序下，旧代码 INSERT 不带该列会触发 IntegrityError 整包 500
# （2026-09-04 线上实爆）。诊断字段必须零风险。
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_add_request_summary'),
    ]

    operations = [
        migrations.AlterField(
            model_name='requestlog',
            name='request_summary',
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
    ]
