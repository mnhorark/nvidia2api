# OpenAI 兼容 API

## 认证

`Authorization: Bearer sk-nvidia2api-xxxxxxxx`

## 渠道路由

平台对外是一套接口，背后可接多个上游渠道：

| 路径 | 目标 |
|---|---|
| `/v1/models`、`/v1/chat/completions` | 平台**默认**渠道 |
| `/c/<slug>/v1/models`、`/c/<slug>/v1/chat/completions` | **指定**渠道，如 `/c/zen/v1/chat/completions` |
| `/v1/responses` | OpenAI Responses API |
| `/v1/messages`、`/v1/messages/count_tokens` | Anthropic Messages API |

也可以在请求体里带 `"channel": "zen"`（URL 前缀优先级更高）。
模型必须存在于目标渠道且 `enabled=true`，否则 404。

渠道管理见 [channels.md](channels.md)。

## 端点

### `GET /v1/models`

```json
{
  "object": "list",
  "data": [
    {"id": "meta/llama-3.3-70b-instruct", "object": "model", "created": 0, "owned_by": "nvidia"}
  ]
}
```

只包含 `enabled=true` 的模型。

### `POST /v1/chat/completions`

支持的参数（透传 NVIDIA；未列出的会被丢弃）：`model`、`messages`、`temperature`、`top_p`、`max_tokens`、`stream`、`stop`、`n`、`seed`、`frequency_penalty`、`presence_penalty`、`response_format`、`tools`、`tool_choice`。

- 非流式：直接返回 NVIDIA 原始 JSON
- 流式：SSE，上游 chunk 透传，结尾 `data: [DONE]`

### `POST /v1/responses`

OpenAI Responses API 协议。请求体（`input`/`max_output_tokens`/`reasoning`/`tools`/`text.format` 等）转成内部 chat 后复用整套链路，出口再转回 Responses 响应 / SSE 事件流。非流式返回 `response` 对象，流式返回 `response.output_text.delta` 一类事件。

### `POST /v1/messages`

Anthropic Messages 协议（Claude Code / Cline 等客户端可直接接入）：

```json
{
  "model": "meta/llama-3.3-70b-instruct",
  "max_tokens": 1024,
  "system": "你是助手",
  "messages": [{"role": "user", "content": "你好"}]
}
```

- 支持 `system`、`messages`（text / image / tool_use / tool_result 块）、`tools`、`tool_choice`、`thinking`、`stop_sequences`、`max_tokens`；
- `thinking.type=enabled` 自动映射为平台思考参数（`reasoning_effort` / `reasoning_budget`），走 `services/thinking.py` 的模型族兼容层；
- 非流式返回 Anthropic Message 对象（`content` 块 + `stop_reason` + `usage.input_tokens/output_tokens`）；
- `stream=true` 返回 Anthropic SSE 事件流（`message_start` / `content_block_*` / `message_delta` / `message_stop`），推理增量映射为 `thinking_delta`，工具调用映射为 `input_json_delta`。

### `POST /v1/messages/count_tokens`

估算 Anthropic 请求的 `input_tokens`（启发式估算，非官方 tokenizer）。

## 思考强度（thinking / reasoning）

思考类参数**不走通用透传**，而是由 `services/thinking.py` 归一化后下发——NVIDIA 不同模型接受的字段不同，客户端写法也各异。

可识别的入参（顶层或 `extra_body` 内均可）：

| 入参 | 说明 |
|---|---|
| `reasoning_effort` | `none` / `low` / `medium` / `high` / `max`，兼容 `minimal`、`balanced`、`xhigh`、`ultra` 等别名，也接受数字档位 |
| `reasoning_budget` / `thinking_budget` | 思考 token 预算 |
| `thinking` / `enable_thinking` | 布尔开关 |
| `chat_template_kwargs` | NVIDIA 原生容器，已知键归一化，其余键原样透传 |
| `clear_thinking` | 是否在输出中保留思考过程 |

