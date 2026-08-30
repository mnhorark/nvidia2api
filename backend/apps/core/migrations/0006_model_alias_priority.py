"""模型别名映射与路由优先级。

- `alias`：对外暴露的模型名，参与 `/v1/*` 的全局路由；留空则用上游原始 `model_name`
- `route_priority`：多个渠道暴露同一个名字时，数值大的胜出
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0005_channel")]

    operations = [
        migrations.AddField(
            model_name="aimodel",
            name="alias",
            field=models.CharField(blank=True, db_index=True, default="", max_length=256),
        ),
        migrations.AddField(
            model_name="aimodel",
            name="route_priority",
            field=models.IntegerField(db_index=True, default=0),
        ),
    ]
