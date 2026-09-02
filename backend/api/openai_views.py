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
    responses_api, sysconfig, thinking,
)
from services.load_balancer import build_routes
from services.race_engine import (
    AllRoutesFailed, NoRouteAvailable, race_chat, race_stream,
)
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


# 协议转换的内部控制字段——绝不透传给上游
_INTERNAL_ONLY = frozenset({"channel"})
# 显式不透传给上游的字段（避免 400 或语义错误）
# - thinking 族由 thinking.build_upstream 按目标 host 归一化后透传
_DROP_FOR_UPSTREAM = frozenset({
    "channel",
    "thinking",
    "enable_thinking",
    "reasoning",
    "reasoning_content",
    "reasoning_budget",
    "reasoning_effort",
    "reasoning_effort_override",
    "reasoning_enabled",
    "reasoning_config",
    "reasoning_level",
    "reasoning_mode",
    "reasoning_type",
    "reasoning_detail",
    "reasoning_details",
    "thinking_budget",
    "thinking_config",
    "thinking_enabled",
    "thinking_level",
    "thinking_mode",
    "thinking_type",
    "enable_thinking",
    "enabled_thinking",
    "is_thinking",
    "chat_template_kwargs",
    "clear_thinking",
    "grok_thinking",
    "thinking_beta",
    "betas",
    "openai",
    "anthropic",
})


def _build_upstream_body(body: dict, model_name: str, channel=None) -> dict:
    """通用参数透传 + 思考强度参数归一化下发。

    无损原则（对标 one-api / new-api / RikkaHub）：
    - 除 _DROP_FOR_UPSTREAM 的思考族字段外，全部忠实透传
    - 不要用白名单过滤未知字段——未来的官方参数会因此被静默丢弃
    - model 始终用真实模型名覆盖
    """
    # 透传所有非思考族参数（thinking 族由 build_upstream 归一化后透传）
    upstream = {
        k: v for k, v in body.items()
        if k not in _DROP_FOR_UPSTREAM and v is not None
    }
    # 思考参数归一化后下发（按渠道隔离）
    upstream.update(thinking.build_upstream(body, model_name, channel))
    # 上游必须用真实模型名（别名不透传）
    upstream["model"] = model_name
    # 流式 usage 选项
    if body.get("stream"):
        if "stream_options" not in upstream:
            upstream["stream_options"] = {"include_usage": True}
    return upstream


def _authorize(request):
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
    ok, reason = api_key_service.claim_quota(user_key)
    if not ok:
        return None, openai_error("Insufficient quota (quota exceeded)",
                                  "insufficient_quota", 402, "insufficient_quota")
    return user_key, None


MAX_BODY_BYTES = 4 * 1024 * 1024


