"""全局模型注册表：把多个渠道的模型汇总成一张对外可见的表。

`/v1/*` 用统一 base_url 时不指定渠道，请求里的 model 名需要反查到
「哪个渠道的哪个上游模型」，这个模块就是那张路由表。

规则：
- 对外名 = `alias`（留空则用上游 `model_name`）
- 只有「渠道启用 + 模型启用」的模型才进入注册表
- 重名时 `route_priority` 大的胜出；再相同则默认渠道优先；最后按 id 稳定排序
- `/c/<slug>/v1/*` 仍然精确锁定到某个渠道，不走这张表
"""
from __future__ import annotations

import logging

from django.db.models import Q

from apps.core.models import AIModel, Channel

logger = logging.getLogger("nvidia2api.registry")


def public_name(model: AIModel) -> str:
    return model.public_name


def public_names(model: AIModel) -> list[str]:
    """模型的所有对外名：主对外名（alias > display_name > model_name）+ 附加别名。

    附加别名存于 `aliases`（JSON 数组），一个模型可暴露多个可调用名字，
    与 one-api 的模型映射（一对多）思路一致。
    """
    names: list[str] = []
    for raw in [model.public_name] + (model.aliases or []):
        n = str(raw or "").strip()
        if n and n not in names:
            names.append(n)
    return names


def _rank(model: AIModel) -> tuple:
    """排序键，越小越优先。"""
    return (
        -model.route_priority,
        0 if (model.channel and model.channel.is_default) else 1,
        model.channel_id or 0,
        model.id,
    )


def candidates() -> list[AIModel]:
    """所有启用渠道下的启用模型，已按优先级排好序。"""
    qs = (
        AIModel.objects.filter(enabled=True, channel__isnull=False)
        .filter(Q(channel__enabled=True))
        .select_related("channel")
    )
    return sorted(qs, key=_rank)


def index() -> dict[str, AIModel]:
    """对外名 -> 模型。重名时按优先级取第一个。含全部附加别名。"""
    table: dict[str, AIModel] = {}
    for m in candidates():
        for n in public_names(m):
            table.setdefault(n, m)
    return table


def list_public() -> list[tuple[AIModel, str]]:
    """/v1/models 返回的 (模型, 对外名) 列表：每个对外名一条（已去重，按对外名排序）。"""
    return [(m, n) for n, m in sorted(index().items(), key=lambda kv: kv[0].lower())]


def resolve(name: str) -> AIModel | None:
    """按任意对外名解析模型（主对外名或附加别名；其次上游原始名）。"""
    name = (name or "").strip()
    if not name:
        return None
    table = index()
    if name in table:
        return table[name]
    # 允许客户端直接用上游原始名调用（该名字可能被同名 alias 遮蔽，但显式匹配原始名更直观）
    for m in candidates():
        if m.model_name == name:
            return m
    return None


def resolve_in_channel(name: str, channel: Channel) -> AIModel | None:
    """在指定渠道内解析：任意对外名（含附加别名）/ display_name / 上游原始名命中即可。"""
    name = (name or "").strip()
    if not name:
        return None
    rec = (
        channel.models.filter(enabled=True)
        .filter(Q(alias=name) | Q(display_name=name) | Q(model_name=name))
        .order_by("-route_priority", "id")
        .first()
    )
    if rec:
        return rec
    # 附加别名在 Python 层匹配（JSON list 不便在 SQLite 上做索引/contains）
    for m in channel.models.filter(enabled=True).order_by("-route_priority", "id"):
        for n in (m.aliases or []):
            if str(n).strip() == name:
                return m
    return None


def conflicts() -> dict[str, list[int]]:
    """对外名 -> 参与冲突的模型 id 列表（长度 > 1 才是冲突）。含附加别名。"""
    buckets: dict[str, list[int]] = {}
    for m in candidates():
        for n in public_names(m):
            buckets.setdefault(n, []).append(m.id)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def channels_with_model(name: str) -> list[Channel]:
    """哪些启用渠道提供了这个名字（用于错误提示，帮用户改用 /c/<slug> 前缀）。"""
    name = (name or "").strip()
    found: list[Channel] = []
    for m in candidates():
        if name in public_names(m) or m.model_name == name:
            if m.channel and m.channel not in found:
                found.append(m.channel)
    return found
