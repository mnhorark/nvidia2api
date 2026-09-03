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

from services import responses_api, sysconfig
from services.key_service import report_failure, report_success
from services.load_balancer import Route
from services.proxy_service import report_proxy_result
from services.reasoning_decrypt import (
    StreamReasoningDecryptor, decrypt_sse_chunk,
)
from services.tool_stream import ToolCallStreamNormalizer

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


class UpstreamTruncated(Exception):
    """上游流在未发出 finish_reason/[DONE] 的情况下静默结束（截断）。

    典型场景：长文档生成到一半，上游网关/代理掐断长连接。旧行为是
    伪造 [DONE] 伪装成功；现在如实上报，未交付内容时走换线重试，
    已交付内容时向客户端显式报错。
    """


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
    """Return parsed chunk dict if it is a valid SSE data line, else None.

    采用宽松判胜（与初始提交一致）：只要结构合法的 `choices` 数组出现即算
    "线路开始响应"——包括空 delta、纯 role 标记等。竞速胜负应比"谁先拿到
    内容"更快地锁定在"谁先开始出流"，避免思考模型（静默几十秒才吐首个
    内容块）把竞速窗口拖到几十秒。正文/思考的到达速度是模型特性，
    胜出后默认完全透传（stream_idle_timeout 作为可配置的僵尸流兜底）。

    保留的关键防呆（不退化的旧 bug）：
    - 裸 `data: [DONE]` 仍判 None：上游空响应 / 立即结束不能当选胜者，
      否则客户端收到空白回答且不走换线重试；
    - `error` 字段存在判 None；无法解析判 None。
    """
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return None
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return None
    return data


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def _client_kwargs(route: Route, stream: bool) -> dict:
    from services import sysconfig
    channel = route.key.channel
    read = sysconfig.get("upstream_read_timeout", channel)
    if stream:
        # 流式请求不设每读超时（read=None）：思考模型可能合法停顿数十秒~数分钟，
        # 固定读超时会在中途掐断（客户端报 "error decoding response body"）。
        # 死线路统一由应用层探测制兜底：竞速阶段 stream_first_byte_timeout、
        # 转发阶段 stream_idle_timeout + 心跳探测（见 openai_views._drain）。
        read = None
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
    from services.crypto import decrypt_secret

    channel = route.key.channel
    key_value = decrypt_secret(route.key.api_key)
    if channel is None:
        return {"Authorization": f"Bearer {key_value}",
                "Content-Type": "application/json"}
    return upstream_service.auth_headers(channel, key_value)


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
                await _mark_failure(route, "invalid_json", resp.status_code)
                return RaceResult(ok=False, route=route, http_status=resp.status_code,
                                  error_type="invalid_json", latency_ms=_elapsed())
            if is_resp:
                data = responses_api.responses_payload_to_chat(data)
            # muse-spark 等上游的 reasoning_content 可能是 Fernet 密文（gAAAA…），
            # 参考 RikkaHub 的 encrypted_content 逻辑，有密钥时尝试解密后再展示/计分
            # 同时处理 Kilo/OpenRouter 网关返回的加密 reasoning 内容
            try:
                msg = (data.get("choices") or [{}])[0].get("message") if isinstance(data, dict) else None
                if isinstance(msg, dict):
                    from services.reasoning_decrypt import decrypt_chat_message as _dec_msg
                    _dec_msg(msg)
                    # 额外处理：Kilo/OpenRouter 网关可能在 message 的其他字段里塞了加密思考内容
                    for _key in ("reasoning", "thinking"):
                        _val = msg.get(_key)
                        if isinstance(_val, str) and _val.startswith("gAAAA"):
                            from services.reasoning_decrypt import decrypt_token as _dec_tok
                            _dec_val = _dec_tok(_val)
                            if _dec_val is not None:
                                msg[_key] = _dec_val
            except Exception:
                pass
            if not is_valid_response(resp.status_code, data):
                typ = _classify_status(resp.status_code, data)
                await _mark_failure(route, typ, resp.status_code)
                return RaceResult(ok=False, route=route, http_status=resp.status_code,
                                  error_type=typ, latency_ms=_elapsed(),
                                  error_message=_error_detail(data))
            await _mark_success(route)
            return RaceResult(ok=True, route=route, payload=data,
                              http_status=resp.status_code, latency_ms=_elapsed())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        typ, _ = _classify_error(exc)
        await _mark_failure(route, typ, 0)
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