def _parse_body(request):
    declared = request.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return None, openai_error("Request body too large",
                                          "payload_too_large", 413)
        except (TypeError, ValueError):
            pass
    try:
        raw = request.body or b""
    except RequestDataTooBig:
        return None, openai_error("Request body too large", "payload_too_large", 413)
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
    if not _try_acquire_request():
        return openai_error("Server busy, too many concurrent requests",
                            "server_overloaded", 429)
    log = None
    semaphore_released_by_stream = False
    upstream_reserved = 0
    try:
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

        model_name = model.model_name
        stream = bool(body.get("stream"))
        upstream_body = _build_upstream_body(body, model_name, channel)
        upstream_thinking = thinking.build_upstream(body, model_name, channel)
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
                routes = routes[:upstream_reserved]
        log = RequestLog.objects.create(
            channel=channel, request_id=request_id, user_api_key=user_key,
            model=requested_name, routes_count=len(routes), is_stream=stream,
            client_thinking=client_thinking, upstream_thinking=upstream_thinking,
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
                api_key_service.record_usage(user_key, reservation=1)  # 退还预占
                return openai_error("当前没有可用线路", "no_available_route", 503)
            report = getattr(last_exc, "report", None) or []
            logger.warning("all routes failed after %d attempt(s): %s",
                           max_attempts, last_exc)
            _finish_log(log, started, False, 502, "all_routes_failed", routes=report)
            api_key_service.record_result(user_key, False)
            api_key_service.record_usage(user_key, reservation=1)  # 退还预占
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
        if protocol == "responses":
            payload = responses_api.chat_to_responses_payload(payload, echo_body or body)
        elif protocol == "anthropic":
            payload = anthropic_api.chat_to_messages_payload(payload)
        return JsonResponse(payload, status=200)
    finally:
        if not semaphore_released_by_stream:
            _bump_active(-1)
            _release_upstream(upstream_reserved)


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
    return _run_authed(user_key, chat_body, channel_slug, "anthropic", echo_body=body)


@csrf_exempt
def anthropic_count_tokens(request, channel_slug: str | None = None):
    if request.method != "POST":
        return openai_error("Method not allowed", "method_not_allowed", 405)
    user_key, err = _authorize(request)
    if err:
        return err
    body, err = _parse_body(request)
    if err:
        return err
    return JsonResponse({"input_tokens": anthropic_api.count_tokens(body)})


_CONTENT_DELTA_KEYS = ("content", "tool_calls")


def _chunk_has_content(line: str) -> bool:
    if not line.startswith("data:"):
        return False
    payload = line[5:].strip()
    if payload == "[DONE]":
        return True
    try:
        data = json.loads(payload)
    except Exception:
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


def _chunk_has_any_signal(line: str) -> bool:
    if not line.startswith("data:"):
        return False
    payload = line[5:].strip()
    if payload == "[DONE]":
        return True
    try:
        data = json.loads(payload)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("usage"):
        return True
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0]
    if not isinstance(first, dict):
        return False
    if first.get("finish_reason"):
        return True
    if isinstance(first.get("text"), str) and first["text"]:
        return True
    delta = first.get("delta")
    if isinstance(delta, dict):
        for key in ("content", "reasoning_content", "reasoning", "tool_calls"):
            if delta.get(key):
                return True
    return False


async def _stream_response(routes, upstream_body, holder, user_key, channel,
                           max_attempts: int = 1, proxy_group: int | None = None,
                           endpoint: str | None = None):
    import asyncio

    winner = None
    sent_content = False
    done_sent = False
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

    try:
        for attempt in range(max_attempts):
            rs = routes if attempt == 0 else await run_db(
                build_routes, channel, proxy_group=proxy_group, endpoint=endpoint,
                exclude=excluded or None, exclude_proxies=excluded_proxies or None)
            if not rs:
                last_exc = NoRouteAvailable()
                if attempt + 1 < max_attempts and backoff > 0:
                    await asyncio.sleep(backoff)
                continue
            reserved = _reserve_upstream(len(rs))
            if reserved < len(rs):
                rs = rs[:reserved]
            if not rs:
                last_exc = NoRouteAvailable()
                _release_upstream(reserved)
                reserved = 0
                if attempt + 1 < max_attempts and backoff > 0:
                    await asyncio.sleep(backoff)
                continue
            w = None
            try:
                w = await race_stream(rs, upstream_body)
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
                usage: dict = {}
                completion_text: list[str] = []
                try:
                    async for chunk in _drain(w, idle_timeout, heartbeat,
                                      max_duration, content_idle_timeout):
                        if _chunk_has_content(chunk):
                            sent_content = True
                        if chunk.strip() == "data: [DONE]":
                            done_sent = True
                        try:
                            if chunk.startswith("data:"):
                                payload = json.loads(chunk[5:].strip())
                                if isinstance(payload, dict):
                                    if payload.get("usage"):
                                        usage = payload["usage"]
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
                        except Exception:
                            pass
                        yield chunk
                finally:
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
                logger.info("stream attempt %d failed, retrying: %s",
                            attempt + 1, exc)
                if attempt + 1 < max_attempts and backoff > 0:
                    await asyncio.sleep(backoff)
            except Exception as exc:
                if sent_content or done_sent:
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
                    await run_db(log.save)
                    if not done_sent:
                        yield "data: [DONE]\n\n"
                    await settle(True)
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
                    await asyncio.sleep(backoff)
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
        else:
            report = getattr(last_exc, "report", None)
            await _safe_finish(holder["log"], holder["started"], False, 502,
                        "stream_error", routes=all_reports or report or None)
            await settle(False)
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


async def _drain(winner, idle_timeout: float = 0,
                 heartbeat: float = 0, max_duration: float = 0,
                 content_idle_timeout: float = 0):
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
                if not seen_signal and _chunk_has_any_signal(chunk):
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
                if not seen_signal and _chunk_has_any_signal(chunk):
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
                    yield ": keep-alive\n\n"
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
