"""Concurrent request racing across routes.

- Non-streaming: all routes race; the first *valid* response wins; rest cancelled.
- Streaming: routes race until one yields a first *valid* SSE chunk; that route
  becomes the winner and its stream is forwarded; others are cancelled.
- `is_valid_response` never treats bare HTTP 200 as success.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AnyStr, AsyncIterator

import httpx
from django.conf import settings

from services import responses_api
from services.key_service import report_failure, report_success
from services.load_balancer import Route
from services.proxy_service import report_proxy_result

logger = logging.getLogger("nvidia2api.race")


@dataclass
class RaceResult:
    ok: bool
    route: Route | None = None
    payload: dict | None = None
    http_status: int = 0
    error_type: str = ""
    error_message: str = ""
    latency_ms: float = 0.0
    report: list[dict] | None = None  # per-route outcomes of the whole race


def route_info(route: Route, status: str, latency_ms: float = 0.0,
               error: str = "", http_status: int = 0) -> dict:
    return {
        "name": route.name,
        "kind": route.kind,
        "key_name": route.key.name,
        "proxy_name": route.proxy.name if route.proxy else "",
        "status": status,                     # winner / failed / cancelled
        "latency_ms": round(latency_ms, 1),
        "error": error,
        "http_status": http_status,
    }


class NoRouteAvailable(Exception):
    pass


class AllRoutesFailed(Exception):
    def __init__(self, errors: list[str], report: list[dict] | None = None):
        self.errors = errors
        self.report = report or []
        super().__init__("; ".join(errors[:5]))


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def is_valid_response(status_code: int, data: dict) -> bool:
    if status_code != 200:
        return False
    if not isinstance(data, dict):
        return False
    if "error" in data and data["error"]:
        return False
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return False
    first = choices[0]
    msg = first.get("message") or first.get("delta")
    if msg is None and not first.get("text"):
        return False
    return True


def is_valid_stream_chunk(line: str) -> dict | None:
    """Return parsed chunk dict if it is a valid SSE data line, else None."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return {"done": True}
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    choices = data.get("choices")
    if not choices:
        return None
    return data


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def _client_kwargs(route: Route, stream: bool) -> dict:
    from services import sysconfig
    channel = route.key.channel
    read = sysconfig.get("upstream_read_timeout", channel)
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(
            connect=sysconfig.get("upstream_connect_timeout", channel),
            read=read, write=read, pool=read,
        ),
    }
    if route.proxy is not None:
        kwargs["proxy"] = route.proxy.url
    return kwargs


def _route_url(route: Route) -> str:
    """该线路的上游完整 chat 端点，由渠道决定；模型级 endpoint 覆盖优先。"""
    channel = route.key.channel
    if route.url_override:
        ep = route.url_override.strip()
        if ep.startswith("http://") or ep.startswith("https://"):
            return ep
        if channel is not None:
            from apps.core.models import join_url
            return join_url(channel.base_url, ep)
    if channel is None:
        from django.conf import settings
        return f"{settings.NVIDIA_BASE_URL}/chat/completions"
    return channel.chat_url


def _route_headers(route: Route) -> dict:
    from services import upstream_service

    channel = route.key.channel
    if channel is None:
        return {"Authorization": f"Bearer {route.key.api_key}",
                "Content-Type": "application/json"}
    return upstream_service.auth_headers(channel, route.key.api_key)


def _classify_error(exc: Exception) -> tuple[str, int]:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", 0
    if isinstance(exc, httpx.ConnectError):
        return "connect_error", 0
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", 0
    return "network_error", 0


