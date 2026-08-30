# 管理 API（`/api/admin/*`）

## 认证

```
POST /api/admin/login
{"username": "admin", "password": "admin123"}
→ {"token": "<ADMIN_TOKEN>"}

之后所有请求带：
Authorization: Token <token>
```

用户名密码与 token 都来自 `.env`（`ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_TOKEN`）。

## 渠道作用域

除「渠道」与「用户 API Key」两组接口外，其余资源接口（Keys、代理、分组、模型、
日志、仪表盘、设置、对话测试）都**按渠道隔离**，通过以下方式选择渠道：

```
X-Channel: zen            # 请求头，推荐
GET /api/admin/keys?channel=zen
```

解析不到时回落到平台默认渠道。详见 [channels.md](channels.md)。

统一错误格式（与 OpenAI 一致）：

```json
{"error": {"message": "…", "type": "api_error", "code": "…"}}
```

## 仪表盘

| 端点 | 说明 |
|---|---|
| `GET /api/admin/dashboard` | keys/proxies/models/请求数/成功率/延迟汇总 + key_status、proxy_status 分布 |
| `GET /api/admin/dashboard/usage?days=7` | 按天的 token 明细（prompt/completion/total + 请求数） |
| `GET/PATCH/DELETE /api/admin/settings` | 运行参数（按渠道）。GET 返回 `{channel, settings:[{key,type,value,default,description,overridden}]}`；PATCH body `{settings:{key:value,...}}`；DELETE 恢复默认（可带 `?keys=a,b`） |

## 渠道

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/channels` | `{results:[...], current}`；含各渠道 Key/代理/模型计数 |
| POST | `/api/admin/channels` | 新建 `{name, slug, base_url, chat_path, models_path, auth_scheme, default_rpm, enabled, is_default, notes}`，`base_url` 可直接粘完整端点 |
| PATCH | `/api/admin/channels/{id}` | 编辑；`is_default:true` 自动取消其他渠道的默认 |
| DELETE | `/api/admin/channels/{id}` | 删除（至少保留一个；级联删除其下资源） |
| POST | `/api/admin/channels/{id}/test` | 用该渠道第一个 Key 打一次 `/models` |

## 渠道 Keys

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/keys` | 列表（默认脱敏） |
| POST | `/api/admin/keys` | 新建 `{name, api_key, rpm_limit}` |
| POST | `/api/admin/keys/import` | 批量导入 `{text}`，`name---key` 或每行一个 key |
| GET | `/api/admin/keys/{id}?reveal=1` | 查看明文 Key |
| PATCH | `/api/admin/keys/{id}` | `{name, rpm_limit, enabled}` |
| DELETE | `/api/admin/keys/{id}` | 删除 |
| POST | `/api/admin/keys/{id}/test` | 检测（走该渠道的 models 端点） |

import 返回 `{success, duplicate, invalid, failed, errors[]}`。
旧路径 `/api/admin/nvidia-keys/*` 保留为别名。

## 代理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/proxies` | 列表 + summary（channel、nvidia_keys、max_enabled_proxies、enabled、total_routes） |
| POST | `/api/admin/proxies` / `import` | 新增 / 批量导入（格式同 Key，`socks5://user:pass@host:port`） |
| PATCH | `/api/admin/proxies/{id}` | 改字段；`{"enabled":true}` 超过「该渠道 Key 数 − 1」上限时返回 400 `proxy_limit_exceeded` |
| POST | `/api/admin/proxies/{id}/test` | 单条测速（连接+延迟+IP） |
| POST | `/api/admin/proxies/{id}/fetch-ip` | 同上（别名） |
| POST | `/api/admin/proxies/test-all` | 并发全部测速（Semaphore 20） |
| DELETE | `/api/admin/proxies/{id}` | 删除 |

## 分组

`GET/POST /api/admin/proxy-groups`、`PATCH/DELETE /api/admin/proxy-groups/{id}`。删除分组会把组内代理置为无分组。

## 模型

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/models?q=` | 列表 |
| POST | `/api/admin/models` | 新建 |
| PATCH | `/api/admin/models/{id}` | display_name/description/enabled |
| DELETE | `/api/admin/models/{id}` | 删除 |
| POST | `/api/admin/models/sync` | 从当前渠道的 models 端点拉取并 upsert |

## 用户 API Key（跨渠道共享）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/api-keys` | 列表（不含 hash） |
| POST | `/api/admin/api-keys` | 创建 `{name, rate_limit}`（0=不限），**响应含完整 Key 一次** |
| PATCH | `/api/admin/api-keys/{id}` | `{enabled, rate_limit, name}` |
| DELETE | `/api/admin/api-keys/{id}` | 删除 |

## 日志

```
GET /api/admin/logs?model=&status=success|error
```

返回最多 200 条（仅当前渠道），字段含 `first_token_ms`、`prompt/completion/total/cached_tokens`、`routes[]`、`winner_*`。

## 对话测试

```
POST /api/admin/chat
{"model":"m","messages":[...],"stream":true|false,"channel":"zen"（可选）}
```

非流式返回 `{request_id, payload, meta:{routes, duration_ms, usage,…}}`。
流式返回 SSE：首包 `{"meta":{…}}` → 上游 chunk → 结束 `{"summary":{duration_ms,first_token_ms,prompt/completion/total/cached_tokens}}`。