def _error_detail(data) -> str:
    """从上游错误响应体提取人类可读的原因（供日志/竞速明细展示）。

    兼容多种错误格式：
    - OpenAI:  {"error": {"message": "...", "code": "..."}}
    - NVIDIA:  {"status":400, "title":"Bad Request", "detail":"Function id ...: DEGRADED..."}
    - 通用:    {"message": "..."} / {"detail": "..."}
    此前只取 `data.error`，NVIDIA 类 400（detail/title）被忽略 → 日志只显示 http_400，
    用户无法得知真实原因（如 "DEGRADED function cannot be invoked"）。
    """
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or ""
        return str(msg)[:256]
    if isinstance(err, str) and err:
        return err[:256]
    for key in ("detail", "message", "title"):
        v = data.get(key)
        if v:
            return str(v)[:256]
    return ""


async def _mark_success(route: Route):
    """线路统计写库。竞速运行在事件循环上，写锁竞争时会冻结整个服务
    （busy_timeout 最长 30s），因此挪到线程池执行；事务块内（测试）自动
    回落同线程。"""
    from services.loop_offload import run_db

    await run_db(report_success, route.key.id)
    if route.proxy is not None:
        await run_db(report_proxy_result, route.proxy.id, True)


async def _mark_failure(route: Route, error_type: str, http_status: int):
    """同 _mark_success：统计写库不阻塞事件循环。"""
    from services.loop_offload import run_db

    await run_db(report_failure, route.key.id, error_type, http_status)
    if route.proxy is not None and http_status == 0:
        await run_db(report_proxy_result, route.proxy.id, False)


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
                try:
                    result = t.result()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    # 任务内部异常（如极端情况下的 DB 写锁）不应杀死整个竞速：
                    # 按失败线路处理，让其余线路继续竞速。
                    r = tasks[t]
                    err = getattr(exc, "code", "") or type(exc).__name__
                    errors.append(f"{r.name}:task_error")
                    report.append(route_info(
                        r, "failed", (_time.monotonic() - t0) * 1000, err, 0))
                    continue
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
        # 正常路径下走到这里所有任务都已结束；但若循环体内抛了意外异常，
        # 未结束的任务必须先取消，否则 gather 会一直等下去。
        if tasks:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks.keys(), return_exceptions=True)
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
            await _mark_failure(route, typ, resp.status_code)
            # 尝试读取错误响应体（有限字节）提取真实原因（如 NVIDIA 的
            # "DEGRADED function cannot be invoked"），供竞速明细展示
            err_detail = ""
            try:
                raw = (await resp.aread())[:2048]
                if raw:
                    import json as _json
                    err_detail = _error_detail(_json.loads(raw))
            except Exception:  # noqa: BLE001
                pass
            await req_cm.__aexit__(None, None, None)
            await cm.__aexit__(None, None, None)
            # error 字段带真实原因（如 "http_400: Function id ...: DEGRADED..."），
            # 日志页不再只显示笼统的 http_400
            return None, route_info(
                route, "failed",
                error=f"{typ}: {err_detail}" if err_detail else typ,
                http_status=resp.status_code)
        first_line: str | None = None
        # 竞速 prelude：判胜首帧之前消费掉的所有非空行（usage 预告帧、
        # 心跳注释、event: 行、上游自定义事件）。零丢失原则：这些字节
        # 是上游真实发出的，胜者确定后必须按原序重放，不得蒸发。
        prelude: list[str] = []
        ait = resp.aiter_lines()
        first_byte_timeout = float(
            sysconfig.get("stream_first_byte_timeout", route.key.channel) or 0)
        try:
            while True:
                if first_byte_timeout and first_byte_timeout > 0:
                    try:
                        line = await asyncio.wait_for(
                            ait.__anext__(), timeout=first_byte_timeout)
                    except asyncio.TimeoutError:
                        # 连上但迟迟无数据 -> 死线路：按失败处理，竞速换线
                        await _mark_failure(route, "first_byte_timeout", 0)
                        await req_cm.__aexit__(None, None, None)
                        await cm.__aexit__(None, None, None)
                        return None, route_info(route, "failed",
                                                error="first_byte_timeout")
                else:
                    line = await ait.__anext__()
                if not line.strip():
                    continue
                # 上游首帧即 [DONE]：立即结束且无任何内容 —— 跳出后按
                # empty_stream 处理换线。此前 Responses 分支的
                # parse_stream_event("[DONE]") 返回 {}（非 None）会误判为
                # 有效首帧，"秒回 [DONE]" 的坏线路反而赢下竞速成空 winner。
                if line.startswith("data:") and line[5:].strip() == "[DONE]":
                    break
                if is_resp:
                    if responses_api.parse_stream_event(line) is not None:
                        first_line = line
                        break
                    prelude.append(line)
                    continue
                elif is_valid_stream_chunk(line) is not None:
                    first_line = line
                    break
                prelude.append(line)
                # 非内容 data 行分两类，不能一律判死线路：
                # - 裸 `data: [DONE]`：上游立即结束且无内容 -> empty_stream（可换线重试）
                # - 空 delta 心跳 / 纯 role 标记：线路还在，继续等真实内容
                # - 真正的错误事件（error / invalid JSON）-> invalid_response
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        # 上游首行即结束、没有任何内容：跳出后按 empty_stream 处理，
                        # 让竞速换到其它线路，而不是把空响应当作胜者。
                        break
                    try:
                        parsed = json.loads(payload)
                    except Exception:  # noqa: BLE001
                        parsed = None
                    if parsed is None or (isinstance(parsed, dict) and parsed.get("error")):
                        await _mark_failure(route, "invalid_response", 200)
                        await req_cm.__aexit__(None, None, None)
                        await cm.__aexit__(None, None, None)
                        return None, route_info(route, "failed", error="invalid_response",
                                                http_status=200)
                    # 其余（心跳/usage 预告等）已入 prelude，继续读取下一行
        except StopAsyncIteration:
            pass
        if first_line is None:
            await _mark_failure(route, "empty_stream", 200)
            await req_cm.__aexit__(None, None, None)
            await cm.__aexit__(None, None, None)
            return None, route_info(route, "failed", error="empty_stream",
                                    http_status=200)
        await _mark_success(route)
        return (cm, req_cm, resp, ait, first_line, prelude), None
    except asyncio.CancelledError:
        await cm.__aexit__(None, None, None)
        raise
    except Exception as exc:  # noqa: BLE001
        typ, _ = _classify_error(exc)
        await _mark_failure(route, typ, 0)
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
                # 与 _race 同级防护：任务自身可能因取消/未捕获异常结束，
                # 裸 t.result() 会把异常/取消直接抛出并中断整条竞速循环。
                try:
                    res, fail_info = t.result()
                except asyncio.CancelledError:
                    report.append(route_info(
                        tasks[t], "cancelled", (_time.monotonic() - t0) * 1000,
                        "task cancelled"))
                    n_failed += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    report.append(route_info(
                        tasks[t], "failed", (_time.monotonic() - t0) * 1000,
                        f"task_error:{type(exc).__name__}"))
                    n_failed += 1
                    continue
                if res is not None:
                    winner_route = tasks[t]
                    latency = (_time.monotonic() - t0) * 1000
                    report.append(route_info(winner_route, "winner", latency))
                    # 同批次内其它"也已拿到首个有效块"的线路（done 集合里并列完成的
                    # 胜者候选）连接仍开着，必须在这里关闭——否则每次竞速都会泄漏
                    # 数个 fd（多线路几乎同时出流很常见），高并发下累积成 fd 耗尽。
                    for other_t in done:
                        if other_t is t:
                            continue
                        try:
                            ores, _oinfo = other_t.result()
                        except Exception:  # noqa: BLE001
                            continue
                        if ores is None:
                            continue
                        _ocm, oreq, _oresp, _oait, _ofirst, _oprelude = ores
                        try:
                            await oreq.__aexit__(None, None, None)
                            await _ocm.__aexit__(None, None, None)
                        except Exception:  # noqa: BLE001
                            pass
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for p in pending:
                        report.append(route_info(
                            tasks[p], "cancelled",
                            (_time.monotonic() - t0) * 1000, "winner decided"))
                    cm, req_cm, resp, ait, first_line, prelude = res
                    return (winner_route, cm, req_cm, resp, ait, first_line,
                            prelude, report)
                if fail_info:
                    report.append(fail_info)
                n_failed += 1
    finally:
        # 兜底取消：客户端在竞速途中断开时，GeneratorExit/CancelledError 会沿
        # asyncio.wait 抛出，若无此段，所有未完成线路任务（各持一个已建连的
        # httpx.AsyncClient + 代理连接/fd）将悬挂至 first_byte_timeout 甚至更久，
        # 长期累积即 fd 耗尽。与 _race 的 finally 兜底保持同级防护。
        leftover = [t for t in tasks if not t.done()]
        for t in leftover:
            t.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)
    failures = [r for r in report if r.get("status") == "failed"] or report
    raise AllRoutesFailed(
        [f"{f['name']}:{f['error']}" for f in failures], report=report)


