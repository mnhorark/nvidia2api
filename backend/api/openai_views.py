"""OpenAI-compatible endpoints: GET /v1/models, POST /v1/chat/completions.

协议转换设计原则（参考 RikkaHub / one-api / new-api）：
1. 入口转换：客户端协议 -> 内部 chat 格式（仅做结构性映射，不丢字段）
2. 出口转换：内部 chat 格式 -> 客户端协议（仅做结构性映射，不丢字段）
3. 竞速/重试/日志/限流等核心链路完全复用内部 chat 格式
4. 同名参数忠实透传；仅对协议结构不同的字段做映射
5. 思考参数归一化：支持任意 agent 框架的写法，按目标 host 分发
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core.models import AIModel, Channel, RequestLog
from services import (
    anthropic_api, api_key_service, channel_service, key_service, model_registry,
    message_fixups, message_shape, responses_api, sysconfig, thinking, tool_alias,
)
from services.load_balancer import build_routes
from services.race_engine import (
    AllRoutesFailed, NoRouteAvailable, UpstreamTruncated, race_chat, race_stream,
)
from services.stream_pipeline import StreamTap
from .auth import openai_error

logger = logging.getLogger("nvidia2api.openai")

_active_lock = threading.Lock()
_active_count = 0


def _try_acquire_request() -> bool:
    global _active_count
    from services import sysconfig
    try:
        limit = int(
            sysconfig.get("max_concurrent_requests") or settings.MAX_CONCURRENT_REQUESTS
        )
    except (TypeError, ValueError):
        limit = settings.MAX_CONCURRENT_REQUESTS
    # 0 = 不限制并发请求数
    if limit is None or limit <= 0:
        return True
    with _active_lock:
        if _active_count >= limit:
            return False
        _active_count += 1
        return True


def active_requests() -> int:
    with _active_lock:
        return _active_count


def _bump_active(delta: int) -> None:
    global _active_count
    with _active_lock:
        _active_count = max(0, _active_count + delta)


_upstream_lock = threading.Lock()
_upstream_active = 0

# max_concurrent_upstream <= 0 视为"不限制"（默认），仅受
# max_concurrent_requests × max_routes_per_request 自然约束。
# 需要在上游总线制上限时（Windows SelectorEventLoop 受限）再调小。
_UNLIMITED = 10**9


def _upstream_limit() -> int:
    global _upstream_active
    try:
        limit = int(
            sysconfig.get("max_concurrent_upstream") or settings.MAX_CONCURRENT_UPSTREAM
        )
    except (TypeError, ValueError):
        limit = settings.MAX_CONCURRENT_UPSTREAM
    if limit is None or limit <= 0:
        return _UNLIMITED
    return limit


def _reserve_upstream(n: int) -> int:
    """预留上游连接额度。

    - n<=0 没有可预留的，直接返回 0。
    - 全局 unlimited 时全部放行，避免大并发下被裁剪饿死。
    - 有限额度且余量不足时：能拿多少拿多少，但**至少保底 1 条**，
      防止请求被裁成 0 线路走 no_available_route 重试风暴（挤兑饿死）。
    """
    global _upstream_active
    n = max(0, int(n or 0))
    if n == 0:
        return 0
    with _upstream_lock:
        limit = _upstream_limit()
        if limit >= _UNLIMITED:
            _upstream_active += n
            return n
        available = max(0, limit - _upstream_active)
        take = min(n, available)
        # 保底 1 条：宁可轻微超额度也不让请求空路由重试
        if take == 0 and available <= 0 and limit > 0:
            take = 1
        _upstream_active += take
        return take


def _release_upstream(n: int) -> None:
    global _upstream_active
    n = max(0, int(n or 0))
    with _upstream_lock:
        _upstream_active = max(0, _upstream_active - n)


def _authenticate(request):
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return api_key_service.authenticate(auth[7:].strip())


def _model_entry(m: AIModel, name: str | None = None) -> dict:
    return {
        "id": name or m.public_name,
        "object": "model",
        "created": int(m.created_at.timestamp()),
        "owned_by": m.channel.slug if m.channel else m.provider,
    }


def list_models(request, channel_slug: str | None = None):
    user_key = _authenticate(request)
    if user_key is None:
        return openai_error("Invalid API key", "invalid_api_key", 401, "authentication_error")
    if not user_key.enabled:
        return openai_error("API key disabled", "key_disabled", 403, "authentication_error")

    if channel_slug:
        channel = channel_service.lookup(channel_slug)
        if channel is None or not channel.enabled:
            return openai_error(f"Unknown channel '{channel_slug}'",
                                "channel_not_found", 404, "invalid_request_error")
        ms = list(channel.models.filter(enabled=True).order_by("model_name"))
        entries: list[tuple[AIModel, str]] = [
            (m, n) for m in ms for n in model_registry.public_names(m)
        ]
    else:
        entries = model_registry.list_public()
    return JsonResponse({"object": "list", "data": [_model_entry(m, n) for m, n in entries]})


class ChannelNotFound(Exception):
    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(f"channel not found: {slug}")


def _resolve_channel(slug: str | None) -> Channel | None:
    if not slug:
        return None
    channel = channel_service.lookup(slug)
    # 禁用即彻底下线：/c/<slug>/ 与 body.channel 两条显式路径都不得再服务。
    # 否则管理员禁用渠道（如 Key 泄露应急）不产生任何效果。
    if channel is None or not channel.enabled:
        raise ChannelNotFound(slug)
    return channel


def _resolve_target(name: str, channel_slug: str | None):
    if channel_slug:
        channel = _resolve_channel(channel_slug)
        model = model_registry.resolve_in_channel(name, channel)
        return model, channel
    model = model_registry.resolve(name)
    return model, (model.channel if model else None)


def _not_found_error(name: str, channel_slug: str | None):
    msg = f"The model '{name}' does not exist"
    if not channel_slug:
        owners = model_registry.channels_with_model(name)
        if owners:
            # 提示模型存在于已禁用渠道，但不回显内部渠道 slug（信息泄露面）
            msg += " (exists on disabled channel(s))"
    return openai_error(msg, "model_not_found", 404, "invalid_request_error")


# 显式不透传给上游的字段（避免 400 或语义错误）
# 单一事实来源：思考族字段清单由 thinking.THINKING_PARAM_KEYS 派生——
# 这批字段的"归一化产物"由 thinking.build_upstream 回填，客户端发来的
# 原始形态（SDK 展开残留如 extra_body / reasoning_effort_value）必须拦下，
# 否则大部分 OpenAI 兼容上游会整包 400。
# 旧实现手工维护了第二份清单且漏掉 extra_body / reasoning_effort_value，
# 已修复为派生。
_DROP_FOR_UPSTREAM = thinking.THINKING_PARAM_KEYS | frozenset({
    "channel",
    # thinking.THINKING_KEY_PATTERNS 里的长尾形态（不在 PARAM_KEYS 里，
    # 但同样不允许原始透传——build_upstream 已按需归一化回填）
    "enabled_thinking",
    "is_thinking",
    "thinking_mode",
    "thinking_type",
})


def _build_upstream_body(body: dict, model_name: str, channel=None,
                         thinking_params: dict | None = None) -> dict:
    """通用参数透传 + 思考强度参数归一化下发。

    无损原则（对标 one-api / new-api / RikkaHub）：
    - 除 _DROP_FOR_UPSTREAM 外**全部忠实透传，含显式 null**——
      null 在 JSON 语义里是"显式未设置"，与缺省不同，剥掉属于改写请求；
    - 不要用白名单过滤未知字段——未来的官方参数会因此被静默丢弃
    - model 始终用真实模型名覆盖

    extra_body 语义：[OI] SDK 的契约是"这些键直接放请求顶层"——整体
    剥掉会连带丢失非思考键（top_k / logit_bias / 供应商私有参数）。
    这里按 SDK 语义平铺：键与顶层同名时客户端显式顶层值优先，思考族
    键仍走归一化通道（平铺先做、归一化后覆盖）。

    `thinking_params` 可传入已计算的归一化结果复用（调用方要为日志再算
    一次时避免二次全量扫描）。
    """
    upstream = {k: v for k, v in body.items() if k not in _DROP_FOR_UPSTREAM}
    # extra_body 平铺（SDK 契约）；思考族键平铺进去后仍会被随后的
    # 归一化产物覆盖，不影响 thinking 通道语义
    extra = body.get("extra_body")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k in upstream or k in ("model",):
                continue  # 顶层显式值 / model 优先，不回退覆盖
            upstream[k] = v
    # 思考参数归一化后下发（按渠道隔离）
    upstream.update(thinking_params if thinking_params is not None
                    else thinking.build_upstream(body, model_name, channel))
    # 上游必须用真实模型名（别名不透传）
    upstream["model"] = model_name
    # tool_choice 对象形态归一化（qwen3.8-flash 400 实证 req_2c34411b 案）：
    # AI SDK 系客户端（zcode 等）发 {"type":"auto"}，部分上游 thinking 模式
    # 只认字符串 "auto"/"none"，对象形态整包 400——且流式下错误体被上游
    # 包装成无信息量的 openai_error（req_2c34411b 四条线路同报此错）。
    # 字符串形态是 OpenAI 规范的权威写法，对象->字符串为无损归一化：
    # 语义完全等价，任何 OpenAI 兼容上游都接受字符串。
    # 带 function 的 {"type":"function",...} 是"强制调用"的唯一表达，
    # 不在此改写——上游 thinking 模式不支持时由上游明确报错表态
    # （对标 muse 的"静默改写不如让上游明确表态"原则）。
    tc = upstream.get("tool_choice")
    if isinstance(tc, dict) and "function" not in tc \
            and tc.get("type") in ("auto", "none", "required"):
        upstream["tool_choice"] = tc["type"]
    # 流式 usage 选项
    if body.get("stream"):
        if "stream_options" not in upstream:
            upstream["stream_options"] = {"include_usage": True}
    return upstream


def _messages_shape(messages) -> list[dict]:
    """messages 形态摘要（诊断）：每条消息的 role/content 类型/工具结构。

    zcode（AI SDK）系客户端的方言（数组 content、null content、空
    arguments、tool 消息附加键）是上游 400 的高频来源；正文不落库，
    只记形态。
    """
    out: list[dict] = []
    if not isinstance(messages, list):
        return out
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            out.append({"i": i, "kind": repr(type(msg).__name__)})
            continue
        content = msg.get("content")
        entry: dict = {
            "i": i,
            "role": msg.get("role"),
            "content_type": type(content).__name__,
            "content_len": len(content) if isinstance(content, str) else None,
        }
        if isinstance(content, list):
            entry["content_blocks"] = [p.get("type") if isinstance(p, dict)
                                       else type(p).__name__ for p in content[:8]]
        tcs = msg.get("tool_calls")
        if isinstance(tcs, list):
            entry["tool_calls"] = [
                {"name": (tc.get("function") or {}).get("name"),
                 "args_len": len(str((tc.get("function") or {}).get("arguments") or "")),
                 "args_head": str((tc.get("function") or {}).get("arguments") or "")[:48]}
                for tc in tcs if isinstance(tc, dict)][:16]
        if msg.get("tool_call_id"):
            entry["tool_call_id"] = msg["tool_call_id"]
        extra = set(msg.keys()) - {"role", "content", "tool_calls",
                                   "tool_call_id", "name"}
        if extra:
            entry["extra_keys"] = sorted(extra)
        out.append(entry)
    return out


def _request_summary(body: dict, tool_alias_map: dict | None) -> dict:
    """构建请求体摘要（诊断用，不含 messages 正文——防膨胀且防泄露）。

    zcode 等 agent 的上游 400 排查依赖它回答"请求的哪个部件引发拒绝"：
    - 顶层参数清单（messages/stream_options 剔除）
    - tools 规模 + 工具名（超长名被 tool_alias 改写的在此可见）
    - tool_choice / response_format 等结构性字段原样
    """
    tools = body.get("tools")
    tool_names: list[str] = []
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function") or {}
                tool_names.append(str(fn.get("name") or t.get("name") or ""))
    summary: dict = {
        "top_keys": sorted(k for k in body.keys() if k != "messages"),
        "messages_count": len(body.get("messages") or [])
        if isinstance(body.get("messages"), list) else 0,
        "tools_count": len(tools) if isinstance(tools, list) else 0,
        "tool_names": tool_names[:64],
        "tool_names_truncated": len(tool_names) > 64,
        # 值敏感字段单独记值：max_tokens 超上游窗口是 zcode 类客户端
        # 400/context-window 报错的高频根因（值必须可见，键名不够）
        "max_tokens": body.get("max_tokens"),
        "max_completion_tokens": body.get("max_completion_tokens"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        # tool_choice 记原始值：对象/字符串形态之争是 thinking 模型上游
        # 400 的高频根因（req_2c34411b 案），只记键名无法区分形态
        "tool_choice": body.get("tool_choice"),
        # 每条消息的形态摘要（不含正文）：role / content 类型 /
        # tool_calls 数 / tool_call_id——AI SDK 系客户端的数组 content、
        # null content、空 arguments 等方言在此一览无余
        "messages_shape": _messages_shape(body.get("messages")),
    }
    if tool_alias_map:
        summary["tool_alias_rewritten"] = len(tool_alias_map)
        summary["tool_alias_map"] = {
            alias: orig for alias, orig in list(tool_alias_map.items())[:64]}
    return summary


def _authorize(request):
    """数据面入口鉴权：Bearer Key 有效性 → enabled → RPM → 额度闸门（只读）。

    ⚠ 本函数**只过闸门、不预占**。真正的原子预占在 `_reserve_quota`，
    由生成类视图在 `_parse_body` 之后、进入 `_run_authed` 之前调用。

    为什么拆两段（2026-09-07，A3）：旧实现在这里就 `claim_quota` 预占 1 token，
    而视图层有 7 个出口在 `_authorize` 成功之后直接 `return err`
    （`_parse_body` 的 400/413、`input is required`、`messages is required`、
    `model and messages are required`、`channel_not_found`、模型不存在），
    **一个都不退还**，且全部位于 `RequestLog.objects.create` 之前——
    畸形 body / 不存在模型每请求永久吞掉 1 额度且零留痕。
    那正是 09-05 修掉的 `count_tokens` 吞额度缺陷换了个形态复现。
    把预占挪到"所有校验都过了、马上要生成"的位置，泄漏面从 7 个分散出口
    收敛为 0：早退发生在预占之前，天然无事可退。

    闸门本身仍然在这里过：额度已耗尽的 Key 不该因为"请求体还没解析"就绕过
    计费边界。只读判定不会超扣，并发超扣由后面的原子 claim 兜住。
    """
    user_key = _authenticate(request)
    if user_key is None:
        return None, openai_error("Invalid API key", "invalid_api_key", 401, "authentication_error")
    if not user_key.enabled:
        return None, openai_error("API key disabled", "key_disabled", 403, "authentication_error")
    ok, reason = api_key_service.check_and_count(user_key)
    if not ok:
        if reason == "rate_limited":
            return None, openai_error("Rate limit exceeded", "rate_limit_exceeded", 429)
        return None, openai_error("API key disabled", "key_disabled", 403, "authentication_error")
    ok, reason = api_key_service.check_quota(user_key)
    if not ok:
        return None, openai_error("Insufficient quota (quota exceeded)",
                                  "insufficient_quota", 402, "insufficient_quota")
    return user_key, None


def _reserve_quota(user_key):
    """原子预占 1 token 额度。返回 `(ok, err_response)`。

    必须在视图里所有可早退的校验之后调用，紧邻 `_run_authed`：
    预占一旦成功，责任就交给 `_run_authed` 的结算/退还路径。
    """
    ok, reason = api_key_service.claim_quota(user_key)
    if ok:
        return True, None
    return False, openai_error("Insufficient quota (quota exceeded)",
                               "insufficient_quota", 402, "insufficient_quota")


def _refund_reservation(user_key):
    """退还 `_reserve_quota` 的预占。用于 `_run_authed` 里预占之后、
    尚未进入结算路径就早退的分支（模型不存在 / 参数缺失）。"""
    try:
        api_key_service.record_usage(user_key, reservation=1)
    except Exception:  # noqa: BLE001
        logger.exception("refund quota reservation failed")


MAX_BODY_BYTES = 32 * 1024 * 1024


def _parse_body(request):
    from services import sysconfig as _sc
    try:
        limit = int(_sc.get("max_request_bytes") or MAX_BODY_BYTES)
    except (TypeError, ValueError):
        limit = MAX_BODY_BYTES
    limit = max(0, limit)
    if limit <= 0:
        limit = MAX_BODY_BYTES
    declared = request.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None, openai_error("Request body too large",
                                          "payload_too_large", 413)
        except (TypeError, ValueError):
            pass
    try:
        raw = request.body or b""
    except RequestDataTooBig:
        return None, openai_error("Request body too large", "payload_too_large", 413)
    if len(raw) > limit:
        return None, openai_error("Request body too large", "payload_too_large", 413)
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, openai_error("Invalid JSON body", "invalid_request",
                                  400, "invalid_request_error")
    if not isinstance(body, dict):
        return None, openai_error(
            "Request body must be a JSON object", "invalid_request",
            400, "invalid_request_error")
    return body, None


def _run_authed(user_key, body, channel_slug, protocol, echo_body=None):
    if not _try_acquire_request():
        # 预占已在视图里发生（_reserve_quota），而这个出口在 try 之外、
        # 走不到 finally 的兜底 —— 不在此退还，拥塞期每一个 429 都会永久吞
        # 1 token 额度且不落 RequestLog（并发越接近 max_concurrent_requests
        # 漏得越快，恰是最不该计费出错的时候）。
        _refund_reservation(user_key)
        return openai_error("Server busy, too many concurrent requests",
                            "server_overloaded", 429)
    log = None
    semaphore_released_by_stream = False
    upstream_reserved = 0
    # B5：`crashed` 只在异常逃逸时置位，用于区分"正常 return（终态路径已结算）"
    # 与"异常穿透"。`request_id` 与 `started` 都在 try 内部才生成，而 finally 里的
    # 兜底要无条件读它们 —— 少预声明任何一个，兜底自己就会先抛 UnboundLocalError，
    # 既掩盖原始异常、又让退款/结算完全不执行（正是它声称要防的场景）。
    crashed = False
    # 兜底路径的耗时从"进入生成流程"起算；建日志后会被重新绑定为原语义
    started = time.monotonic()
    request_id = ""
    try:
        requested_name = body.get("model", "")
        messages = body.get("messages")
        if not requested_name or not isinstance(messages, list) or not messages:
            # 预占已在视图里发生（_reserve_quota），此处早退必须退还
            _refund_reservation(user_key)
            return openai_error("model and messages are required", "invalid_request",
                                400, "invalid_request_error")

        slug = channel_slug or (str(body.get("channel") or "").strip() or None)
        try:
            model, channel = _resolve_target(requested_name, slug)
        except ChannelNotFound as exc:
            _refund_reservation(user_key)
            return openai_error(f"Unknown channel '{exc.slug}'",
                                "channel_not_found", 404, "invalid_request_error")
        if model is None:
            _refund_reservation(user_key)
            return _not_found_error(requested_name, slug)

        model_name = model.model_name
        stream = bool(body.get("stream"))
        # 思考参数归一化只算一次：同一份结果既合入上游请求体、又记审计日志
        upstream_thinking = thinking.build_upstream(body, model_name, channel)
        upstream_body = _build_upstream_body(body, model_name, channel,
                                             thinking_params=upstream_thinking)
        # 消息形态钳制（AI SDK 系客户端方言）：tool/assistant 消息的
        # content 数组 -> 字符串、tool 消息剔除规范外附加键——多数上游
        # 对 tool.content 强校验 string，数组形态整包 400（字节零丢失，
        # 纯形态转换）
        message_shape.clamp_message_shapes(upstream_body)
        # 跨轮重复 tool_call id 唯一化（zen/Anthropic 系强校验
        # "每个 function_call 恰好一个 output"，跨轮同名 id 整包 400）
        message_fixups.dedupe_tool_call_ids(upstream_body)
        # 超长工具名（上游 >64 字符会 400）替换为确定性短别名，
        # 映射表在响应返回客户端前还原（工具调用对客户端无感）
        tool_alias_map = tool_alias.shorten_function_names(upstream_body)
        try:
            _flat_for_log = thinking._flatten(body)
        except Exception:
            _flat_for_log = dict(body)
        client_thinking = {
            k: _flat_for_log.get(k) for k in thinking.THINKING_PARAM_KEYS
            if k in _flat_for_log and _flat_for_log.get(k) is not None
        }

        request_id = key_service.new_request_id()
        routes = build_routes(channel, proxy_group=model.proxy_group_id,
                              endpoint=model.endpoint)
        if not stream:
            upstream_reserved = _reserve_upstream(len(routes))
            if upstream_reserved < len(routes):
                # 被全局并发闸门截断的线路：其 Key 已在 build_routes 内领取
                # RPM 名额，整条丢弃前必须退回，否则拥塞期白烧配额。
                for dropped in routes[upstream_reserved:]:
                    if dropped.claimed and getattr(dropped.key, "id", None):
                        key_service.release_rpm_slot(dropped.key.id)
                routes = routes[:upstream_reserved]
        log = RequestLog.objects.create(
            channel=channel, request_id=request_id, user_api_key=user_key,
            model=requested_name, routes_count=len(routes), is_stream=stream,
            client_thinking=client_thinking, upstream_thinking=upstream_thinking,
            request_summary=_request_summary(body, tool_alias_map),
        )
        started = time.monotonic()

        if not routes:
            _finish_log(log, started, False, 503, "no_available_route")
            api_key_service.record_result(user_key, False)
            # claim_quota 预占的 1 token 在失败路径退还
            api_key_service.record_usage(user_key, reservation=1)
            return openai_error("当前没有可用线路（该渠道没有可用的 Key）",
                                "no_available_route", 503)

        retries = max(0, min(int(sysconfig.get("retry_count", channel) or 0), 5))
        max_attempts = 1 + retries

        if stream:
            log_id_holder = {"log": log, "started": started}
            gen = _stream_response(routes, upstream_body, log_id_holder,
                                   user_key, channel, max_attempts,
                                   proxy_group=model.proxy_group_id,
                                   endpoint=model.endpoint,
                                   tool_map=tool_alias_map or None)
            if protocol == "responses":
                gen = responses_api.iter_chat_sse_as_responses(gen)
            elif protocol == "anthropic":
                gen = anthropic_api.iter_chat_sse_as_anthropic(gen)
            response = StreamingHttpResponse(gen, content_type="text/event-stream")
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            # B9：交接标志必须**在响应对象构造成功之后**才置位。
            # 旧写法在创建生成器之前就置 True，于是若 `StreamingHttpResponse(...)`
            # 或上面任何一行抛错，外层 finally 会因为该标志为 True 而跳过
            # `_bump_active(-1)` —— 而生成器从未被迭代，它自己的 finally 也不会跑，
            # `_active_count` 就永久 +1（累积到 max_concurrent_requests 后整站 429
            # 且不可自愈，因为僵尸名额自己不会退出）。
            semaphore_released_by_stream = True
            return response

        result = None
        last_exc: Exception | None = None
        # B2：重试必须排除上一轮已判定死亡的 Key+代理组合。
        # 流式路径一直这么做（见下方 _stream_response 的 excluded /
        # excluded_proxies），非流式路径此前**没传**这两个参数——于是
        # `retry_count>0` 时第二轮 build_routes 很可能又抽回同一条死线路，
        # 重试等于白跑一轮，还多烧一次 RPM。
        # load_balancer.build_routes 的 docstring 明说这两个参数就是为此存在的。
        excluded: set[tuple[int, int | None]] = set()
        excluded_proxies: set[int] = set()
        for attempt in range(max_attempts):
            attempt_routes = routes if attempt == 0 else build_routes(
                channel, proxy_group=model.proxy_group_id, endpoint=model.endpoint,
                exclude=excluded or None, exclude_proxies=excluded_proxies or None)
            if not attempt_routes:
                last_exc = NoRouteAvailable()
                continue
            if attempt > 0:
                # 重试换线同样要过上游并发闸门（旧实现只在首次预留，
                # 重试时按陈旧额度跑满新线路，闸门被绕过）。
                _release_upstream(upstream_reserved)
                upstream_reserved = _reserve_upstream(len(attempt_routes))
                if upstream_reserved < len(attempt_routes):
                    # 被裁线路的 Key 已在 build_routes 内领取 RPM 名额，
                    # 整条丢弃前必须退回。
                    for dropped in attempt_routes[upstream_reserved:]:
                        if dropped.claimed and getattr(dropped.key, "id", None):
                            key_service.release_rpm_slot(dropped.key.id)
                    attempt_routes = attempt_routes[:upstream_reserved]
                if not attempt_routes:
                    last_exc = NoRouteAvailable()
                    continue
            try:
                result = race_chat(attempt_routes, upstream_body)
                break
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                # 把本轮判定死亡的 Key+代理组合记入排除集，下一轮换线时
                # 不会再抽到它们（与流式路径同一套口径）
                comb_by_name = {
                    r.name: (getattr(r.key, "id", None),
                             getattr(r.proxy, "id", None)
                             if r.proxy is not None else None)
                    for r in attempt_routes
                }
                for item in (getattr(exc, "report", None) or []):
                    if item.get("status") != "failed":
                        continue
                    comb = comb_by_name.get(item.get("name"))
                    if comb is None:
                        continue
                    excluded.add(comb)
                    if comb[1] is not None:
                        excluded_proxies.add(comb[1])
                last_exc = exc
                # 内容被拒（探针确认）是确定性失败：同样的内容重试必然
                # 同样 400，换线无意义——直接终止重试（对标"模型不存在
                # 不应重复竞速"，AGENTS.md §四十五）
                if getattr(exc, "content_rejected", False):
                    break
                if attempt + 1 < max_attempts:
                    logger.info("request %s attempt %d failed, retrying: %s",
                                request_id, attempt + 1, exc)
        if result is None:
            if isinstance(last_exc, NoRouteAvailable):
                _finish_log(log, started, False, 503, "no_available_route")
                api_key_service.record_result(user_key, False)
                api_key_service.record_usage(user_key, reservation=1)  # 退还预占
                return openai_error("当前没有可用线路", "no_available_route", 503)
            report = getattr(last_exc, "report", None) or []
            logger.warning("all routes failed after %d attempt(s): %s",
                           max_attempts, last_exc)
            content_rejected = bool(getattr(last_exc, "content_rejected", False))
            _finish_log(log, started, False, 502,
                        "upstream_content_rejected" if content_rejected
                        else "all_routes_failed", routes=report)
            api_key_service.record_result(user_key, False)
            api_key_service.record_usage(user_key, reservation=1)  # 退还预占
            if content_rejected:
                # req_2c34411b / req_32382 案：上游内容审核拒收请求体时
                # 返回无信息量的通用 400，四线路全灭曾被误报为"上游暂时
                # 不可用"，误导排查数小时。探针确认后如实分类。
                return openai_error(
                    "上游拒绝了请求内容：所有线路均返回 400，但同一通道的"
                    "最小无害探针可通过——判定为请求内容命中上游内容策略/"
                    "参数校验（并非上游故障，重试无意义）。请检查 system/"
                    "消息内容，或更换模型/通道",
                    "upstream_content_rejected", 502)
            return openai_error("上游服务暂时不可用，请稍后重试", "upstream_error", 502)

        r = result.route
        usage = (result.payload or {}).get("usage") or {}
        # 上游缺省/省略 usage 时落本地估算：否则该请求对额度完全免费
        # （流式路径本就有 tokenizer 兜底，两条路径口径必须一致）。
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        if not prompt_tokens or not completion_tokens:
            from services import tokenizer as _tokenizer
            if not prompt_tokens:
                prompt_tokens = _tokenizer.estimate_messages_tokens(
                    upstream_body.get("messages"))
            if not completion_tokens:
                _text_parts: list[str] = []
                for _ch in ((result.payload or {}).get("choices") or []):
                    _msg = (_ch or {}).get("message") or {}
                    if isinstance(_msg.get("content"), str):
                        _text_parts.append(_msg["content"])
                completion_tokens = _tokenizer.estimate_tokens("".join(_text_parts))
        usage = dict(usage, prompt_tokens=prompt_tokens,
                     completion_tokens=completion_tokens)
        _finish_log(log, started, True, result.http_status, "", route_kind=r.kind,
                    key_name=r.key.name, proxy_name=r.proxy.name if r.proxy else "",
                    proxy_ip=(r.proxy.public_ip if r.proxy else ""),
                    usage=usage, routes=result.report or [])
        api_key_service.record_result(user_key, True)
        api_key_service.record_usage(
            user_key,
            prompt_tokens,
            completion_tokens,
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
            reservation=1,  # 扣除 claim_quota 预占的 1 token
        )
        payload = result.payload
        # 还原超长工具名别名（入境时被缩短的名字在出口处恢复为原始长名）
        if tool_alias_map:
            tool_alias.restore_payload(payload, tool_alias_map)
        if protocol == "responses":
            payload = responses_api.chat_to_responses_payload(payload, echo_body or body)
        elif protocol == "anthropic":
            payload = anthropic_api.chat_to_messages_payload(payload)
        return JsonResponse(payload, status=200)
    except BaseException:
        # B5：区分"正常 return（各终态路径已自行结算）"与"异常逃逸"。
        # 只有后者需要兜底，绝不能在正常路径上重复退款/重复结算。
        crashed = True
        raise
    finally:
        if not semaphore_released_by_stream:
            _bump_active(-1)
            _release_upstream(upstream_reserved)
        if crashed:
            _force_settle_non_stream(user_key, log, started, request_id)


def _force_settle_non_stream(user_key, log, started: float,
                             request_id: str = "") -> None:
    """B5：非流式路径的兜底结算。

    流式的 `finally` 会强制 settle（未结算的置 failed 并退款），非流式此前
    **只释放信号量与上游额度**——于是 `RequestLog.objects.create` 之后、
    `_finish_log` 之前抛出的任何未捕获异常，都会同时留下：

    1. 一行永久 `status="pending"` 的 RequestLog。实测库里 630 条（最早 9 天前），
       而成功率与平均延迟的分母都含 pending → 统计被系统性拉低；
    2. 一份悬空的 `claim_quota` 预占（`used_quota` 永久 +1）。

    触发面很宽：`build_routes` 撞 SQLite 写锁、`sysconfig.get` 对未知键抛
    KeyError、协议转换抛 AttributeError，以及最要紧的一条——一旦有人把数据面
    视图改成 async，`race_chat` 里的 `asyncio.run` 必抛 RuntimeError，
    每个非流式请求都会走到这里。

    判据用 `log.status == "pending"`：所有正常终态都是先 `_finish_log`
    （置 success/failed）再退款，所以"仍是 pending"等价于"没人结算过"。
    """
    try:
        if log is None:
            # 日志还没建就炸了：只退额度，没有行可结算
            api_key_service.record_usage(user_key, reservation=1)
            return
        if log.status != "pending":
            return          # 已有终态路径处理过，绝不重复退
        _finish_log(log, started, False, 500, "unhandled_error")
        api_key_service.record_result(user_key, False)
        api_key_service.record_usage(user_key, reservation=1)
        logger.error(
            "request %s 未捕获异常逃逸出非流式路径，已兜底结算为 failed 并退还额度",
            log.request_id or request_id)
    except Exception:  # noqa: BLE001
        # 兜底路径自己再抛会掩盖原始异常，这里只留痕
        logger.exception("非流式兜底结算自身失败 (req %s)", request_id)


@csrf_exempt
def chat_completions(request, channel_slug: str | None = None):
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)
    user_key, err = _authorize(request)
    if err:
        return err
    body, err = _parse_body(request)
    if err:
        return err
    # 预占放在**所有可早退的校验之后**：见 _authorize 的 A3 说明
    ok, err = _reserve_quota(user_key)
    if err:
        return err
    return _run_authed(user_key, body, channel_slug, "chat")


@csrf_exempt
def responses(request, channel_slug: str | None = None):
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)
    user_key, err = _authorize(request)
    if err:
        return err
    body, err = _parse_body(request)
    if err:
        return err
    chat_body = responses_api.responses_to_chat_body(body)
    if not isinstance(chat_body.get("messages"), list) or not chat_body.get("messages"):
        return openai_error("input is required", "invalid_request", 400, "invalid_request_error")
    ok, err = _reserve_quota(user_key)
    if err:
        return err
    return _run_authed(user_key, chat_body, channel_slug, "responses", echo_body=body)


@csrf_exempt
def anthropic_messages(request, channel_slug: str | None = None):
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)
    user_key, err = _authorize(request)
    if err:
        return err
    body, err = _parse_body(request)
    if err:
        return err
    chat_body = anthropic_api.messages_to_chat_body(body)
    if not isinstance(chat_body.get("messages"), list) or not chat_body.get("messages"):
        return openai_error("messages is required", "invalid_request", 400, "invalid_request_error")
    ok, err = _reserve_quota(user_key)
    if err:
        return err
    return _run_authed(user_key, chat_body, channel_slug, "anthropic", echo_body=body)


@csrf_exempt
def anthropic_count_tokens(request, channel_slug: str | None = None):
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)
    # 本端点是**纯本地计算**（不产生上游消耗）：过 `_authorize` 的只读额度闸门，
    # 但**不调 `_reserve_quota`**。历史上它走默认分支 claim 了 1 token 却从不结算，
    # Claude Code 类客户端每轮上下文计数都永久吞掉 1 额度且不落 RequestLog。
    user_key, err = _authorize(request)
    if err:
        return err
    body, err = _parse_body(request)
    if err:
        return err
    return JsonResponse({"input_tokens": anthropic_api.count_tokens(body)})


# 转发路径的 chunk 观察统一走 StreamTap（单次解析、零改写）：
# 旧实现在这里有两份逐行差两行的 JSON 探测器（_chunk_has_content /
# _chunk_has_any_signal），加记账循环每 chunk 最多 3 次 json.loads，
# 且"已交付内容"的语义在两处口径不一。见 services/stream_pipeline.py。


async def _stream_response(routes, upstream_body, holder, user_key, channel,
                           max_attempts: int = 1, proxy_group: int | None = None,
                           endpoint: str | None = None,
                           tool_map: dict | None = None):
    import asyncio

    def _restore(chunk: str) -> str:
        return tool_alias.restore_stream_chunk(chunk, tool_map) if tool_map else chunk

    winner = None
    last_exc: Exception | None = None
    idle_timeout = float(sysconfig.get("stream_idle_timeout", channel) or 0)
    heartbeat = float(sysconfig.get("stream_heartbeat_interval", channel) or 0)
    max_duration = float(sysconfig.get("stream_max_duration", channel) or 0)
    backoff = float(sysconfig.get("retry_backoff_seconds", channel) or 0)
    content_idle_timeout = float(
        sysconfig.get("stream_content_idle_timeout", channel) or 0)
    excluded: set[tuple[int, int | None]] = set()
    excluded_proxies: set[int] = set()
    all_reports: list[dict] = []

    settled = {"done": False}
    reserved = 0

    from services.loop_offload import run_db

    # 竞速/重试静默期的心跳间隔：winner 诞生之前（race_stream 首帧等待 +
    # backoff sleep + 换线重建），客户端可能连续数分钟收不到任何字节，
    # agent 客户端（zcode 等）的 modelStream idleTimeout 会 abort 连接
    # （UND_ERR_SOCKET terminated）→ 生成器被 cancel → 记账收尾丢失。
    # SSE 注释行对 OpenAI/Anthropic/Responses 客户端均不可见，纯保活。
    race_heartbeat = heartbeat if heartbeat and heartbeat > 0 else 15.0

    async def settle(success: bool) -> None:
        if settled["done"]:
            return
        settled["done"] = True
        rec = holder["log"]
        await run_db(api_key_service.record_result, user_key, success)
        await run_db(
            api_key_service.record_usage,
            user_key, rec.prompt_tokens or 0,
            rec.completion_tokens or 0, rec.cached_tokens or 0,
            1)  # reservation：扣除入口 claim_quota 预占的 1 token

    async def _safe_finish(*args, **kwargs):
        try:
            await run_db(_finish_log, *args, **kwargs)
        except Exception:
            logger.exception("stream finish log save failed")

    async def _backoff_with_heartbeat(seconds: float):
        """退避等待，每 race_heartbeat 秒产一条心跳（async gen，调用方转发）。"""
        end = time.monotonic() + seconds
        while True:
            remain = end - time.monotonic()
            if remain <= 0:
                return
            await asyncio.sleep(min(remain, race_heartbeat))
            yield ": keep-alive\n\n"

    try:
        for attempt in range(max_attempts):
            rs = routes if attempt == 0 else await run_db(
                build_routes, channel, proxy_group=proxy_group, endpoint=endpoint,
                exclude=excluded or None, exclude_proxies=excluded_proxies or None)
            if not rs:
                last_exc = NoRouteAvailable()
                if attempt + 1 < max_attempts and backoff > 0:
                    async for _hb in _backoff_with_heartbeat(backoff):
                        yield _hb
                continue
            reserved = _reserve_upstream(len(rs))
            if reserved < len(rs):
                # 被上游并发闸门截断的线路：退回它们已 claim 的 RPM 名额，
                # 避免全局拥塞期按比率虚耗各 Key 的分钟配额。
                for dropped in rs[reserved:]:
                    if dropped.claimed and getattr(dropped.key, "id", None):
                        await run_db(key_service.release_rpm_slot, dropped.key.id)
                rs = rs[:reserved]
            if not rs:
                last_exc = NoRouteAvailable()
                _release_upstream(reserved)
                reserved = 0
                if attempt + 1 < max_attempts and backoff > 0:
                    async for _hb in _backoff_with_heartbeat(backoff):
                        yield _hb
                continue
            w = None
            tap = StreamTap()  # 整条尝试共用一个观察器（race 失败时为空态）
            try:
                # 竞速等待包成心跳循环：race_stream 期间（首帧等待可长达
                # stream_first_byte_timeout）客户端零字节，agent 客户端的
                # idleTimeout 会掐连接。每 race_heartbeat 秒注入一条
                # SSE 注释行（对所有协议客户端不可见，纯保活）。
                race_task = asyncio.ensure_future(race_stream(rs, upstream_body))
                try:
                    while True:
                        done, _pending = await asyncio.wait(
                            {race_task}, timeout=race_heartbeat)
                        if done:
                            w = race_task.result()
                            break
                        yield ": keep-alive\n\n"
                finally:
                    if not race_task.done():
                        # 客户端在心跳 yield 点断开（GeneratorExit）：
                        # 撕掉竞速 task，race_stream_winner 的 finally
                        # 会级联清理所有线路连接（防 fd 泄漏）
                        race_task.cancel()
                        await asyncio.gather(race_task, return_exceptions=True)
                if reserved > 1:
                    _release_upstream(reserved - 1)
                    reserved = 1
                winner = w
                log = holder["log"]
                log.winner_route_type = w.route.kind
                log.winner_key_name = w.route.key.name
                log.winner_proxy_name = w.route.proxy.name if w.route.proxy else ""
                log.proxy_public_ip = w.route.proxy.public_ip if w.route.proxy else ""
                # 关键：流式请求在 winner 出现时**不**标记 success，仅记录首字耗时与线路；
                # status 保持 pending，直到 _drain 完整结束再置为 success。
                # 否则若客户端在 drain 结束前断开（0ms 现象），success 记录会永久残留 0ms/0 token。
                log.first_token_ms = round(
                    (time.monotonic() - holder["started"]) * 1000, 1)
                log.duration_ms = log.first_token_ms
                log.http_status = 200
                all_reports.extend(w.report or [])
                log.routes = all_reports
                # 预填 prompt token，避免前端在 pending 阶段看到 0 token（NV 渠道常无 usage）
                try:
                    from services import tokenizer as _tok
                    log.prompt_tokens = _tok.estimate_messages_tokens(upstream_body.get("messages"))
                    log.total_tokens = log.prompt_tokens
                except Exception:
                    pass
                await run_db(log.save)
                # 单点观察：_drain 内每 chunk 只解析一次，这里只读累积态
                truncated = False
                try:
                    async for chunk in _drain(w, idle_timeout, heartbeat,
                                      max_duration, content_idle_timeout,
                                      tap=tap):
                        yield _restore(chunk)
                    # 静默截断检测：流结束了，但既没有 finish_reason 也没有上游
                    # [DONE] —— 上游把流掐了（长连接被网关/代理切断的典型形态）。
                    # 绝不能伪装成成功：未发内容走换线重试，已发内容显式报错。
                    # saw_done 双源取或：final_state（iter_sse 的行级观察，要求
                    # "data: [DONE]" 带空格）与 tap.saw_done（帧级观察，无空格
                    # 变体也认）任一为真即视为上游完整收尾——方言变体不应触发
                    # 假截断报错；final_state 缺失（测试 monkeypatch lines()）
                    # 时退回 tap 观察。
                    upstream_done = bool(
                        (getattr(w, "final_state", None) or {}).get("saw_done")
                        or tap.saw_done)
                    if not (tap.finish_reason or upstream_done):
                        truncated = True
                        # 闸门包含思考增量：已经吐过 reasoning 的流**不能**再透明
                        # 换线重跑——那些字节早已逐块 yield 给客户端（kimi-k3 实测
                        # 丢过 300 秒思考流并重复交付一遍）。
                        if not tap.delivered_to_client:
                            raise UpstreamTruncated(
                                "upstream closed stream without finish_reason/[DONE]")
                finally:
                    usage = dict(tap.usage)
                    completion_text = tap.completion_parts
                    # 观测：区分"上游静默 300s"与"思考流了 300s 被掐"——这两种
                    # 情况的正确处置相反（前者该换线、后者绝不能换线），而此前
                    # 日志里两者长得一模一样（completion_tokens 都是 0）。
                    log.stream_chunks = tap.chunk_count
                    log.content_chars = sum(len(p) for p in completion_text)
                    log.reasoning_chars = tap.reasoning_chars
                    if truncated:
                        log.status = "failed"
                        log.error_type = "upstream_truncated"
                    elif tap.saw_error:
                        # 上游流内 error 帧（流已建立、上游在数据流里报错，
                        # 如 muse 超上下文窗口）：字节已如实透传给客户端，
                        # 但记账必须如实——status=failed + 上游错误详情入
                        # routes 明细，渠道健康同步入账（下方 record）。
                        log.status = "failed"
                        log.error_type = "upstream_stream_error"
                        log.http_status = 200
                        err = dict(tap.error_detail or {})
                        log.routes = (log.routes or []) + [{
                            "name": getattr(w.route, "name", ""),
                            "kind": getattr(w.route, "kind", ""),
                            "key_name": getattr(w.route.key, "name", ""),
                            "status": "failed",
                            "error": "stream_error: " + json.dumps(
                                err, ensure_ascii=False)[:512],
                            "http_status": 200,
                        }]
                    else:
                        log.status = "success"
                    log.duration_ms = round((time.monotonic() - holder["started"]) * 1000, 1)
                    if usage.get("prompt_tokens"):
                        log.prompt_tokens = usage["prompt_tokens"]
                    elif log.prompt_tokens:
                        pass
                    else:
                        from services import tokenizer
                        log.prompt_tokens = tokenizer.estimate_messages_tokens(
                            upstream_body.get("messages"))
                    if usage.get("completion_tokens"):
                        log.completion_tokens = usage["completion_tokens"]
                    elif "".join(completion_text).strip():
                        from services import tokenizer
                        log.completion_tokens = tokenizer.estimate_tokens(
                            "".join(completion_text))
                    log.total_tokens = (log.prompt_tokens or 0) + (log.completion_tokens or 0)
                    log.cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                    await run_db(log.save)
                if truncated:
                    # 已交付部分内容，无法透明重试：向客户端显式报错（zcode 等
                    # agent 由此得知回答不完整，可整体重试），并把线路记一次
                    # 失败让调度器学会避开这种掐长连接的代理/Key。
                    try:
                        from services.proxy_service import report_proxy_result
                        if w.route.proxy is not None:
                            await run_db(report_proxy_result, w.route.proxy.id, False)
                        key_id = getattr(w.route.key, "id", None)
                        if key_id is not None:
                            await run_db(key_service.report_failure,
                                         key_id, "stream_truncated", 0)
                    except Exception:
                        pass
                    yield "data: " + json.dumps({
                        "error": {"message": "上游输出中断：流被提前关闭（未收到 "
                                  "finish_reason/[DONE]），已收到的内容不完整，请重试",
                                  "type": "api_error", "param": None,
                                  "code": "upstream_truncated"}
                    }) + "\n\n"
                    if not tap.saw_done:
                        yield "data: [DONE]\n\n"
                    await settle(False)
                    return
                if not tap.saw_done:
                    # 上游给出了 finish_reason 但漏发 [DONE]：内容已完整，
                    # 补一个干净的收尾帧保证协议闭合
                    yield "data: [DONE]\n\n"
                if tap.saw_error:
                    # 上游流内报错：错误帧已零丢失透传给客户端（其报错
                    # 文案就是上游原文），记账按失败走——settle(False) +
                    # 渠道健康入账失败。上游语义性错误（如上下文窗口超限）
                    # 不触发 Key/代理失败计数——那是模型/请求本身的问题，
                    # 不是线路质量问题。
                    await settle(False)
                    from services import channel_health as _ch
                    await run_db(_ch.record, log.channel, False, 200,
                                 "upstream_stream_error")
                    return
                await settle(True)
                from services import channel_health
                await run_db(channel_health.record, log.channel, True, 200)
                return
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                if isinstance(exc, AllRoutesFailed):
                    comb_by_name = {
                        r.name: (getattr(r.key, "id", None),
                                 getattr(r.proxy, "id", None)
                                 if r.proxy is not None else None)
                        for r in rs
                    }
                    for item in (exc.report or []):
                        if item.get("status") != "failed":
                            continue
                        comb = comb_by_name.get(item.get("name"))
                        if comb is None:
                            continue
                        excluded.add(comb)
                        if comb[1] is not None:
                            excluded_proxies.add(comb[1])
                last_exc = exc
                # 内容被拒（探针确认）是确定性失败：重试必然复现，直接终止
                if getattr(exc, "content_rejected", False):
                    break
                logger.info("stream attempt %d failed, retrying: %s",
                            attempt + 1, exc)
                if attempt + 1 < max_attempts and backoff > 0:
                    async for _hb in _backoff_with_heartbeat(backoff):
                        yield _hb
            except Exception as exc:
                if tap.delivered_to_client or tap.saw_done:
                    # 线路中途死亡（含已出部分内容后断流）：竞速时该 Key 已被
                    # _mark_success 记为成功，但中途死亡是真实故障。补记一次失败
                    # （不标 invalid，仅累计 failure_count + 冷却），否则"先吐
                    # role chunk 然后死亡"的慢性坏 Key 永远不会被调度打分发现。
                    try:
                        if w is not None and w.route is not None:
                            from services.proxy_service import report_proxy_result
                            if w.route.proxy is not None:
                                await run_db(report_proxy_result, w.route.proxy.id, False)
                            key_id = getattr(w.route.key, "id", None)
                            if key_id is not None:
                                await run_db(key_service.report_failure,
                                             key_id, "stream_died", 0)
                    except Exception:
                        pass
                    logger.warning("stream truncated after content (req %s): %s",
                                   log.request_id, exc)
                    log.error_type = "stream_truncated"
                    log.status = "failed"
                    await run_db(log.save)
                    # 中途异常断流同样不能只发干净 [DONE]：客户端（agent）必须
                    # 知道回答不完整，否则把半截文档当成功收货
                    yield "data: " + json.dumps({
                        "error": {"message": "上游输出中断：连接在生成过程中断开，"
                                  "已收到的内容不完整，请重试",
                                  "type": "api_error", "param": None,
                                  "code": "stream_truncated"}
                    }) + "\n\n"
                    if not tap.saw_done:
                        yield "data: [DONE]\n\n"
                    await settle(False)
                    return
                if w is not None and w.route is not None:
                    try:
                        from services.proxy_service import report_proxy_result
                        if w.route.proxy is not None:
                            await run_db(report_proxy_result, w.route.proxy.id, False)
                        # 宽松判胜下 winner 可能在首个结构合法 chunk 后即判死：
                        # 该 Key 竞速时已被记成功，这里补记失败保持统计可信
                        # （不标 invalid，仅累计 + 冷却）。
                        key_id = getattr(w.route.key, "id", None)
                        if key_id is not None:
                            await run_db(key_service.report_failure,
                                         key_id, "stream_died", 0)
                    except Exception:
                        pass
                    excluded.add((getattr(w.route.key, "id", None),
                                  getattr(w.route.proxy, "id", None)
                                  if w.route.proxy is not None else None))
                    if (w.route.proxy is not None
                            and getattr(w.route.proxy, "id", None) is not None):
                        excluded_proxies.add(w.route.proxy.id)
                last_exc = exc
                logger.info("stream attempt %d failed before any content, retrying: %s",
                            attempt + 1, exc)
                if attempt + 1 < max_attempts and backoff > 0:
                    async for _hb in _backoff_with_heartbeat(backoff):
                        yield _hb
            finally:
                if w is not None:
                    try:
                        await w.close()
                    except Exception:
                        pass
                if reserved:
                    _release_upstream(reserved)
                    reserved = 0
        if isinstance(last_exc, TimeoutError):
            await _safe_finish(holder["log"], holder["started"], False, 504,
                        "stream_idle_timeout", routes=all_reports or None)
            await settle(False)
            yield "data: " + json.dumps({
                "error": {"message": "上游连续无响应（"
                          f"{round(idle_timeout or 0, 1)} 秒"
                          "未收到任何数据），已判定线路死亡。可调大 stream_idle_timeout",
                          "type": "api_error", "param": None, "code": "stream_error"}
            }) + "\n\n"
        elif isinstance(last_exc, NoRouteAvailable):
            await _safe_finish(holder["log"], holder["started"], False, 503, "no_available_route")
            await settle(False)
            yield "data: " + json.dumps({
                "error": {"message": "当前没有可用线路或所有线路均失败", "type": "api_error",
                           "param": None, "code": "no_available_route"}
            }) + "\n\n"
        elif isinstance(last_exc, UpstreamTruncated):
            await _safe_finish(holder["log"], holder["started"], False, 502,
                        "upstream_truncated", routes=all_reports or None)
            await settle(False)
            yield "data: " + json.dumps({
                "error": {"message": "上游输出中断：所有线路的流均在完成前被上游关闭"
                          "（未收到 finish_reason/[DONE]）。已开启重试时将自动换线，"
                          "否则请重试请求",
                          "type": "api_error", "param": None,
                          "code": "upstream_truncated"}
            }) + "\n\n"
        else:
            report = getattr(last_exc, "report", None)
            content_rejected = bool(getattr(last_exc, "content_rejected", False))
            await _safe_finish(holder["log"], holder["started"], False, 502,
                        "upstream_content_rejected" if content_rejected
                        else "stream_error", routes=all_reports or report or None)
            await settle(False)
            if content_rejected:
                # 同非流式路径：探针确认"内容被拒"后如实分类（req_32382 案）
                yield "data: " + json.dumps({
                    "error": {"message": "上游拒绝了请求内容：所有线路均返回 400，"
                              "但同一通道的最小无害探针可通过——判定为请求内容命中"
                              "上游内容策略/参数校验（并非上游故障，重试无意义）。"
                              "请检查 system/消息内容，或更换模型/通道",
                              "type": "api_error", "param": None,
                              "code": "upstream_content_rejected"}
                }) + "\n\n"
            else:
                yield "data: " + json.dumps({
                    "error": {"message": "上游服务暂时不可用，请稍后重试", "type": "api_error",
                               "param": None, "code": "stream_error"}
                }) + "\n\n"
        yield "data: [DONE]\n\n"
    except (NoRouteAvailable, AllRoutesFailed) as exc:
        report = exc.report if isinstance(exc, AllRoutesFailed) else None
        await _safe_finish(holder["log"], holder["started"], False, 503, "no_available_route",
                     routes=report)
        await settle(False)
        yield "data: " + json.dumps({
            "error": {"message": "当前没有可用线路或所有线路均失败", "type": "api_error",
                       "param": None, "code": "no_available_route"}
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    except Exception:
        logger.exception("stream failed")
        await _safe_finish(holder["log"], holder["started"], False, 502, "stream_error")
        await settle(False)
        yield "data: " + json.dumps({
            "error": {"message": "上游服务暂时不可用，请稍后重试", "type": "api_error",
                       "param": None, "code": "stream_error"}
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    finally:
        try:
            if winner is not None:
                await winner.close()
        except Exception:
            pass
        if reserved:
            _release_upstream(reserved)
            reserved = 0
        _bump_active(-1)
        # 客户端中途断开（GeneratorExit/CancelledError 穿透生成器）时，
        # 正常收尾路径（drain 结束后的 log.save + settle）不会执行——
        # 日志永久滞留 pending、配额预占不结算。此处强制收尾；
        # settled 标记保证与正常路径幂等互斥（正常完成时 settle 已置位）。
        if not settled["done"]:
            settled["done"] = True
            rec = holder["log"]
            rec.status = "failed"
            rec.error_type = rec.error_type or (
                "stream_truncated" if winner is not None else "stream_error")
            rec.duration_ms = round(
                (time.monotonic() - holder["started"]) * 1000, 1)
            rec.http_status = rec.http_status or 200
            try:
                await run_db(rec.save)
                await run_db(api_key_service.record_result, user_key, False)
                await run_db(
                    api_key_service.record_usage,
                    user_key, rec.prompt_tokens or 0,
                    rec.completion_tokens or 0, rec.cached_tokens or 0,
                    1)  # 结算入口 claim_quota 预占的 1 token
            except Exception:
                logger.exception("stream client-disconnect settle failed")


async def _drain(winner, idle_timeout: float = 0,
                 heartbeat: float = 0, max_duration: float = 0,
                 content_idle_timeout: float = 0,
                 tap: "StreamTap | None" = None):
    """转发泵：从 winner 读上游 chunk 原样下发给客户端。

    透传纯度：本函数不改写任何字节；超时窗口内只注入 `: keep-alive`
    SSE 注释行（对 OpenAI/Anthropic/Responses 客户端均不可见）。
    每个真实 chunk 恰好解析一次（tap.feed），供超时档位切换与调用方
    记账共享——旧实现同一 chunk 在这里探测一次、调用方再解析两次。
    调用方不传 tap 时内部自建（超时档位切换仍正常）。
    """
    import asyncio
    import time as _time

    if tap is None:
        tap = StreamTap()

    ait = winner.lines()
    started = _time.monotonic()
    last_data = started

    async def take():
        try:
            return await ait.__anext__()
        except StopAsyncIteration:
            return None

    read_task: asyncio.Task | None = None
    seen_signal = False
    try:
        while True:
            if max_duration and max_duration > 0:
                remaining = max_duration - (_time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(
                        f"stream exceeded max duration {max_duration}s")
            else:
                remaining = 0.0

            interval = 0.0
            for tick in (heartbeat, idle_timeout):
                if tick and tick > 0:
                    interval = min(interval, tick) if interval > 0 else tick
            if remaining > 0:
                interval = min(interval, remaining) if interval > 0 else remaining

            if read_task is None:
                read_task = asyncio.ensure_future(take())
            if read_task.done():
                chunk = read_task.result()
                read_task = None
                if chunk is None:
                    break
                last_data = _time.monotonic()
                info = tap.feed(chunk) if tap is not None else None
                if not seen_signal and info is not None and (
                        info.has_payload or info.has_reasoning
                        or info.has_usage or info.has_finish or info.saw_done):
                    seen_signal = True
                yield chunk
                continue

            if interval > 0:
                done, _ = await asyncio.wait({read_task}, timeout=interval)
            else:
                done, _ = await asyncio.wait({read_task})
            if read_task.done():
                chunk = read_task.result()
                read_task = None
                if chunk is None:
                    break
                last_data = _time.monotonic()
                info = tap.feed(chunk) if tap is not None else None
                if not seen_signal and info is not None and (
                        info.has_payload or info.has_reasoning
                        or info.has_usage or info.has_finish or info.saw_done):
                    seen_signal = True
                yield chunk
            else:
                elapsed = _time.monotonic() - last_data
                if not seen_signal:
                    if idle_timeout and idle_timeout > 0 and elapsed > idle_timeout:
                        raise TimeoutError(
                            "upstream unresponsive: no bytes for "
                            f"{round(elapsed, 1)}s (> idle_timeout {idle_timeout}s)")
                elif content_idle_timeout and content_idle_timeout > 0:
                    if elapsed > content_idle_timeout:
                        raise TimeoutError(
                            "upstream idle after content: no bytes for "
                            f"{round(elapsed, 1)}s (> content_idle_timeout "
                            f"{content_idle_timeout}s)")
                if heartbeat and heartbeat > 0:
                    heartbeat_frame = ": keep-alive\n\n"
                    if tap is not None:
                        tap.feed(heartbeat_frame)  # 注释行：不产生信号
                    yield heartbeat_frame
    finally:
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


def _finish_log(log: RequestLog, started: float, success: bool, http_status: int,
                error_type: str = "", route_kind: str = "", key_name: str = "",
                proxy_name: str = "", proxy_ip: str = "", usage: dict | None = None,
                routes: list | None = None):
    log.status = "success" if success else "failed"
    log.http_status = http_status
    log.error_type = error_type
    log.duration_ms = round((time.monotonic() - started) * 1000, 1)
    if route_kind:
        log.winner_route_type = route_kind
        log.winner_key_name = key_name
        log.winner_proxy_name = proxy_name
        log.proxy_public_ip = proxy_ip
    if usage:
        log.prompt_tokens = usage.get("prompt_tokens", 0) or 0
        log.completion_tokens = usage.get("completion_tokens", 0) or 0
        log.total_tokens = usage.get("total_tokens", 0) or 0
        log.cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    if routes:
        log.routes = routes
    log.save()
    from services import channel_health
    channel_health.record(log.channel, success, http_status, error_type,
                          routes=routes if isinstance(routes, list) else None)