async def _do_request(route: Route, body: dict,
                      started: float | None = None) -> RaceResult:
    import time as _time
    t0 = started if started is not None else _time.monotonic()

    def _elapsed() -> float:
        return (_time.monotonic() - t0) * 1000

    headers = _route_headers(route)
    url = _route_url(route)
    # 模型级端点若为 Responses API（/responses），请求体与响应格式都需转换
    is_resp = responses_api.is_responses_url(url)
    body_to_send = responses_api.chat_to_responses_body(body) if is_resp else body
    try:
        async with httpx.AsyncClient(**_client_kwargs(route, False)) as client:
            resp = await client.post(url, json=body_to_send, headers=headers)
            data: dict[str, Any] = {}
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                _mark_failure(route, "invalid_json", resp.status_code)
                return RaceResult(ok=False, route=route, http_status=resp.status_code,
                                  error_type="invalid_json", latency_ms=_elapsed())
            if is_resp:
                data = responses_api.responses_payload_to_chat(data)
            if not is_valid_response(resp.status_code, data):
                typ = _classify_status(resp.status_code, data)
                _mark_failure(route, typ, resp.status_code)
                return RaceResult(ok=False, route=route, http_status=resp.status_code,
                                  error_type=typ, latency_ms=_elapsed(),
                                  error_message=str(data.get("error", ""))[:256])
            _mark_success(route)
            return RaceResult(ok=True, route=route, payload=data,
                              http_status=resp.status_code, latency_ms=_elapsed())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        typ, _ = _classify_error(exc)
        _mark_failure(route, typ, 0)
        return RaceResult(ok=False, route=route, error_type=typ, error_message=str(exc),
                          latency_ms=_elapsed())


def _classify_status(code: int, data: dict) -> str:
    mapping = {401: "invalid_key", 403: "forbidden", 404: "model_not_found", 429: "rate_limited"}
    if code in mapping:
        return mapping[code]
    if code >= 500:
        return "upstream_server_error"
    if code == 200:
        return "invalid_response"
    return f"http_{code}"


def _mark_success(route: Route):
    report_success(route.key.id)
    if route.proxy is not None:
        report_proxy_result(route.proxy.id, True)


def _mark_failure(route: Route, error_type: str, http_status: int):
    report_failure(route.key.id, error_type, http_status)
    if route.proxy is not None and http_status == 0:
        report_proxy_result(route.proxy.id, False)


# ---------------------------------------------------------------------------
# racing
# ---------------------------------------------------------------------------

async def _race(routes: list[Route], body: dict) -> RaceResult:
    import time as _time
    if not routes:
        raise NoRouteAvailable()
    t0 = _time.monotonic()
    tasks: dict[asyncio.Task, Route] = {
        asyncio.ensure_future(_do_request(r, body, t0)): r for r in routes
    }
    report: list[dict] = []
    errors: list[str] = []
    try:
        pending = set(tasks.keys())
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                result = t.result()
                if result.ok:
                    report.append(route_info(result.route, "winner",
                                             result.latency_ms, "", result.http_status))
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for p in pending:
                        r = tasks[p]
                        report.append(route_info(
                            r, "cancelled",
                            (_time.monotonic() - t0) * 1000, "winner decided"))
                    result.report = report
                    return result
                errors.append(f"{result.route.name}:{result.error_type}")
                report.append(route_info(result.route, "failed", result.latency_ms,
                                         result.error_type, result.http_status))
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    raise AllRoutesFailed(errors, report)


