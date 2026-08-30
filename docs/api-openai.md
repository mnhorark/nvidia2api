# OpenAI 兼容 API

## 认证

`Authorization: Bearer sk-nvidia2api-xxxxxxxx`

## 渠道路由

平台对外是一套接口，背后可接多个上游渠道：

| 路径 | 目标 |
|---|---|
| `/v1/models`、`/v1/chat/completions` | 平台**默认**渠道 |
| `/c/<slug>/v1/models`、`/c/<slug>/v1/chat/completions` | **指定**渠道，如 `/c/zen/v1/chat/completions` |

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

## 指标

每条请求都写入 RequestLog：

- `first_token_ms`（首 chunk 到达时间；只有流式）
- `duration_ms`（总耗时，流式里 [DONE] 时结算）
- `prompt/completion/total/cached_tokens`（流式会自动让上游带 `stream_options.include_usage`，非流式从 usage 字段取）
- `routes[]`（每条线路的 winner/failed/cancelled 明细）

## 保护

全局并发上限 `MAX_CONCURRENT_REQUESTS`（线程 semaphore）。
