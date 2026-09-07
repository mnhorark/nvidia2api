# 竞速引擎（services/race_engine.py）

不是"依次重试"，是真正的并行。

## 线路构建

```
N = min(启用代理数 + 1, 可用 Key 数, max_routes_per_request)
route[i]   = proxy[i]  + key[i]      (i < N-1)
route[N-1] = 直连      + key[N-1]
```

每个 Key 通过 `claim_rpm_slot` 原子地领到窗口配额后才进线路，一次请求内没有 Key 复用。

## 有效响应判定

非流式 `is_valid_response`：
- HTTP 200
- JSON 里有非空 `choices`
- 首 choice 的 `message`/`delta`/`text` 至少一个有内容
- 不能有 `error` 字段

流式首 chunk 判定 `is_valid_stream_chunk`：
- 以 `data:` 开头
- JSON 可解析、非 `[DONE]`
- 有 `choices`、无 `error`

**HTTP 200 本身不构成成功**：上游 200 但 body 是错误对象、或流为空，都被视为失败（`invalid_response` / `empty_stream`）。

## 竞速循环（非流式）

```python
pending = {task(route_i): route_i}
while pending:
    done, pending = await wait(pending, return_when=FIRST_COMPLETED)
    for t in done:
        r = t.result()
        if r.ok:
            cancel(*pending); report.append(winner); return r
        report.append(failed(r.error_type))
raise AllRoutesFailed(errors, report)
```

时间维度独立计时（每线路自己的 latency_ms），报告里保存每条线路的 winner/failed/cancelled 结果，最终写入 `RequestLog.routes`。

## 流式竞速

```
route_i ── open POST stream ──► 等第一个有效 SSE chunk
                                     │
首选有效 chunk ──────────────────────┤ Winner
                                     ▼
                              其余线路 cancel + 释放 httpx Client
```

`race_stream_winner` 返回 `StreamWinner(route, cm, req_cm, ait, first_line, report)`，调用方随后将首个 chunk 与后续 `aiter_lines()` 转发出去。

只在第一条 `data:` 有效时才算 Winner；第一条 `data:` 无效直接判 `invalid_response`，不等完整流。

## 故障分级

| 现象 | error_type | 处置 |
|---|---|---|
| HTTP 401/403 | invalid_key / forbidden | Key → `invalid` |
| HTTP 429 | rate_limited | Key → `rate_limited` + 冷却 60s |
| HTTP 404 | model_not_found | 不再尝试其他线路由用户换模型 |
| HTTP 5xx | upstream_server_error | 冷却 60s |
| 代理 connect/timeout | connect_error / timeout / network_error | 代理计数失败；连续 3 次标记 unhealthy + 冷却 |
| 200 但无 choices | invalid_response | 30s 冷却 |

代理与 Key 的失败互不影响：单个 Key 失效不会抑制代理，反之亦然。

## 截断与换线重试闸门

上游流结束但**既没有 `finish_reason` 也没有 `[DONE]`** = 静默截断。网关绝不伪造
`[DONE]` 把它伪装成成功。此时要不要换线重跑，取决于一个判据：
**有没有已经下发给客户端、重跑就会重复交付的字节**
（`StreamTap.delivered_to_client`）。

| 已交付的内容 | 能否换线重跑 | 原因 |
|---|---|---|
| 无（连 role 帧都没有 / 只有 role 帧） | ✅ 可以 | 客户端什么都没收到 |
| 正文 / 工具调用 / usage / finish / 流内 error | ❌ 不可以 | 重发即重复交付 |
| **思考增量 `reasoning_content`** | ❌ **不可以** | 同上（2026-09 修正） |

第三条是本轮修的。旧闸门只认正文，其成立前提是"思考不作为载体转发给客户端"——
这个前提随思考成为一等公民载体而失效：`_drain` 对每个 chunk 都实时 `yield`，
reasoning 同样送到了客户端。

线上后果（`moonshotai/kimi-k3` + `reasoning_effort=max` + ~90K prompt）：思考流已
逐块下发约 **300 秒**后被上游掐断，旧闸门判定"一个字都没交付"→ 丢弃这 300 秒、
换线从头重跑 → 客户端再收一遍思考流 → 最终 502。实测占 `upstream_truncated` 的
**7/10（其中 6 条 kimi-k3）**。

守卫：`R12_SilentTruncationTests.test_reasoning_only_truncation_must_not_retry_on_new_route`
（思考已交付 → `race_stream` 只调用一次）与
`test_silent_stream_still_eligible_for_route_switch`（真空流 → 仍调用两次），
两条互为反向锁，防止把合法重试一起关掉。

### 为什么日志里要记交付量

截断的两种形态在旧日志里**完全同形**——`completion_tokens` 都是 0（usage 帧永远在
截断之后才到），但处置**相反**。所以 `RequestLog` 记了三个观测列（迁移 0026）：

```
stream_chunks / content_chars / reasoning_chars     null = 非流式或历史行
```

有了它们才能回答"这条流在被掐之前到底有没有在动"，而不是靠猜。

## 取消语义

```
对 pending 任务 cancel() → gather(return_exceptions=True)
httpx.AsyncClient 在 __aexit__ 释放连接池
Django 侧 StreamingHttpResponse 在 finally 里 winner.close()
```

不会残留后台 task 或-connection。