归一化后的上游形态：

```json
{
  "chat_template_kwargs": {"thinking": true, "enable_thinking": true},
  "reasoning_effort": "high",
  "reasoning_budget": 16384
}
```

`chat_template_kwargs` 同时写 `thinking` 与 `enable_thinking`：DeepSeek / Nemotron 系认前者，Qwen / GLM 系认后者。
`reasoning_effort=none|off` 视为显式关闭思考。

```python
r = client.chat.completions.create(
    model="deepseek-ai/deepseek-r1",
    messages=[{"role": "user", "content": "9.11 和 9.8 哪个大"}],
    extra_body={"reasoning_effort": "high"},
)
```

### 开关与剥离名单

`services/sysconfig.py` 中两个运行时参数（后台 Settings 页可改，即时生效）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `thinking_passthrough` | `true` | 关闭后丢弃全部思考参数 |
| `thinking_strip_models` | 空 | 模型名子串黑名单，英文逗号分隔。命中时剥离思考参数，用于规避不支持这些字段、会返回 400 的模型 |

## 错误格式

与 OpenAI 完全一致：

```json
{"error": {"message": "…", "type": "api_error", "param": null, "code": "invalid_request"}}
```

| HTTP | code |
|---|---|
| 400 | `invalid_request` |
| 401 | `invalid_api_key` |
| 402 | `insufficient_quota`（用户 Key 额度耗尽） |
| 403 | `key_disabled` |
| 404 | `model_not_found` |
| 429 | `rate_limit_exceeded` / `server_overloaded` |
| 502 | `upstream_error` |
| 503 | `no_available_route` |

## 调用示例

```python
from openai import OpenAI
client = OpenAI(api_key="sk-nvidia2api-...", base_url="http://localhost:8000/v1")

r = client.chat.completions.create(
    model="meta/llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in r:
    delta = chunk.choices[0].delta
    if delta.reasoning_content:
        print("[思考]", delta.reasoning_content)
    if delta.content:
        print(delta.content, end="")
```

## Token 额度（quota）

用户 API Key 可设置 Token 额度（后台 API Keys 页创建/调整，`quota=0` 表示不限）：

- 每次成功请求后按「输入 + 输出 token」累计到 `used_quota`；缓存读取的输入 token 不占额度；
- 额度耗尽后新请求返回 `402 insufficient_quota`，直到管理员调高额度；
- 前端显示 `used / quota`，耗尽后标红。

## 指标

每条请求都写入 RequestLog：

- `first_token_ms`（首 chunk 到达时间；只有流式）
- `duration_ms`（总耗时，流式里 [DONE] 时结算）
- `prompt/completion/total/cached_tokens`（流式会自动让上游带 `stream_options.include_usage`，非流式从 usage 字段取）
- `routes[]`（每条线路的 winner/failed/cancelled 明细）

## 可观测性

| 端点 | 说明 |
|---|---|
| `GET /healthz` | 存活探针（无需鉴权），数据库可达返回 200 |
| `GET /api/admin/health` | 全量健康信息（鉴权）：资源计数、渠道状态、Key/Proxy 状态分布 |
| `GET /metrics` | Prometheus 文本格式指标（只读）：请求量、token 用量、各资源状态计数 |

## 保护

全局并发上限 `MAX_CONCURRENT_REQUESTS`（线程 semaphore）。

**渠道级自动熔断**：渠道连续"系统级失败"（竞速全挂 / 5xx / 线路不可用）达到阈值后自动进入冷却，期间该渠道的 Key 不再参与线路构建，请求自动转向其他渠道；任意一次成功立即复位。阈值与冷却时长走系统参数 `channel_cooldown_failures`（默认 5 次）、`channel_cooldown_seconds`（默认 120 秒）。单 Key 的 401/403/429 属于 Key 级问题，不触发渠道熔断。
