# 总体架构

## 定位

NVIDIA2API 是面向 NVIDIA AI API 的聚合代理平台：

- 管理多个 NVIDIA API Key（40 RPM/Key 默认）、多协议代理（SOCKS5/HTTP/HTTPS）两者组成"线路池"
- 对外暴露 OpenAI 兼容的 `/v1/models`、`/v1/chat/completions`
- 核心能力：**多线路并发竞速 + 首个有效响应 Winner + 其余线路立即取消**

## 分层

```
┌────────────────────────── 客户端 ──────────────────────────┐
│ Browser (Next.js 控制台)   OpenAI SDK / curl (用户 API)     │
└──────────────┬────────────────────────┬────────────────────┘
               │ Admin Token            │ Bearer sk-nvidia2api-*
┌──────────────▼────────────────────────▼────────────────────┐
│                Django + DRF (api/)                         │
│   /api/admin/*                 /v1/*                       │
│   管理 CRUD/统计               OpenAI 兼容                 │
└──────────────┬─────────────────────────────────────────────┘
               │
┌──────────────▼────────────────┐
│        services/ 服务层        │
│  channel_service 渠道解析      │
│  key_service      Key 限流冷却 │
│  proxy_service    代理启用限制 │
│  proxy_checker    并发测速/IP  │
│  load_balancer    线路构建     │
│  race_engine      竞速执行     │
│  upstream_service 上游 HTTP    │
│  api_key_service        用户Key│
│  thinking         思考强度归一化│
│  sysconfig        运行时参数   │
└──────────────┬────────────────┘
               │ httpx(异步) + SQLite
┌──────────────▼────────┐   ┌────────────────────┐
│  SQLite (data/)       │   │ 多渠道上游          │
│                       │   │ NVIDIA / Zen / Kilo │
└───────────────────────┘   └────────────────────┘
```

## 关键决策

1. **业务不落 View**：`api/*` 只做参数校验与响应拼装，业务都在 `services/`。
2. **同步视图 + 竞速内部 async**：竞速在 `asyncio.run()` / 独立 event loop 中执行；"DRF 视图保持同步"避免 ASGI 迁移复杂度。
3. **SQLite 并发控制**：Key 的 RPM 计数用数据库侧条件 `UPDATE ... WHERE count < rpm_limit`，放弃 `SELECT FOR UPDATE`，避免 SQLite 锁升级死锁（detail 见 [database.md](database.md)）。
4. **运行时参数优先于环境变量**：`SystemSetting` 表中的值覆盖 `.env`，改后即时生效（`sysconfig.py`），且**按渠道隔离**。
5. **线路数 = 启用代理数 + 1 直连**：代理数量上限 = 该渠道 Key 数 − 1，由后端在 `set_enabled` 强制（不是前端校验）。
6. **渠道是一等公民**：上游 URL 与鉴权方式由 `Channel` 决定，不再有全局的 `NVIDIA_BASE_URL` 单点；Keys/代理/分组/模型/日志/设置全部挂 channel 外键（见 [channels.md](channels.md)）。

## 请求路径（聊天）

```
POST /v1/chat/completions  或  POST /c/<slug>/v1/chat/completions
  解析渠道（URL 前缀 > body.channel > 平台默认）
  验证 Bearer（UserApiKey, sha256）
  验证模型 enabled（限定在该渠道内）
  用户 Key 限流（rate_limit>0 才计数）
  全局并发信号量
  build_routes(channel) → [代理+Key]*N + [直连+Key]
  race (asyncio.FIRST_COMPLETED)
    ├ 首个"有效响应"判定 Winner（见 race-engine.md）
    ├ 其余任务 cancel + httpx 连接关闭
    └ 写 RequestLog（含每条线路明细）
  返回用户（SSE 或 JSON）
```
