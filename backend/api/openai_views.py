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

from apps.core.models import AIModel, RequestLog
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
        channel = channel_service.resolve(channel_slug)
        ms = list(channel.models.filter(enabled=True).order_by("model_name"))
        entries: list[tuple[AIModel, str]] = [
            (m, n) for m in ms for n in model_registry.public_names(m)
        ]
    else:
        entries = model_registry.list_public()
    return JsonResponse({"object": "list", "data": [_model_entry(m, n) for m, n in entries]})


def _resolve_target(name: str, channel_slug: str | None):
    """把客户端的 model 名解析成 (AIModel, Channel)；失败返回 (None, None)。

    /c/<slug> 前缀锁定渠道；否则走全局注册表（跨渠道）。
    """
    if channel_slug:
        channel = channel_service.resolve(channel_slug)
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


def _parse_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return None


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
        model, channel = _resolve_target(requested_name, slug)
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
    body = _parse_body(request)
    if body is None:
        return openai_error("Invalid JSON body", "invalid_request", 400, "invalid_request_error")
    if len(request.body) > 4 * 1024 * 1024:
        return openai_error("Request body too large", "payload_too_large", 413)
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
    body = _parse_body(request)
    if body is None:
        return openai_error("Invalid JSON body", "invalid_request", 400, "invalid_request_error")
    if len(request.body) > 4 * 1024 * 1024:
        return openai_error("Request body too large", "payload_too_large", 413)
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
    body = _parse_body(request)
    if body is None:
        return openai_error("Invalid JSON body", "invalid_request", 400, "invalid_request_error")
    if len(request.body) > 4 * 1024 * 1024:
        return openai_error("Request body too large", "payload_too_large", 413)
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
    body = _parse_body(request)
    if body is None:
        return openai_error("Invalid JSON body", "invalid_request", 400, "invalid_request_error")
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


def _stream_response(routes, upstream_body, holder, user_key, channel,
                     max_attempts: int = 1, proxy_group: int | None = None,
                     endpoint: str | None = None):
    import asyncio

    loop = asyncio.new_event_loop()
    winner = None
    sent_content = False
    last_exc: Exception | None = None
    try:
        for attempt in range(max_attempts):
            rs = routes if attempt == 0 else build_routes(
                channel, proxy_group=proxy_group, endpoint=endpoint)
            if not rs:
                last_exc = NoRouteAvailable()
                continue
            w = None
            try:
                w = loop.run_until_complete(race_stream(rs, upstream_body))
                winner = w
                log = holder["log"]
                log.winner_route_type = w.route.kind
                log.winner_key_name = w.route.key.name
                log.winner_proxy_name = w.route.proxy.name if w.route.proxy else ""
                log.proxy_public_ip = w.route.proxy.public_ip if w.route.proxy else ""
                log.status = "success"
                log.http_status = 200
                log.first_token_ms = round((time.monotonic() - holder["started"]) * 1000, 1)
                log.routes = w.report or []
                log.save()
                api_key_service.record_result(user_key, True)
                usage: dict = {}
                for chunk in _drain(loop, w):
                    if _chunk_has_content(chunk):
                        sent_content = True
                    try:
                        if chunk.startswith("data:"):
                            payload = json.loads(chunk[5:].strip())
                            if isinstance(payload, dict) and payload.get("usage"):
                                usage = payload["usage"]
                    except Exception:  # noqa: BLE001
                        pass
                    yield chunk
                log.duration_ms = round((time.monotonic() - holder["started"]) * 1000, 1)
                log.prompt_tokens = usage.get("prompt_tokens", 0) or 0
                log.completion_tokens = usage.get("completion_tokens", 0) or 0
                log.total_tokens = usage.get("total_tokens", 0) or 0
                log.cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                log.save()
                api_key_service.record_usage(
                    user_key,
                    log.prompt_tokens, log.completion_tokens, log.cached_tokens)
                return
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                # 竞速阶段全部失败（超时/401/403/429/5xx/无效响应等）：重建线路重试
                last_exc = exc
                logger.info("stream attempt %d failed, retrying: %s",
                            attempt + 1, exc)
            except Exception as exc:  # noqa: BLE001
                # 已向客户端输出过实际内容（delta 有 content/reasoning/tool_calls、
                # 出现 finish_reason 或 [DONE]）则响应已提交、无法干净重试；
                # 仅收到过心跳/空 delta 则本次线路视为失败，纳入自动重试。
                if sent_content:
                    raise
                last_exc = exc
                logger.info("stream attempt %d failed before any content, retrying: %s",
                            attempt + 1, exc)
            finally:
                if w is not None:
                    try:
                        loop.run_until_complete(w.close())
                    except Exception:  # noqa: BLE001
                        pass
        # 所有尝试均失败：竞速失败重建线路也无济于事，直接回上游错误
        if isinstance(last_exc, NoRouteAvailable):
            _finish_log(holder["log"], holder["started"], False, 503, "no_available_route")
            api_key_service.record_result(user_key, False)
            yield "data: " + json.dumps({
                "error": {"message": "当前没有可用线路或所有线路均失败", "type": "api_error",
                           "param": None, "code": "no_available_route"}
            }) + "\n\n"
        else:
            report = getattr(last_exc, "report", None)
            _finish_log(holder["log"], holder["started"], False, 502,
                        "stream_error", routes=report)
            api_key_service.record_result(user_key, False)
            yield "data: " + json.dumps({
                "error": {"message": "上游服务暂时不可用，请稍后重试", "type": "api_error",
                           "param": None, "code": "stream_error"}
            }) + "\n\n"
        yield "data: [DONE]\n\n"
    except (NoRouteAvailable, AllRoutesFailed) as exc:
        report = exc.report if isinstance(exc, AllRoutesFailed) else None
        _finish_log(holder["log"], holder["started"], False, 503, "no_available_route",
                    routes=report)
        api_key_service.record_result(user_key, False)
        yield "data: " + json.dumps({
            "error": {"message": "当前没有可用线路或所有线路均失败", "type": "api_error",
                       "param": None, "code": "no_available_route"}
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    except Exception:  # noqa: BLE001
        logger.exception("stream failed")
        _finish_log(holder["log"], holder["started"], False, 502, "stream_error")
        api_key_service.record_result(user_key, False)
        yield "data: " + json.dumps({
            "error": {"message": "上游服务暂时不可用，请稍后重试", "type": "api_error",
                       "param": None, "code": "stream_error"}
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    finally:
        try:
            if winner is not None:
                loop.run_until_complete(winner.close())
        except Exception:  # noqa: BLE001
            pass
        loop.close()
        _request_semaphore.release()
        _bump_active(-1)


def _drain(loop, winner):
    async def collect():
        out = []
        async for chunk in winner.lines():
            out.append(chunk)
        return out

    # Stream chunk groups to the caller as they arrive by progressive polling.
    ait = winner.lines()

    async def take():
        try:
            return await ait.__anext__()
        except StopAsyncIteration:
            return None

    while True:
        chunk = loop.run_until_complete(take())
        if chunk is None:
            break
        yield chunk


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
