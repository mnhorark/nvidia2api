"""Admin API：所有资源按渠道隔离，渠道由 `X-Channel` 头或 `?channel=` 决定。

本文件由原单文件 admin_views.py 按资源拆分（new-api 按资源分 handler 的理念），
模块划分见包内各文件；`__init__.py` 聚合导出保持 `from . import admin_views`
兼容（urls.py 与测试无需改动）。
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger("nvidia2api.admin")

from apps.core.models import (
    AIModel, Channel, ChannelKey, ChannelKeyStatus, Proxy, ProxyGroup, ProxyStatus,
    RequestLog, SystemSetting, UserApiKey,
)
from services import (
    api_key_service, channel_service, key_service, model_registry, proxy_service,
    thinking, upstream_service,
)
from services.proxy_checker import check_all, check_proxy

from ..auth import AdminRequiredMixin
from .common import *  # noqa: F401,F403  （辅助函数：_parse_int/_slugify 等）
from ..serializers import (
    ChannelKeySerializer, ChannelSerializer, ModelSerializer, ProxyGroupSerializer,
    ProxySerializer, ProxyWriteSerializer, RequestLogSerializer, SettingSerializer,
    UserApiKeySerializer,
)
class AdminChatView(AdminRequiredMixin, APIView):
    """Playground: run a real chat completion through the race engine."""
    ALLOWED = {'model', 'messages', 'temperature', 'top_p', 'max_tokens', 'frequency_penalty', 'presence_penalty', 'stream'}

    def post(self, request):
        from services.load_balancer import build_routes
        from services.race_engine import AllRoutesFailed, NoRouteAvailable, race_chat
        from services import key_service as ks
        channel_param = (request.data.get('channel') or '').strip()
        channel = channel_service.resolve(channel_param) if channel_param else current_channel(request)
        model = (request.data.get('model') or '').strip()
        prompt = request.data.get('prompt')
        messages = request.data.get('messages')
        if prompt and (not messages):
            messages = [{'role': 'user', 'content': str(prompt)}]
        if not model or not messages:
            return Response({'error': {'message': 'model and prompt/messages required', 'code': 'bad_request'}}, status=400)
        model_rec = channel.models.filter(model_name=model).first()
        if not model_rec or not model_rec.enabled:
            return Response({'error': {'message': f'模型 {model} 不存在或未启用', 'code': 'model_not_found'}}, status=404)
        body = {k: v for k, v in request.data.items() if k in self.ALLOWED and k not in thinking.THINKING_PARAM_KEYS and (v is not None)}
        body['model'] = model
        body['messages'] = messages
        body.update(thinking.build_upstream(request.data, model, channel))
        client_thinking = {k: request.data.get(k) for k in thinking.THINKING_PARAM_KEYS if k in request.data and request.data.get(k) is not None}
        upstream_thinking = thinking.build_upstream(request.data, model, channel)
        proxy_group = model_rec.proxy_group_id if model_rec else None
        if request.data.get('stream'):
            return self._stream(body, model, channel, proxy_group=proxy_group, endpoint=model_rec.endpoint, client_thinking=client_thinking, upstream_thinking=upstream_thinking)
        routes = build_routes(channel, proxy_group=proxy_group, endpoint=model_rec.endpoint)
        started = timezone.now().timestamp()
        request_id = ks.new_request_id()
        log = RequestLog.objects.create(channel=channel, request_id=request_id, model=model, routes_count=len(routes), client_thinking=client_thinking, upstream_thinking=upstream_thinking)
        if not routes:
            log.status, log.http_status, log.error_type = ('failed', 503, 'no_available_route')
            log.save()
            return Response({'error': {'message': '当前没有可用线路（没有可用的渠道 Key）', 'code': 'no_available_route'}}, status=503)
        import time
        t0 = time.monotonic()
        try:
            result = race_chat(routes, body)
        except AllRoutesFailed as exc:
            log.status, log.error_type = ('failed', 'all_routes_failed')
            log.http_status = 502
            log.routes = exc.report
            log.save()
            return Response({'error': {'message': f'所有线路均失败: {exc}', 'code': 'upstream_error'}, 'routes': exc.report}, status=502)
        except NoRouteAvailable:
            log.status, log.error_type = ('failed', 'no_available_route')
            log.http_status = 503
            log.save()
            return Response({'error': {'message': '当前没有可用线路', 'code': 'no_available_route'}}, status=503)
        duration = round((time.monotonic() - t0) * 1000, 1)
        r = result.route
        usage = (result.payload or {}).get('usage') or {}
        log.status, log.http_status = ('success', 200)
        log.duration_ms = duration
        log.winner_route_type = r.kind
        log.winner_key_name = r.key.name
        log.winner_proxy_name = r.proxy.name if r.proxy else ''
        log.proxy_public_ip = r.proxy.public_ip if r.proxy else ''
        log.prompt_tokens = usage.get('prompt_tokens', 0) or 0
        log.completion_tokens = usage.get('completion_tokens', 0) or 0
        log.total_tokens = usage.get('total_tokens', 0) or 0
        log.cached_tokens = (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0) or 0
        log.routes = result.report or []
        log.save()
        return Response({'request_id': request_id, 'channel': channel.slug, 'payload': result.payload, 'meta': {'route_type': r.kind, 'key_name': r.key.name, 'proxy_name': r.proxy.name if r.proxy else '', 'duration_ms': duration, 'usage': usage, 'routes': result.report or []}})

    def _stream(self, body, model, channel, proxy_group=None, endpoint=None, client_thinking=None, upstream_thinking=None):
        """SSE（异步生成器）: 竞速胜出后逐块转发上游 SSE，实现真正 token-by-token 流式。

        必须是 async 生成器：Django 对**同步**流式内容会用
        `sync_to_async(list(...))` 一次性消费完整个生成器才下发，导致"假流式"——
        思考 token 与正文全部攒到请求结束才一次性吐出。async 生成器则被 ASGI
        逐块下发，思考（reasoning_content）与正文 token 边到边实时转发。

        心跳与掐线（参考 new-api / sub-api / cliproxy 思路）：
        - stream_heartbeat_interval：上游静默时向客户端发 `: keep-alive` 心跳，
          防 NAT/负载均衡/客户端把连接误判为死，链路保活；
        - stream_idle_timeout：胜出后未产出真实内容的静默判死上限——
          连续 N 个探测周期无任何数据（含思考 token），判定线路死亡；
          思考模型会持续吐 reasoning token，正常"正在思考"不会被掐断；
        - 已向客户端交付正文后断流：发 error 事件明确告知"输出不完整"
          （upstream_truncated/stream_truncated），绝不把半截回答伪装成
          干净完成——agent/用户必须知道内容被截断才能整体重试。
        """
        import asyncio
        import json
        from django.http import StreamingHttpResponse
        from services import key_service as ks
        from services import sysconfig
        from services.load_balancer import build_routes
        from services.race_engine import AllRoutesFailed, NoRouteAvailable, race_stream
        from services.stream_pipeline import StreamTap
        from ..openai_views import _drain
        routes = build_routes(channel, proxy_group=proxy_group, endpoint=endpoint)
        request_id = ks.new_request_id()
        log = RequestLog.objects.create(channel=channel, request_id=request_id, model=model, routes_count=len(routes), is_stream=True, client_thinking=client_thinking or {}, upstream_thinking=upstream_thinking or {})
        body = dict(body)
        body.setdefault('stream_options', {}).update({'include_usage': True})
        if not routes:
            log.status, log.http_status, log.error_type = ('failed', 503, 'no_available_route')
            log.save()
            return Response({'error': {'message': '当前没有可用线路', 'code': 'no_available_route'}}, status=503)
        import time as _time
        started = _time.monotonic()
        idle_timeout = float(sysconfig.get('stream_idle_timeout', channel) or 0)
        heartbeat = float(sysconfig.get('stream_heartbeat_interval', channel) or 0)
        max_duration = float(sysconfig.get('stream_max_duration', channel) or 0)
        # 内容出现后的静默判死（与 /v1 流式同参）：思考模型长停顿/掐流兜底
        content_idle_timeout = float(sysconfig.get('stream_content_idle_timeout', channel) or 0)

        async def gen():
            from services.loop_offload import run_db

            async def _safe_save():
                # 失败/收尾路径的落库绝不能抛：一旦逃逸会炸掉整个 ASGI 连接
                # （历史事故：run_db import 丢失导致 NameError → network error）。
                try:
                    await run_db(log.save)
                except Exception:
                    logger.exception("admin stream log save failed (req %s)", request_id)

            winner = None
            tap = StreamTap()  # 整条尝试共用一个观察器（race 失败时为空态）
            try:
                winner = await race_stream(routes, body)
                log.winner_route_type = winner.route.kind
                log.winner_key_name = winner.route.key.name
                log.winner_proxy_name = winner.route.proxy.name if winner.route.proxy else ''
                log.proxy_public_ip = winner.route.proxy.public_ip if winner.route.proxy else ''
                log.routes = winner.report or []
                # 关键：winner 出现时不标记 success，仅记录首字耗时；
                # 避免客户端断开导致 success 记录永久停留 0ms/0 token。
                log.first_token_ms = round((_time.monotonic() - started) * 1000, 1)
                log.duration_ms = log.first_token_ms
                log.http_status = 200
                try:
                    from services import tokenizer as _tok
                    log.prompt_tokens = _tok.estimate_messages_tokens(body.get('messages'))
                    log.total_tokens = log.prompt_tokens
                except Exception:
                    pass
                await _safe_save()
                duration = log.first_token_ms
                yield ('data: ' + json.dumps({'meta': {'request_id': request_id, 'channel': channel.slug, 'route_type': winner.route.kind, 'key_name': winner.route.key.name, 'proxy_name': winner.route.proxy.name if winner.route.proxy else '', 'first_chunk_ms': duration, 'routes': winner.report or []}}) + '\n\n')
                # 单点观察（与 /v1 流式同一 StreamTap 管线）：_drain 每 chunk
                # 解析一次，这里只读累积态
                stream_ok = False
                truncated_stream = False
                try:
                    async for chunk in _drain(winner, idle_timeout, heartbeat, max_duration,
                                              content_idle_timeout, tap=tap):
                        yield chunk
                    # 静默截断检测（与 /v1 流式同语义）：流结束但既无 finish_reason
                    # 也无上游 [DONE] = 上游掐断，必须如实上报而非伪装成功。
                    # saw_done 双源取或（行级 final_state 与帧级 tap 观察口径
                    # 互补，方言变体不应触发假截断）
                    upstream_done = bool(
                        (getattr(winner, 'final_state', None) or {}).get('saw_done')
                        or tap.saw_done)
                    stream_ok = bool(tap.finish_reason or upstream_done)
                    truncated_stream = not stream_ok
                finally:
                    usage = dict(tap.usage)
                    if truncated_stream:
                        log.status = "failed"
                        log.error_type = "upstream_truncated"
                    else:
                        log.status = "success"
                    total_ms = round((_time.monotonic() - started) * 1000, 1)
                    log.duration_ms = total_ms
                    if usage.get('prompt_tokens'):
                        log.prompt_tokens = usage['prompt_tokens']
                    elif log.prompt_tokens:
                        pass
                    else:
                        from services import tokenizer
                        log.prompt_tokens = tokenizer.estimate_messages_tokens(body.get('messages'))
                    if usage.get('completion_tokens'):
                        log.completion_tokens = usage['completion_tokens']
                    elif tap.completion_text.strip():
                        from services import tokenizer
                        log.completion_tokens = tokenizer.estimate_tokens(tap.completion_text)
                    log.total_tokens = (log.prompt_tokens or 0) + (log.completion_tokens or 0)
                    details = usage.get('prompt_tokens_details') or {}
                    log.cached_tokens = details.get('cached_tokens', 0) or 0
                    await _safe_save()
                    if stream_ok:
                        yield ('data: ' + json.dumps({'summary': {'duration_ms': total_ms, 'first_token_ms': log.first_token_ms or duration, 'prompt_tokens': log.prompt_tokens, 'completion_tokens': log.completion_tokens, 'total_tokens': log.total_tokens, 'cached_tokens': log.cached_tokens}}) + '\n\n')
                        yield 'data: [DONE]\n\n'
                    elif truncated_stream:
                        # 静默截断：显式告知前端"输出不完整"（前端已支持 error 事件），
                        # 不再把半截回答伪装成正常完成
                        yield ('data: ' + json.dumps({'error': {'message': '上游输出中断：流被提前关闭（未收到 finish_reason/[DONE]），已收到的内容不完整', 'type': 'api_error', 'param': None, 'code': 'upstream_truncated'}}) + '\n\n')
                        yield 'data: [DONE]\n\n'
            except (NoRouteAvailable, AllRoutesFailed) as exc:
                log.status, log.http_status, log.error_type = ('failed', 502, 'all_routes_failed')
                if isinstance(exc, AllRoutesFailed):
                    log.routes = exc.report
                await _safe_save()
                yield ('data: ' + json.dumps({'error': {'message': f'所有线路均失败: {exc}', 'type': 'api_error', 'param': None, 'code': 'upstream_error'}}) + '\n\n')
                yield 'data: [DONE]\n\n'
            except Exception as exc:
                if tap.sent_content or tap.saw_done:
                    log.duration_ms = round((_time.monotonic() - started) * 1000, 1)
                    await _safe_save()
                    if not tap.saw_done:
                        yield 'data: [DONE]\n\n'
                    return
                is_stall = isinstance(exc, TimeoutError)
                log.status, log.http_status = ('failed', 504 if is_stall else 502)
                log.error_type = 'stream_idle_timeout' if is_stall else 'stream_error'
                log.duration_ms = round((_time.monotonic() - started) * 1000, 1)
                await _safe_save()
                if is_stall:
                    msg = f'上游连续无响应（{round(idle_timeout or 0, 1)} 秒未收到任何数据），已判定线路死亡。可调大 stream_idle_timeout'
                else:
                    msg = f'stream error: {exc}'
                yield ('data: ' + json.dumps({'error': {'message': msg, 'type': 'api_error', 'param': None, 'code': 'stream_error'}}) + '\n\n')
                yield 'data: [DONE]\n\n'
            finally:
                if winner is not None:
                    try:
                        await winner.close()
                    except Exception:
                        pass
        response = StreamingHttpResponse(gen(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
