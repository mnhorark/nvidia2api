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
from services import api_key_service, key_service
from services.load_balancer import build_routes
from services.race_engine import (
    AllRoutesFailed, NoRouteAvailable, race_chat, race_stream,
)
from .auth import openai_error

logger = logging.getLogger("nvidia2api.openai")

_request_semaphore = threading.BoundedSemaphore(settings.MAX_CONCURRENT_REQUESTS)


def _authenticate(request):
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return api_key_service.authenticate(auth[7:].strip())


def list_models(request):
    user_key = _authenticate(request)
    if user_key is None:
        return openai_error("Invalid API key", "invalid_api_key", 401, "authentication_error")
    if not user_key.enabled:
        return openai_error("API key disabled", "key_disabled", 403, "authentication_error")
    models = AIModel.objects.filter(enabled=True).order_by("model_name")
    return JsonResponse({
        "object": "list",
        "data": [
            {
                "id": m.model_name,
                "object": "model",
                "created": int(m.created_at.timestamp()),
                "owned_by": m.provider,
            }
            for m in models
        ],
    })


ALLOWED_PARAMS = {
    "model", "messages", "temperature", "top_p", "max_tokens", "stream",
    "stop", "frequency_penalty", "presence_penalty", "response_format",
    "tools", "tool_choice", "n", "seed",
}


@csrf_exempt
def chat_completions(request):
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)

    user_key = _authenticate(request)
    if user_key is None:
        return openai_error("Invalid API key", "invalid_api_key", 401, "authentication_error")
    if not user_key.enabled:
        return openai_error("API key disabled", "key_disabled", 403, "authentication_error")

    ok, reason = api_key_service.check_and_count(user_key)
    if not ok:
        if reason == "rate_limited":
            return openai_error("Rate limit exceeded", "rate_limit_exceeded", 429)
        return openai_error("API key disabled", "key_disabled", 403, "authentication_error")

    if not _request_semaphore.acquire(blocking=False):
        return openai_error("Server busy, too many concurrent requests",
                            "server_overloaded", 429)
    log = None
    try:
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return openai_error("Invalid JSON body", "invalid_request", 400, "invalid_request_error")

        if len(request.body) > 4 * 1024 * 1024:
            return openai_error("Request body too large", "payload_too_large", 413)

        model_name = body.get("model", "")
        messages = body.get("messages")
        if not model_name or not isinstance(messages, list) or not messages:
            return openai_error("model and messages are required", "invalid_request",
                                400, "invalid_request_error")
        if not AIModel.objects.filter(model_name=model_name, enabled=True).exists():
            return openai_error(f"The model '{model_name}' does not exist",
                                "model_not_found", 404, "invalid_request_error")

        stream = bool(body.get("stream"))
        upstream_body = {k: v for k, v in body.items() if k in ALLOWED_PARAMS and v is not None}

        request_id = key_service.new_request_id()
        routes = build_routes()
        log = RequestLog.objects.create(
            request_id=request_id, user_api_key=user_key, model=model_name,
            routes_count=len(routes), is_stream=stream,
        )
        started = time.monotonic()

        if not routes:
            _finish_log(log, started, False, 503, "no_available_route")
            return openai_error("当前没有可用线路（没有可用的 NVIDIA Key）",
                                "no_available_route", 503)

        if stream:
            log_id_holder = {"log": log, "started": started}
            resp = _stream_response(routes, upstream_body, log_id_holder, user_key)
            response = StreamingHttpResponse(resp, content_type="text/event-stream")
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            return response

        try:
            result = race_chat(routes, upstream_body, settings.NVIDIA_BASE_URL)
        except NoRouteAvailable:
            _finish_log(log, started, False, 503, "no_available_route")
            return openai_error("当前没有可用线路", "no_available_route", 503)
        except AllRoutesFailed as exc:
            _finish_log(log, started, False, 502, "all_routes_failed", routes=exc.report)
            return openai_error(f"所有线路均失败: {exc}", "upstream_error", 502)

        r = result.route
        usage = (result.payload or {}).get("usage") or {}
        _finish_log(log, started, True, result.http_status, "", route_kind=r.kind,
                    key_name=r.key.name, proxy_name=r.proxy.name if r.proxy else "",
                    proxy_ip=(r.proxy.public_ip if r.proxy else ""),
                    usage=usage, routes=result.report or [])
        api_key_service.record_result(user_key, True)
        return JsonResponse(result.payload, status=200)
    finally:
        _request_semaphore.release()


def _stream_response(routes, upstream_body, holder, user_key):
    import asyncio

    async def produce():
        from services.race_engine import race_stream
        return await race_stream(routes, upstream_body, settings.NVIDIA_BASE_URL)

    winner = None
    loop = asyncio.new_event_loop()
    try:
        winner = loop.run_until_complete(produce())
        log = holder["log"]
        log.winner_route_type = winner.route.kind
        log.winner_key_name = winner.route.key.name
        log.winner_proxy_name = winner.route.proxy.name if winner.route.proxy else ""
        log.proxy_public_ip = winner.route.proxy.public_ip if winner.route.proxy else ""
        log.status = "success"
        log.http_status = 200
        log.duration_ms = round((time.monotonic() - holder["started"]) * 1000, 1)
        log.routes = winner.report or []
        log.save()
        api_key_service.record_result(user_key, True)
        first = True
        for chunk in _drain(loop, winner):
            yield chunk
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("stream failed")
        _finish_log(holder["log"], holder["started"], False, 502, "stream_error")
        api_key_service.record_result(user_key, False)
        yield "data: " + json.dumps({
            "error": {"message": f"stream error: {exc}", "type": "api_error",
                       "param": None, "code": "stream_error"}
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    finally:
        loop.close()


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
    loop.run_until_complete(winner.close())


def _finish_log(log: RequestLog, started: float, success: bool, http_status: int,
                error_type: str = "", route_kind: str = "", key_name: str = "",
                proxy_name: str = "", proxy_ip: str = "", usage: dict | None = None,
                routes: list | None = None):
    log.status = "success" if success else "error"
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
    if routes:
        log.routes = routes
    log.save()
