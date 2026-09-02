"""回填 channel_key.api_key_hint：对存量加密 Key 逐条解密算一次提示掩码。

随着 api_key_hint 上线，列表接口不再逐行 Fernet 解密（千级 Key 列表页延迟的
主要来源）。本迁移只在升级时跑一次；解密失败的行（换过密钥）留空提示位，
序列化器会落回运行时解密兜底。
"""
from django.db import migrations


def backfill(apps, schema_editor):
    ChannelKey = apps.get_model("core", "ChannelKey")
    from services.crypto import decrypt_secret, mask_secret
    db_alias = schema_editor.connection.alias
    qs = ChannelKey.objects.using(db_alias).exclude(api_key="")
    batch = []
    for row in qs.only("id", "api_key").iterator(chunk_size=500):
        plain = decrypt_secret(row.api_key)
        # 解密失败（密钥轮换过）就留空，不落脏提示
        row.api_key_hint = mask_secret(plain) if plain else ""
        batch.append(row)
        if len(batch) >= 500:
            ChannelKey.objects.using(db_alias).bulk_update(batch, ["api_key_hint"])
            batch = []
    if batch:
        ChannelKey.objects.using(db_alias).bulk_update(batch, ["api_key_hint"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_channelkey_api_key_hint"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
