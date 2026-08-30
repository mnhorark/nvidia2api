# 多渠道（Channel）

## 概念

一个**渠道** = 一个上游 OpenAI 兼容端点 + 一套完全独立的配置。

| 资源 | 是否按渠道隔离 |
|---|---|
| 渠道 Keys | ✅ |
| 代理池 / 代理分组 | ✅ |
| 模型 | ✅ |
| 请求日志 | ✅ |
| 运行参数（设置页） | ✅ |
| 平台用户 API Key（`sk-nvidia2api-*`） | ❌ 跨渠道共享 |

## 数据模型

```
Channel 1───*  ChannelKey        (原 NvidiaApiKey，重命名)
        1───*  Proxy
        1───*  ProxyGroup
        1───*  AIModel
        1───*  RequestLog
        1───*  SystemSetting
```

`Channel` 字段：`name` / `slug` / `base_url` / `chat_path` / `models_path` /
`key_prefix` / `auth_scheme` / `default_rpm` / `enabled` / `is_default` / `notes`。

唯一性约束全部是「渠道内唯一」：`(channel, api_key)`、`(channel, name)`（分组）、
`(channel, protocol, host, port, username)`、`(channel, model_name)`、`(channel, key)`。
因此同一个模型名可以在多个渠道共存。

## 端点解析

用户通常直接粘贴完整 chat 地址。`Channel.save()` 与 `split_endpoint()` 会自动剥离：

| 粘贴内容 | base_url | chat_path |
|---|---|---|
| `https://opencode.ai/zen/v1/chat/completions` | `https://opencode.ai/zen/v1` | `/chat/completions` |
| `https://api.kilo.ai/api/gateway/chat/completions` | `https://api.kilo.ai/api/gateway` | `/chat/completions` |
| `https://api.llm7.io/v1/chat/completions` | `https://api.llm7.io/v1` | `/chat/completions` |
| `https://integrate.api.nvidia.com/v1` | `https://integrate.api.nvidia.com/v1` | `/chat/completions`（补全） |

`chat_url` = `base_url + chat_path`，`models_url` = `base_url + models_path`。

## 鉴权

`auth_scheme` 决定上游请求头，由 `services/upstream_service.auth_headers()` 生成：

| auth_scheme | 请求头 |
|---|---|
| `bearer`（默认） | `Authorization: Bearer <key>` |
| `x_api_key` | `X-API-Key: <key>` |
| `none` | 不加鉴权头 |

## 对外接口

```text
GET  /v1/models                      -> 默认渠道
POST /v1/chat/completions            -> 默认渠道
GET  /c/<slug>/v1/models             -> 指定渠道
POST /c/<slug>/v1/chat/completions   -> 指定渠道
```

也可以在请求体里带 `"channel": "zen"`（优先级低于 URL 前缀）。
模型必须存在于目标渠道且 `enabled=true`，否则返回 404 `model_not_found`。

## 管理 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/channels` | 渠道列表 + `current`（平台默认渠道） |
| POST | `/api/admin/channels` | 新建；`base_url` 可直接粘贴完整端点 |
| PATCH | `/api/admin/channels/{id}` | 编辑；`is_default: true` 设为默认（自动取消其他） |
| DELETE | `/api/admin/channels/{id}` | 删除（至少保留一个） |
| POST | `/api/admin/channels/{id}/test` | 用该渠道第一个 Key 打一次 `/models` |

## 渠道作用域的传递方式

管理端所有资源接口通过 **`X-Channel: <slug>`** 头或 **`?channel=<slug>`** 查询参数
选择渠道，由 `services/channel_service.resolve_from_request()` 解析，
解析不到时回落到平台默认渠道。

前端在 `lib/api.ts` 的 `request()` 里统一注入 `X-Channel`（读 localStorage），
因此各页面组件无需关心渠道；切换渠道时 `(console)/layout.tsx` 用 `key={channel}`
重挂载 `<main>`，页面数据自动按新渠道重新加载。

## 兼容性

- 旧路径 `/api/admin/nvidia-keys/*` 保留为 `/api/admin/keys/*` 的别名
- `NvidiaApiKey` 模型重命名为 `ChannelKey`（迁移 `0005_channel` 用 `RenameModel`，数据保留）
- 旧的运行参数 key `default_nvidia_rpm` 自动映射到 `default_upstream_rpm`
- 升级后既有的 Keys / 代理 / 模型 / 日志 / 设置会回填到自动创建的默认 NVIDIA 渠道