async def _stream_first_valid(route: Route, body: dict):
    """Open a streaming connection; yield (client_ctx, response, first_chunk) on validity."""
    headers = _route_headers(route)
    cm = httpx.AsyncClient(**_client_kwargs(route, True))
    client = await cm.__aenter__()
    try:
        url = _route_url(route)
        is_resp = responses_api.is_responses_url(url)
        body_to_send = responses_api.chat_to_responses_body(body) if is_resp else body
        req_cm = client.stream("POST", url, json=body_to_send, headers=headers)
        resp = await req_cm.__aenter__()
        if resp.status_code != 200:
            typ = _classify_status(resp.status_code, {})
            _mark_failure(route, typ, resp.status_code)
            await req_cm.__aexit__(None, None, None)
            await cm.__aexit__(None, None, None)
            return None, route_info(route, "failed", error=typ, http_status=resp.status_code)
        first_line: str | None = None
        ait = resp.aiter_lines()
        async for line in ait:
            if not line.strip():
                continue
            if is_resp:
                if responses_api.parse_stream_event(line) is not None:
                    first_line = line
                    break
            elif is_valid_stream_chunk(line) is not None:
                first_line = line
                break
            # a data line present but invalid -> invalid response
            if line.startswith("data:"):
                _mark_failure(route, "invalid_response", 200)
                await req_cm.__aexit__(None, None, None)
                await cm.__aexit__(None, None, None)
                return None, route_info(route, "failed", error="invalid_response", http_status=200)
        if first_line is None:
            _mark_failure(route, "empty_stream", 200)
            await req_cm.__aexit__(None, None, None)
            await cm.__aexit__(None, None, None)
            return None, route_info(route, "failed", error="empty_stream", http_status=200)
        _mark_success(route)
        return (cm, req_cm, resp, ait, first_line), None
    except asyncio.CancelledError:
        await cm.__aexit__(None, None, None)
        raise
    except Exception as exc:  # noqa: BLE001
        typ, _ = _classify_error(exc)
        _mark_failure(route, typ, 0)
        try:
            await cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        return None, route_info(route, "failed", error=typ)


def race_chat(routes: list[Route], body: dict) -> RaceResult:
    """Synchronous entry: race non-streaming chat completion."""
    return asyncio.run(_race(routes, body))


async def race_stream_winner(routes: list[Route], body: dict):
    """Race streaming connections; returns (route, cm, req_cm, resp, aiter, first_line, report)."""
    import time as _time
    if not routes:
        raise NoRouteAvailable()
    t0 = _time.monotonic()
    tasks = {
        asyncio.ensure_future(_stream_first_valid(r, body)): r for r in routes
    }
    report: list[dict] = []
    n_failed = 0
    try:
        pending = set(tasks.keys())
        while pending and n_failed < len(routes):
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                res, fail_info = t.result()
                if res is not None:
                    winner_route = tasks[t]
                    latency = (_time.monotonic() - t0) * 1000
                    report.append(route_info(winner_route, "winner", latency))
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for p in pending:
                        report.append(route_info(
                            tasks[p], "cancelled",
                            (_time.monotonic() - t0) * 1000, "winner decided"))
                    cm, req_cm, resp, ait, first_line = res
                    return winner_route, cm, req_cm, resp, ait, first_line, report
                if fail_info:
                    report.append(fail_info)
                n_failed += 1
    finally:
        pass
    failures = [r for r in report if r.get("status") == "failed"] or report
    raise AllRoutesFailed(
        [f"{f['name']}:{f['error']}" for f in failures], report=report)


async def iter_sse(first_line: str, aiter, include_first: bool = True) -> AsyncIterator[str]:
    """Yield SSE lines: the validating first chunk, then the remainder, then [DONE]."""
    if include_first:
        yield first_line + "\n\n"
    saw_done = False
    async for line in aiter:
        if not line.strip():
            continue
        if line.strip() == "data: [DONE]":
            saw_done = True
        yield line + "\n\n"
    if not saw_done:
        yield "data: [DONE]\n\n"


@dataclass
class StreamWinner:
    route: Route
    cm: Any
    req_cm: Any
    aiter: Any
    first_line: str
    report: list[dict] = None  # type: ignore[assignment]

    async def lines(self) -> AsyncIterator[str]:
        if responses_api.is_responses_url(_route_url(self.route)):
            async for chunk in responses_api.iter_responses_sse(self.first_line, self.aiter):
                yield chunk
        else:
            async for chunk in iter_sse(self.first_line, self.aiter):
                yield chunk

    async def close(self):
        try:
            await self.req_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


async def race_stream(routes: list[Route], body: dict) -> StreamWinner:
    route, cm, req_cm, resp, ait, first_line, report = await race_stream_winner(routes, body)
    return StreamWinner(route=route, cm=cm, req_cm=req_cm, aiter=ait, first_line=first_line,
                        report=report)