async def iter_sse(first_line: str, aiter, include_first: bool = True,
                   state: dict | None = None) -> AsyncIterator[str]:
    """Yield SSE lines verbatim（含上游自己的 [DONE]，如有）。

    逐行透传、零延迟、零重排（与 one-api/new-api 的逐行解析同口径：
    OpenAI 系上游一行即一事件，输出逐字节等价于上游字节流）。与旧实现
    唯一差异：流结束时若存在未终止的残余行，冲刷保留，不静默丢弃。

    **不再伪造结尾 [DONE]**：上游流结束却未发 [DONE] 属于"静默截断"——
    继续伪造会把不完整响应伪装成正常完成，客户端（agent）把写了一半的
    文档当成功收货且毫无报错。截断真相通过 `state["saw_done"]` 上报给
    调用方，由其决定重试或向客户端报错。
    """
    if include_first:
        yield first_line + "\n\n"
    saw_done = False
    async for line in aiter:
        if not line.strip():
            continue
        # 终结帧兼容两种空格形态（"data: [DONE]" / "data:[DONE]"），
        # 与 StreamTap 的帧级观察口径一致
        if line.startswith("data:") and line[5:].strip() == "[DONE]":
            saw_done = True
        yield line + "\n\n"
    if state is not None:
        state["saw_done"] = saw_done


