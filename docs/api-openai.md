# OpenAI 兼容 API（`/v1/*`）

## 认证

`Authorization: Bearer sk-nvidia2api-xxxxxxxx`

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
