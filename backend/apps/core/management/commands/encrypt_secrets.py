"""把存量明文敏感字段补加密为 `enc:v1:` 密文。

## 为什么需要

`ChannelKey.api_key` / `Proxy.password` 的加密是在 `save()` 里做的——
**只有加密功能上线之后写入的行才是密文**。历史行永远不会被自动改写，
于是库里长期并存两种形态，而"敏感字段加密保存"这条要求实际上是部分失效的
（实测某部署：1262 把 Key 里 346 条明文、1212 个代理里 500 条明文）。

读路径有明文回落（`decrypt_secret` 对无前缀值原样返回），所以功能上看不出问题，
**这正是它一直没被发现的原因**。

## 语义

- 幂等：只处理无 `enc:v1:` 前缀且非空的行，重复执行安全。
- 分批提交：SQLite 单写者，一次性 UPDATE 上万行会长时间持锁，
  把在线请求的统计写入全部堵住。
- 默认 `--dry-run` 不写库，先看影响面。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Channel, ChannelKey, Proxy
from services.crypto import encrypt_secret

PREFIX = "enc:v1:"
_BATCH = 500


class Command(BaseCommand):
    help = "将存量明文 API Key / 代理密码补加密为 enc:v1: 密文（幂等，可重复执行）"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="真正写库；不加此开关只统计不修改")
        parser.add_argument("--channel", default="",
                            help="只处理指定渠道 slug（默认全部渠道）")

    def handle(self, *args, **opts):
        apply_changes = bool(opts["apply"])
        slug = (opts.get("channel") or "").strip()

        key_qs = ChannelKey.objects.exclude(api_key="").exclude(
            api_key__startswith=PREFIX)
        pw_qs = Proxy.objects.exclude(password="").exclude(
            password__startswith=PREFIX)
        if slug:
            channel = Channel.objects.filter(slug=slug).first()
            if channel is None:
                raise CommandError(f"未知渠道 slug: {slug}")
            key_qs = key_qs.filter(channel=channel)
            pw_qs = pw_qs.filter(channel=channel)

        keys = list(key_qs.values_list("id", "api_key"))
        proxs = list(pw_qs.values_list("id", "password"))
        self.stdout.write(
            f"待加密：ChannelKey {len(keys)} 条，Proxy.password {len(proxs)} 条")

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "dry-run（未写库）。确认影响面后加 --apply 执行"))
            return

        done = 0
        for model, rows, field in ((ChannelKey, keys, "api_key"),
                                   (Proxy, proxs, "password")):
            for i in range(0, len(rows), _BATCH):
                chunk = rows[i:i + _BATCH]
                objs = []
                for pk, value in chunk:
                    obj = model(id=pk)
                    setattr(obj, field, encrypt_secret(value))
                    objs.append(obj)
                # 只更新敏感列：bulk_update 不走 save()，因此不会被
                # "save 时再加密一次"叠加，也不会顺带改写 updated_at
                model.objects.bulk_update(objs, [field], batch_size=_BATCH)
                done += len(objs)
                self.stdout.write(f"  已加密 {done} 行")

        self.stdout.write(self.style.SUCCESS(
            f"完成：{done} 行敏感字段已补加密"))
