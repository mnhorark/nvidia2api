"""OpenAI-compatible endpoints: GET /v1/models, POST /v1/chat/completions."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core.models import AIModel, Channel, RequestLog
from services import (
    anthropic_api, api_key_service, channel_service, key_service, model_registry,
    responses_api, sysconfig, thinking,
)
from services.load_balancer import build_routes
from services.race_engine import (
    AllRoutesFailed, NoRouteAvailable, race_chat, race_stream,
)
from .auth import openai_error

logger = logging.getLogger("nvidia2api.openai")

_request_semaphore = threading.BoundedSemaphore(settings.MAX_CONCURRENT_REQUESTS)

# 实时在途请求计数：线程安全，供仪表盘"实时并发"展示。
_active_lock = threading.Lock()
_active_count = 0


def active_requests() -> int:
    """当前在途（已通过并发闸门且未结束）的请求数。"""
    with _active_lock:
        return _active_count


def _bump_active(delta: int) -> None:
    global _active_count
    with _active_lock:
        _active_count = max(0, _active_count + delta)


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
    """/v1/models 汇总所有渠道；/c/<slug>/v1/models 只看该渠道。
    每个对外名（主对外名 + 附加别名）各返回一条记录。
    """
    user_key = _authenticate(request)
    if user_key is None:
        return openai_error("Invalid API key", "invalid_api_key", 401, "authentication_error")
    if not user_key.enabled:
        return openai_error("API key disabled", "key_disabled", 403, "authentication_error")

    if channel_slug:
        channel = channel_service.lookup(channel_slug)
        if channel is None:
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
    """/c/<slug>/ 指定的渠道不存在。必须 404，不能静默回落到默认渠道。"""

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(f"channel not found: {slug}")


def _resolve_channel(slug: str | None) -> Channel | None:
    """解析 /c/<slug> 指定的渠道；不存在时抛 ChannelNotFound。"""
    if not slug:
        return None
    channel = channel_service.lookup(slug)
    if channel is None:
        raise ChannelNotFound(slug)
    return channel


def _resolve_target(name: str, channel_slug: str | None):
    """把客户端的 model 名解析成 (AIModel, Channel)；失败返回 (None, None)。

    /c/<slug> 前缀严格锁定渠道（未知 slug -> ChannelNotFound，由调用方转 404）；
    否则走全局注册表（跨渠道）。
    """
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
            # 模型存在但所属渠道被禁用，给个可操作的提示
            msg += f" (disabled channel(s): {', '.join(c.slug for c in owners)})"
    return openai_error(msg, "model_not_found", 404, "invalid_request_error")


ALLOWED_PARAMS = {
    "model", "messages", "temperature", "top_p", "max_tokens", "stream",
    "stop", "frequency_penalty", "presence_penalty", "response_format",
    "tools", "tool_choice", "n", "seed",
}


def _build_upstream_body(body: dict, model_name: str) -> dict:
    """通用参数透传 + 思考强度参数归一化下发。"""
    upstream = {
        k: v for k, v in body.items()
        if k in ALLOWED_PARAMS and k not in thinking.THINKING_PARAM_KEYS and v is not None
    }
    upstream.update(thinking.build_upstream(body, model_name))
    # 关键：上游必须用真实模型名。别名只在平台对外这一层存在，
    # 客户端用别名调用时，绝不能把别名原样透传给上游（否则上游 404）。
    upstream["model"] = model_name
    if body.get("stream"):
        # 请求流式 usage：部分上游（如 NVIDIA DeepSeek）默认流式不返回 usage，
        # 需显式 include_usage 才在收尾 chunk 里给出 token 统计。
        upstream["stream_options"] = {"include_usage": True}
    return upstream


def _authorize(request):
    """校验用户 API Key 与限流；返回 (user_key, error_response)。"""
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


# 请求体大小上限（字节）。必须在 json.loads 之前校验，否则"解析后再判大小"
# 形同虚设——超大请求照样吃满内存。
MAX_BODY_BYTES = 4 * 1024 * 1024


def _parse_body(request):
    """解析 JSON 请求体；返回 (body, error_response)。

    严格区分三类失败，避免把客户端的畸形输入变成 500：
    - 超出大小上限 -> 413（在解析之前按 Content-Length / 实际长度拦截）
    - 非法 JSON    -> 400 invalid_request
    - 合法 JSON 但不是对象（数组/字符串/数字/null）-> 400 invalid_request
      这类请求过去会让 `body.get(...)` 抛 AttributeError 直接 500。
    """
    declared = request.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return None, openai_error("Request body too large",
                                          "payload_too_large", 413)
        except (TypeError, ValueError):
            pass
    raw = request.body or b""
    if len(raw) > MAX_BODY_BYTES:
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
    """核心执行：校验 -> 建路由 -> 竞速(带重试) -> 按协议返回结果。

    `protocol`: "chat" | "responses"。内部一律以 chat 格式处理，
    responses 协议在入口(responses_to_chat_body)与出口(响应/SSE 转换)
    做格式转换，其余（竞速、重试、日志、限流）完全复用。
    `echo_body`: responses 协议出口回显用的原始 Responses 请求体。
    调用方已持有 _request_semaphore，此处负责释放。
    """
    if not _request_semaphore.acquire(blocking=False):
        return openai_error("Server busy, too many concurrent requests",
                            "server_overloaded", 429)
    _bump_active(1)
    log = None
    # 流式响应由 _stream_response 生成器在结束时释放信号量（覆盖客户端断开）。
    semaphore_released_by_stream = False
    try:
        # 渠道优先级：URL 前缀 > 请求体里的 channel 字段 > 按 model 名跨渠道解析
        requested_name = body.get("model", "")
        messages = body.get("messages")
        if not requested_name or not isinstance(messages, list) or not messages:
            return openai_error("model and messages are required", "invalid_request",
                                400, "invalid_request_error")

        slug = channel_slug or (str(body.get("channel") or "").strip() or None)
        try:
            model, channel = _resolve_target(requested_name, slug)
        except ChannelNotFound as exc:
            return openai_error(f"Unknown channel '{exc.slug}'",
                                "channel_not_found", 404, "invalid_request_error")
        if model is None:
            return _not_found_error(requested_name, slug)

        # 上游必须用真实模型名，别名只在平台对外这一层存在
        model_name = model.model_name
        stream = bool(body.get("stream"))
        upstream_body = _build_upstream_body(body, model_name)
        # 记录思考参数：客户端原始传入 + 实际下发到上游，供日志页排查
        upstream_thinking = thinking.build_upstream(body, model_name)
        client_thinking = {
            k: body.get(k) for k in thinking.THINKING_PARAM_KEYS
            if k in body and body.get(k) is not None
        }

        request_id = key_service.new_request_id()
        # 若模型绑定了独立代理分组，则仅在该分组内选代理；
        # 若模型设置了独立端点（如 /v1/responses），则覆盖渠道 chat 端点
        routes = build_routes(channel, proxy_group=model.proxy_group_id,
                              endpoint=model.endpoint)
        log = RequestLog.objects.create(
            channel=channel, request_id=request_id, user_api_key=user_key,
            model=requested_name, routes_count=len(routes), is_stream=stream,
            client_thinking=client_thinking, upstream_thinking=upstream_thinking,
        )
        started = time.monotonic()

        if not routes:
            _finish_log(log, started, False, 503, "no_available_route")
            api_key_service.record_result(user_key, False)
            return openai_error("当前没有可用线路（该渠道没有可用的 Key）",
                                "no_available_route", 503)

        # 自动重试:竞速失败时重建线路再试(retry_count 系统参数,上限 5)
        retries = max(0, min(int(sysconfig.get("retry_count", channel) or 0), 5))
        max_attempts = 1 + retries

        if stream:
            log_id_holder = {"log": log, "started": started}
            semaphore_released_by_stream = True
            gen = _stream_response(routes, upstream_body, log_id_holder,
                                   user_key, channel, max_attempts,
                                   proxy_group=model.proxy_group_id,
                                   endpoint=model.endpoint)
            if protocol == "responses":
                gen = responses_api.iter_chat_sse_as_responses(gen)
            elif protocol == "anthropic":
                gen = anthropic_api.iter_chat_sse_as_anthropic(gen)
            response = StreamingHttpResponse(gen, content_type="text/event-stream")
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            return response

        result = None
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            attempt_routes = routes if attempt == 0 else build_routes(
                channel, proxy_group=model.proxy_group_id, endpoint=model.endpoint)
            if not attempt_routes:
                last_exc = NoRouteAvailable()
                continue
            try:
                result = race_chat(attempt_routes, upstream_body)
                break
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    logger.info("request %s attempt %d failed, retrying: %s",
                                request_id, attempt + 1, exc)
        if result is None:
            if isinstance(last_exc, NoRouteAvailable):
                _finish_log(log, started, False, 503, "no_available_route")
                api_key_service.record_result(user_key, False)
                return openai_error("当前没有可用线路", "no_available_route", 503)
            report = getattr(last_exc, "report", None) or []
            logger.warning("all routes failed after %d attempt(s): %s",
                           max_attempts, last_exc)
            _finish_log(log, started, False, 502, "all_routes_failed", routes=report)
            api_key_service.record_result(user_key, False)
            return openai_error("上游服务暂时不可用，请稍后重试", "upstream_error", 502)

        r = result.route
        usage = (result.payload or {}).get("usage") or {}
        _finish_log(log, started, True, result.http_status, "", route_kind=r.kind,
                    key_name=r.key.name, proxy_name=r.proxy.name if r.proxy else "",
                    proxy_ip=(r.proxy.public_ip if r.proxy else ""),
                    usage=usage, routes=result.report or [])
        api_key_service.record_result(user_key, True)
        api_key_service.record_usage(
            user_key,
            usage.get("prompt_tokens", 0) or 0,
            usage.get("completion_tokens", 0) or 0,
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
        )
        payload = result.payload
        if protocol == "responses":
            payload = responses_api.chat_to_responses_payload(payload, echo_body or body)
        elif protocol == "anthropic":
            payload = anthropic_api.chat_to_messages_payload(payload)
        return JsonResponse(payload, status=200)
    finally:
        if not semaphore_released_by_stream:
            _request_semaphore.release()
            _bump_active(-1)


@csrf_exempt
def chat_completions(request, channel_slug: str | None = None):
    """POST /v1/chat/completions —— Chat Completions 协议。"""
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)
    user_key, err = _authorize(request)
    if err:
        return err
    body, err = _parse_body(request)
    if err:
        return err
    return _run_authed(user_key, body, channel_slug, "chat")


@csrf_exempt
def responses(request, channel_slug: str | None = None):
    """POST /v1/responses —— Responses API 协议。

    客户端请求体(input/max_output_tokens)转成内部 chat 后复用整套链路，
    出口再转回 Responses 响应/SSE 事件流。
    """
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
    return _run_authed(user_key, chat_body, channel_slug, "responses", echo_body=body)


@csrf_exempt
def anthropic_messages(request, channel_slug: str | None = None):
    """POST /v1/messages —— Anthropic Messages 协议。

    请求体（system/messages/tools/tool_choice/thinking/max_tokens）转成内部
    chat 后复用整套链路，出口再转回 Anthropic Message 对象 / SSE 事件流。
    """
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
    return _run_authed(user_key, chat_body, channel_slug, "anthropic", echo_body=body)


@csrf_exempt
def anthropic_count_tokens(request, channel_slug: str | None = None):
    """POST /v1/messages/count_tokens —— 估算 Anthropic 请求的 input token 数。"""
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)
    user_key, err = _authorize(request)
    if err:
        return err
    body, err = _parse_body(request)
    if err:
        return err
    return JsonResponse({"input_tokens": anthropic_api.count_tokens(body)})


# 只有"最终答案内容"才算已提交：content（正文）与 tool_calls（已承诺的
# 工具调用）。reasoning_content / reasoning 是思考过程，不属于最终答案——
# 思考阶段流中断（用户还没收到任何正文）应允许重建线路自动重试。
_CONTENT_DELTA_KEYS = ("content", "tool_calls")


def _chunk_has_content(line: str) -> bool:
    """该 SSE 行是否已向客户端交付"最终答案"级别的实际内容。

    纯心跳（choices 为空、delta 全空、无 finish_reason、无 usage）以及
    思考类 delta（reasoning_content / reasoning）不算——流在此阶段中断时
    客户端还未收到正文，可以安全重建线路重试。
    """
    if not line.startswith("data:"):
        return False
    payload = line[5:].strip()
    if payload == "[DONE]":
        return True
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    if data.get("usage"):
        return True
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return False
    first = choices[0]
    if not isinstance(first, dict):
        return False
    if first.get("finish_reason"):
        return True
    delta = first.get("delta")
    if isinstance(delta, dict):
        for key in _CONTENT_DELTA_KEYS:
            if delta.get(key):
                return True
    return False


async def _stream_response(routes, upstream_body, holder, user_key, channel,
                           max_attempts: int = 1, proxy_group: int | None = None,
                           endpoint: str | None = None):
    """流式响应（异步生成器）：竞速胜出后逐块转发上游 SSE。

    必须是 async 生成器：Django 对同步流式内容会 `sync_to_async(list(...))`
    一次性消费完整个生成器才下发，导致"假流式"。async 生成器被 ASGI 逐块下发。

    心跳与掐线（参考 new-api / sub-api / cliproxy 思路）：
    - stream_heartbeat_interval：上游静默时向客户端发 `: keep-alive` 心跳，
      防 NAT/负载均衡/客户端把连接误判为死，保持链路活性（流式保活）；
    - stream_probe_interval × stream_max_idle_probes：判死的"心跳机制"——
      连续 N 个探测周期无任何数据（含思考 token）判定线路死亡，
      思考模型持续吐 reasoning token 时不会被误掐；
    - stream_max_duration：整条流总时长兜底，防僵尸流；
    - 已向客户端交付正文后断流：绝不发 error 事件（会破坏 OpenAI SSE 解析，
      客户端报 "error decoding response body"），干净收尾 [DONE]；
    - 未交付正文前的失败：按 retry_count 重建线路重试（含停滞死线）。
    """
    import asyncio

    winner = None
    sent_content = False
    done_sent = False
    last_exc: Exception | None = None
    # 判死：连续 stream_max_idle_probes 个 stream_probe_interval 周期无任何数据
    # （含思考 token）视为"连续心跳失败"，总容忍 ≈ probe × count。
    probe_interval = float(sysconfig.get("stream_probe_interval", channel) or 0)
    max_idle_probes = int(sysconfig.get("stream_max_idle_probes", channel) or 0)
    heartbeat = float(sysconfig.get("stream_heartbeat_interval", channel) or 0)
    max_duration = float(sysconfig.get("stream_max_duration", channel) or 0)
    # 被静默/断流掐断的死线路（Key_id, proxy_id）集合：重试时排除，
    # 避免下一轮竞速又抽到同一假死线路（代理池质量差时尤其关键）。
    excluded: set[tuple[int, int | None]] = set()
    # 被判定死亡的坏代理集合：组合排除会被"同一代理换一把 Key"绕过，
    # 代理才是坏源大头，因此被掐断/竞速失败的线路的代理也一并即时排除。
    excluded_proxies: set[int] = set()
    # 每轮尝试的竞速明细累积展示（用户可在日志页看到发生过几次换线重试）。
    all_reports: list[dict] = []

    # 一次请求只能记一次成败。竞速胜出时并不代表请求成功：流式中途断流仍会
    # 走到失败分支，若两处各自 record_result，会出现 success/failed 各 +1 而
    # total 只 +1 的"双计"，成功率统计失真。这里用 settled 保证只结算一次，
    # 且成功判定推迟到流真正结束（见下方 settle(True)）。
    settled = {"done": False}

    def settle(success: bool) -> None:
        if settled["done"]:
            return
        settled["done"] = True
        api_key_service.record_result(user_key, success)

    try:
        for attempt in range(max_attempts):
            rs = routes if attempt == 0 else build_routes(
                channel, proxy_group=proxy_group, endpoint=endpoint,
                exclude=excluded or None, exclude_proxies=excluded_proxies or None)
            if not rs:
                last_exc = NoRouteAvailable()
                continue
            w = None
            try:
                w = await race_stream(rs, upstream_body)
                winner = w
                log = holder["log"]
                log.winner_route_type = w.route.kind
                log.winner_key_name = w.route.key.name
                log.winner_proxy_name = w.route.proxy.name if w.route.proxy else ""
                log.proxy_public_ip = w.route.proxy.public_ip if w.route.proxy else ""
                log.status = "success"
                log.http_status = 200
                # 首字 = 首个正文（content/tool_calls）到达时间，非首个思考 chunk
                all_reports.extend(w.report or [])
                log.routes = all_reports
                log.save()
                # 注意：此处不结算成功。竞速胜出 ≠ 请求成功，流式中途断流仍会
                # 计入失败；成功统一在流正常结束后由 settle(True) 结算。
                usage: dict = {}
                completion_text: list[str] = []
                try:
                    async for chunk in _drain(w, probe_interval, max_idle_probes,
                                      heartbeat, max_duration):
                        if _chunk_has_content(chunk):
                            if not sent_content:
                                log.first_token_ms = round(
                                    (time.monotonic() - holder["started"]) * 1000, 1)
                            sent_content = True
                        if chunk.strip() == "data: [DONE]":
                            done_sent = True
                        try:
                            if chunk.startswith("data:"):
                                payload = json.loads(chunk[5:].strip())
                                if isinstance(payload, dict):
                                    if payload.get("usage"):
                                        usage = payload["usage"]
                                    # 累积正文，供上游未返回 usage 时本地估算 token
                                    choices = payload.get("choices")
                                    if choices:
                                        delta = choices[0].get("delta") or {}
                                        for key in ("content", "reasoning_content", "reasoning"):
                                            v = delta.get(key)
                                            if isinstance(v, str) and v:
                                                completion_text.append(v)
                                                break
                                        text = choices[0].get("text")
                                        if isinstance(text, str) and text:
                                            completion_text.append(text)
                        except Exception:  # noqa: BLE001
                            pass
                        yield chunk
                finally:
                    # 正常结束或客户端断开/超时都收尾：记录耗时与已解析 token
                    log.duration_ms = round((time.monotonic() - holder["started"]) * 1000, 1)
                    if usage.get("prompt_tokens"):
                        log.prompt_tokens = usage["prompt_tokens"]
                    else:
                        # 上游未返回流式 usage（如 NVIDIA DeepSeek）：本地估算兜底
                        from services import tokenizer
                        log.prompt_tokens = tokenizer.estimate_messages_tokens(
                            upstream_body.get("messages"))
                    if usage.get("completion_tokens"):
                        log.completion_tokens = usage["completion_tokens"]
                    else:
                        from services import tokenizer
                        log.completion_tokens = tokenizer.estimate_tokens(
                            "".join(completion_text))
                    log.total_tokens = (log.prompt_tokens or 0) + (log.completion_tokens or 0)
                    log.cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                    log.save()
                    api_key_service.record_usage(
                        user_key,
                        log.prompt_tokens, log.completion_tokens, log.cached_tokens)
                settle(True)
                return
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                # 竞速阶段全部失败（连接/超时/401/403/429/5xx/无效响应等）：
                # 重建线路重新竞速（重试线路竞速）。失败线路（及其代理）即时
                # 纳入排除，避免重试原样再打同一批已失败的线路。
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
                logger.info("stream attempt %d failed, retrying: %s",
                            attempt + 1, exc)
            except Exception as exc:  # noqa: BLE001
                # 已向客户端交付过实际内容（正文/tool_calls/已发 [DONE]）：
                # 响应已提交，无法也不应重试。上游中途断流时绝不能发 error 事件
                # （会破坏 OpenAI SSE 解析，客户端报 "Transport error: error
                # decoding response body"），这里干净收尾 [DONE]。
                if sent_content or done_sent:
                    try:
                        if (w is not None and w.route is not None
                                and w.route.proxy is not None):
                            from services.proxy_service import report_proxy_result
                            report_proxy_result(w.route.proxy.id, False)
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning("stream truncated after content (req %s): %s",
                                   log.request_id, exc)
                    if not done_sent:
                        yield "data: [DONE]\n\n"
                    return
                # 未交付任何内容：视为线路失败，重建线路重试。
                if w is not None and w.route is not None:
                    try:
                        from services.proxy_service import report_proxy_result
                        if w.route.proxy is not None:
                            report_proxy_result(w.route.proxy.id, False)
                    except Exception:  # noqa: BLE001
                        pass
                    # 被静默/断流掐断的死线路（Key+代理组合）加入排除集合，
                    # 下一轮竞速不再抽到同一组合，避免立刻又打到死线路。
                    # （getattr 兼容测试用 SimpleNamespace mock）
                    excluded.add((getattr(w.route.key, "id", None),
                                  getattr(w.route.proxy, "id", None)
                                  if w.route.proxy is not None else None))
                    # 组合排除会被"同一坏代理换一把 Key"绕过：代理才是坏源大头，
                    # 掐断线路的代理也一并即时排除（代理池质量差时尤为关键）。
                    if (w.route.proxy is not None
                            and getattr(w.route.proxy, "id", None) is not None):
                        excluded_proxies.add(w.route.proxy.id)
                last_exc = exc
                logger.info("stream attempt %d failed before any content, retrying: %s",
                            attempt + 1, exc)
            finally:
                if w is not None:
                    try:
                        await w.close()
                    except Exception:  # noqa: BLE001
                        pass
        # 所有尝试均失败：竞速失败重建线路也无济于事，直接回上游错误
        if isinstance(last_exc, TimeoutError):
            _finish_log(holder["log"], holder["started"], False, 504,
                        "stream_idle_timeout", routes=all_reports or None)
            settle(False)
            yield "data: " + json.dumps({
                "error": {"message": "上游连续无响应（"
                          f"{int(max_idle_probes or 0)}×{round(probe_interval or 0, 1)} 秒"
                          "未收到任何数据），已判定线路死亡。可调大 stream_probe_interval"
                          " / stream_max_idle_probes",
                          "type": "api_error", "param": None, "code": "stream_error"}
            }) + "\n\n"
        elif isinstance(last_exc, NoRouteAvailable):
            _finish_log(holder["log"], holder["started"], False, 503, "no_available_route")
            settle(False)
            yield "data: " + json.dumps({
                "error": {"message": "当前没有可用线路或所有线路均失败", "type": "api_error",
                           "param": None, "code": "no_available_route"}
            }) + "\n\n"
        else:
            report = getattr(last_exc, "report", None)
            _finish_log(holder["log"], holder["started"], False, 502,
                        "stream_error", routes=all_reports or report or None)
            settle(False)
            yield "data: " + json.dumps({
                "error": {"message": "上游服务暂时不可用，请稍后重试", "type": "api_error",
                           "param": None, "code": "stream_error"}
            }) + "\n\n"
        yield "data: [DONE]\n\n"
    except (NoRouteAvailable, AllRoutesFailed) as exc:
        report = exc.report if isinstance(exc, AllRoutesFailed) else None
        _finish_log(holder["log"], holder["started"], False, 503, "no_available_route",
                    routes=report)
        settle(False)
        yield "data: " + json.dumps({
            "error": {"message": "当前没有可用线路或所有线路均失败", "type": "api_error",
                       "param": None, "code": "no_available_route"}
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    except Exception:  # noqa: BLE001
        logger.exception("stream failed")
        _finish_log(holder["log"], holder["started"], False, 502, "stream_error")
        settle(False)
        yield "data: " + json.dumps({
            "error": {"message": "上游服务暂时不可用，请稍后重试", "type": "api_error",
                       "param": None, "code": "stream_error"}
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    finally:
        try:
            if winner is not None:
                await winner.close()
        except Exception:  # noqa: BLE001
            pass
        _request_semaphore.release()
        _bump_active(-1)


async def _drain(winner, probe_interval: float = 0, max_idle_probes: int = 0,
                 heartbeat: float = 0, max_duration: float = 0):
    """逐块转发上游 SSE：心跳保活 + 连续"心跳探测"失败判死 + 总时长兜底。

    - `heartbeat` > 0：上游静默超过该秒数时向客户端发送 SSE 注释心跳
      `: keep-alive`，证明平台↔客户端的连接仍然活着（NAT / 负载均衡 /
      客户端读超时不会误杀），实现"流式保活"；
    - `probe_interval` / `max_idle_probes`：**判死的心跳机制**。SSE 是 HTTP 单向流，
      没有 WebSocket 那种应用层 Pong 帧，平台无法向上游"发心跳等响应"；此处取其
      在 HTTP 上的等价形式：上游在单个探测周期内没有任何字节（含思考 token）即
      视为一次"心跳失败"，**连续 max_idle_probes 次失败**（总时长 ≈ probe_interval
      × max_idle_probes）才判定连接真死——对应参考项目"连续 N 次 Pong 超时"的判死
      逻辑，避免网络抖动一次误杀。任何数据（含 token 流）到达即清零重计：
      生成慢但连接活着绝不误杀，"无 token 判死"与"心跳判死"互相辅助；
    - `max_duration` > 0：整条流超过该秒数强制收尾（僵尸流兜底）。

    实现要点：上游读取使用**常驻 read_task**，心跳期间不取消这个 pending read
    （asyncio.wait_for 会在超时瞬间取消底层读取，导致真实流被误杀）。因此
    心跳/探测只是"观察"read_task 是否完成，而永不打断它。

    其余任何异常（连接被切断 / 解码失败等）原样上抛，由上层决定重试或收尾。
    """
    import asyncio
    import time as _time

    ait = winner.lines()
    started = _time.monotonic()
    last_data = started

    async def take():
        try:
            return await ait.__anext__()
        except StopAsyncIteration:
            return None

    read_task: asyncio.Task | None = asyncio.ensure_future(take())
    try:
        while True:
            if max_duration and max_duration > 0:
                remaining = max_duration - (_time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(
                        f"stream exceeded max duration {max_duration}s")
            else:
                remaining = 0.0

            # 本轮等待间隔 = 心跳节拍与探测节拍中较细的一个（有数据/心跳交错的粒度）
            interval = 0.0
            for tick in (heartbeat, probe_interval):
                if tick and tick > 0:
                    interval = min(interval, tick) if interval > 0 else tick
            if remaining > 0:
                interval = min(interval, remaining) if interval > 0 else remaining

            if read_task is None or read_task.done():
                read_task = asyncio.ensure_future(take())
            if interval > 0:
                done, _ = await asyncio.wait({read_task}, timeout=interval)
            else:
                done, _ = await asyncio.wait({read_task})
            if read_task.done():
                chunk = read_task.result()  # 异常（断流/解码失败）原样上抛
                if chunk is None:
                    break
                last_data = _time.monotonic()  # 任何数据到达：心跳探测计数清零
                read_task = None
                yield chunk
            else:
                # 静默期：推进"心跳失败"计数，达到连续失败上限才判真死；期间发客户端保活
                if probe_interval and probe_interval > 0 and max_idle_probes and max_idle_probes > 0:
                    elapsed = _time.monotonic() - last_data
                    misses = int(elapsed // probe_interval)
                    if misses >= max_idle_probes:
                        raise TimeoutError(
                            "upstream unresponsive: no bytes for "
                            f"{round(elapsed, 1)}s (> {max_idle_probes}×{probe_interval}s)")
                if heartbeat and heartbeat > 0:
                    yield ": keep-alive\n\n"
    finally:
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001
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
    channel_health.record(log.channel, success, http_status, error_type)
