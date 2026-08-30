"""引入 Channel（上游渠道）模型，并把 Keys / 代理 / 分组 / 模型 / 日志 / 设置挂到渠道上。

兼容处理：
- `NvidiaApiKey` 重命名为 `ChannelKey`（数据保留），因为渠道不再只有 NVIDIA
- 已存在的行统一回填到自动创建的默认 NVIDIA 渠道
- 唯一性约束从「全局唯一」改为「渠道内唯一」，同名模型可共存于不同渠道
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _split_endpoint(url, suffix="/chat/completions"):
    raw = (url or "").strip()
    if not raw:
        return "", suffix
    u = raw.rstrip("/")
    if u.lower().endswith(suffix.lower()):
        return u[: -len(suffix)], suffix
    return u, suffix


def seed_default_channel(apps, schema_editor):
    import sys
    # 测试库会反复重建并自建 fixture，跳过种子数据避免 slug 冲突
    if "pytest" in sys.modules or "test" in sys.argv:
        return
    Channel = apps.get_model("core", "Channel")
    base_url, chat_path = _split_endpoint(
        getattr(settings, "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    )
    channel, _ = Channel.objects.get_or_create(
        slug="nvidia",
        defaults={
            "name": "NVIDIA",
            "base_url": base_url,
            "chat_path": chat_path,
            "models_path": "/models",
            "key_prefix": "nvapi",
            "auth_scheme": "bearer",
            "default_rpm": getattr(settings, "DEFAULT_NVIDIA_RPM", 40),
            "enabled": True,
            "is_default": True,
        },
    )
    for model_name in ("ChannelKey", "Proxy", "ProxyGroup", "AIModel",
                       "RequestLog", "SystemSetting"):
        model = apps.get_model("core", model_name)
        model.objects.filter(channel__isnull=True).update(channel=channel)


def unseed_default_channel(apps, schema_editor):
    apps.get_model("core", "Channel").objects.filter(slug="nvidia").delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0004_requestlog_cached_tokens_requestlog_first_token_ms")]

    operations = [
        migrations.CreateModel(
            name="Channel",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=64, unique=True)),
                ("slug", models.SlugField(max_length=64, unique=True)),
                ("base_url", models.CharField(max_length=512)),
                ("chat_path", models.CharField(default="/chat/completions", max_length=128)),
                ("models_path", models.CharField(default="/models", max_length=128)),
                ("key_prefix", models.CharField(blank=True, default="", max_length=32)),
                ("auth_scheme", models.CharField(
                    choices=[("bearer", "Bearer Token"),
                             ("x_api_key", "X-API-Key Header"),
                             ("none", "无鉴权")],
                    default="bearer", max_length=16)),
                ("default_rpm", models.IntegerField(default=40)),
                ("enabled", models.BooleanField(db_index=True, default=True)),
                ("is_default", models.BooleanField(db_index=True, default=False)),
                ("notes", models.TextField(blank=True, default="")),
            ],
            options={"db_table": "channel"},
        ),

        migrations.RenameModel(old_name="NvidiaApiKey", new_name="ChannelKey"),
        migrations.AlterModelTable(name="channelkey", table="channel_key"),

        # ---- 挂载 channel 外键 ----
        migrations.AddField(
            model_name="channelkey", name="channel",
            field=models.ForeignKey(blank=True, db_index=True, null=True,
                                    on_delete=django.db.models.deletion.CASCADE,
                                    related_name="keys", to="core.channel"),
        ),
        migrations.AddField(
            model_name="proxy", name="channel",
            field=models.ForeignKey(blank=True, db_index=True, null=True,
                                    on_delete=django.db.models.deletion.CASCADE,
                                    related_name="proxies", to="core.channel"),
        ),
        migrations.AddField(
            model_name="proxygroup", name="channel",
            field=models.ForeignKey(blank=True, db_index=True, null=True,
                                    on_delete=django.db.models.deletion.CASCADE,
                                    related_name="proxy_groups", to="core.channel"),
        ),
        migrations.AddField(
            model_name="aimodel", name="channel",
            field=models.ForeignKey(blank=True, db_index=True, null=True,
                                    on_delete=django.db.models.deletion.CASCADE,
                                    related_name="models", to="core.channel"),
        ),
        migrations.AddField(
            model_name="requestlog", name="channel",
            field=models.ForeignKey(blank=True, db_index=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="logs", to="core.channel"),
        ),
        migrations.AddField(
            model_name="systemsetting", name="channel",
            field=models.ForeignKey(blank=True, db_index=True, null=True,
                                    on_delete=django.db.models.deletion.CASCADE,
                                    related_name="settings", to="core.channel"),
        ),

        # ---- 唯一性：全局 -> 渠道内 ----
        migrations.AlterField(
            model_name="channelkey", name="api_key",
            field=models.CharField(max_length=256),
        ),
        migrations.AlterField(
            model_name="proxygroup", name="name",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name="aimodel", name="model_name",
            field=models.CharField(max_length=256),
        ),
        migrations.AlterField(
            model_name="systemsetting", name="key",
            field=models.CharField(max_length=64),
        ),

        migrations.RemoveConstraint(
            model_name="proxy", name="unique_proxy_endpoint",
        ),

        migrations.AddConstraint(
            model_name="channelkey",
            constraint=models.UniqueConstraint(fields=("channel", "api_key"),
                                               name="unique_channel_key"),
        ),
        migrations.AddConstraint(
            model_name="proxygroup",
            constraint=models.UniqueConstraint(fields=("channel", "name"),
                                               name="unique_channel_proxy_group"),
        ),
        migrations.AddConstraint(
            model_name="proxy",
            constraint=models.UniqueConstraint(
                fields=("channel", "protocol", "host", "port", "username"),
                name="unique_channel_proxy_endpoint"),
        ),
        migrations.AddConstraint(
            model_name="aimodel",
            constraint=models.UniqueConstraint(fields=("channel", "model_name"),
                                               name="unique_channel_model"),
        ),
        migrations.AddConstraint(
            model_name="systemsetting",
            constraint=models.UniqueConstraint(fields=("channel", "key"),
                                               name="unique_channel_setting"),
        ),

        migrations.RunPython(seed_default_channel, unseed_default_channel),
    ]