@dataclass
class StreamWinner:
    route: Route
    cm: Any
    req_cm: Any
    aiter: Any
    first_line: str
    report: list[dict] = None  # type: ignore[assignment]
    # lines() 消费完毕后写入：上游是否真正发出了 [DONE]。
    # False = 上游静默断流（无 [DONE]），调用方须按截断处理而非成功。
    final_state: dict = None  # type: ignore[assignment]
    # 竞速 prelude：判胜首帧之前上游已发出的行（usage 预告帧、心跳注释、
    # 自定义事件）。lines() 开头按原序重放——零丢失原则。
    prelude: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.final_state is None:
            self.final_state = {}
        if self.prelude is None:
            self.prelude = []

    async def _replay_prelude(self) -> AsyncIterator[str]:
        """重放竞速窗口期消费掉的 prelude 行。"""
        for line in self.prelude:
            yield line + "\n\n"

    async def lines(self) -> AsyncIterator[str]:
        # 兼容规整器总开关（渠道隔离，SystemSetting 配置，默认开启）：
        # - "on"（默认）：解密器 + 工具流规整器挂载。两者对行为良好的上游
        #   是零改动快速路径（无 gAAAA 密文 / 无畸形 tool_calls 时逐字节
        #   透传），只对"协议破损"的方言（muse/zen 换 id、整段重述、加密
        #   思考）出手——即 new-api 的哲学：同协议好上游字节级透传，
        #   破损上游重建流。
        # - "off"：纯转发模式，上游字节原样直达客户端（自担方言风险）。
        compat = str(sysconfig.get(
            "stream_compat_normalizers", self.route.key.channel) or "on"
        ).strip().lower() != "off"
        # 工具调用流规整：放置于解密/协议转换之后、下发客户端之前——
        # 上游（muse/zen 等）可能发乱序 tool_calls（换 id + 全参重复），
        # [OI] SDK 按 index 累加参数会得到非法 JSON。
        tc_norm = ToolCallStreamNormalizer()
        if responses_api.is_responses_url(_route_url(self.route)):
            if not compat:
                async for chunk in self._replay_prelude():
                    yield chunk
                async for chunk in responses_api.iter_responses_sse(
                        self.first_line, self.aiter, done_state=self.final_state):
                    yield chunk
                return
            async for chunk in responses_api.iter_responses_sse(
                    self.first_line, self.aiter, done_state=self.final_state,
                    prelude=self.prelude):
                # Responses 链路的 reasoning 已在 iter_responses_sse 内由 _reasoning_text 解密
                # 额外的 SSE chunk 解密（处理 Kilo/OpenRouter 加密 reasoning）
                for out in tc_norm.feed(decrypt_sse_chunk(chunk)):
                    yield out
        else:
            if not compat:
                async for chunk in self._replay_prelude():
                    yield chunk
                async for chunk in iter_sse(self.first_line, self.aiter,
                                            state=self.final_state):
                    yield chunk
                return
            decryptor = StreamReasoningDecryptor()
            try:
                async for chunk in self._replay_prelude():
                    for out in decryptor.feed(chunk):
                        for fixed in tc_norm.feed(out):
                            yield fixed
                async for chunk in iter_sse(self.first_line, self.aiter,
                                            state=self.final_state):
                    # 有状态解密：跨 chunk 分片的 Fernet token 缓冲凑齐后一次解密；
                    # 解不开的密文原样透传（零丢失）。
                    for out in decryptor.feed(chunk):
                        for fixed in tc_norm.feed(out):
                            yield fixed
            finally:
                # 异常收尾（上游暴毙）也冲刷残留缓冲。客户端断开时
                # GeneratorExit 已进入本生成器，任何 yield 都会触发
                # RuntimeError——静默跳过即可（客户端已不在，冲刷无处投递）。
                try:
                    for out in decryptor.finalize():
                        for fixed in tc_norm.feed(out):
                            yield fixed
                except RuntimeError:
                    pass  # GeneratorExit 期间不可再 yield

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
    route, cm, req_cm, resp, ait, first_line, prelude, report = \
        await race_stream_winner(routes, body)
    return StreamWinner(route=route, cm=cm, req_cm=req_cm, aiter=ait,
                        first_line=first_line, report=report, prelude=prelude)