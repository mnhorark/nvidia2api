# 总体架构

## 定位

NVIDIA2API 是**多通道（Channel）AI API 聚合网关**（项目名沿用自最初的 NVIDIA 单上游形态）：

- 每个渠道 = 一个 OpenAI 兼容上游端点 + 独立的 Key 池 / 代理池 / 模型表 / 运行参数
- 管理多个上游 API Key（默认 40 RPM/Key）与多协议代理（SOCKS5/HTTP/HTTPS），两者组成"线路池"
- 对外暴露三套兼容协议，且都支持 `/v1/*`（默认渠道）与 `/c/<slug>/v1/*`（指定渠道）双前缀：
  OpenAI Chat Completions、OpenAI Responses、Anthropic Messages（含 `count_tokens`）
- 核心能力：**多线路并发竞速 + 首个有效响应 Winner + 其余线路立即取消**
- **单进程契约**：并发闸门、登录限速、运行参数缓存均为进程内状态，启动时用文件锁拒绝第二个实例（详见 README「并发说明」）

## 分层

```
┌────────────────────────── 客户端 ──────────────────────────┐
│ Browser (Next.js 控制台)   OpenAI SDK / curl (用户 API)     │
└──────────────┬────────────────────────┬────────────────────┘
               │ Admin Token            │ Bearer sk-nvidia2api-*
┌──────────────▼────────────────────────▼────────────────────┐
│                Django + DRF (api/)                         │
│   /api/admin/*（admin_views 包，按资源拆分）   /v1/*       │
│   管理 CRUD/统计               OpenAI 兼容                 │
└──────────────┬─────────────────────────────────────────────┘
               │
┌──────────────▼────────────────┐
│        services/ 服务层        │
│  channel_service 渠道解析      │
│  channel_health 渠道熔断       │
│  key_service      Key 限流冷却 │
│  proxy_service    代理启用限制 │
│  proxy_checker    并发测速/IP  │
│  load_balancer    线路构建     │
│  race_engine      竞速执行     │
│  responses_api / anthropic_api  协议转换（内部统一 chat 格式）│
│  thinking         思考强度归一化│
│  tokenizer        本地 token 估算│
│  crypto           敏感字段加密   │
│  cleanup          日志保留清理   │
│  loop_offload     事件循环去阻塞 │
│  sysconfig        运行时参数(缓存)│
│  api_key_service        用户Key│
│  upstream_service 上游 HTTP    │
└──────────────┬────────────────┘
               │ httpx(异步) + SQLite
┌──────────────▼────────┐   ┌────────────────────┐
│  SQLite (data/)       │   │ 多渠道上游          │
│                       │   │ NVIDIA / Zen / Kilo │
└───────────────────────┘   └────────────────────┘
```

## 关键决策

1. **业务不落 View**：`api/*` 只做参数校验与响应拼装，业务都在 `services/`。
2. **异步流式 + ASGI 逐块下发**：流式响应是 async 生成器，Django ASGI 逐块转发
   （同步生成器会被一次性缓冲成"假流式"）。竞速在事件循环内执行；
   同步 DB 写经 `services/loop_offload.run_db` 挪到线程池，避免写锁阻塞事件循环
   （卡死主链，见 audit R4）。
3. **SQLite 并发控制**：Key 的 RPM 计数用数据库侧条件 `UPDATE ... WHERE count < rpm_limit`，放弃 `SELECT FOR UPDATE`，避免 SQLite 锁升级死锁（detail 见 [database.md](database.md)）。
4. **运行时参数优先于环境变量**：`SystemSetting` 表中的值覆盖 `.env`，改后即时生效（`sysconfig.py`，带 TTL 缓存 + 信号失效），且**按渠道隔离**。
5. **线路数 = 启用代理数 + 1 直连**：代理数量上限 = 该渠道 Key 数 − 1，由后端在 `set_enabled` 强制（不是前端校验）。
6. **渠道是一等公民**：上游 URL 与鉴权方式由 `Channel` 决定，不再有全局的 `NVIDIA_BASE_URL` 单点；Keys/代理/分组/模型/日志/设置全部挂 channel 外键（见 [channels.md](channels.md)）。
7. **管理视图按资源拆分**：`api/admin_views/` 包（auth/channels/keys/proxies/proxy\_groups/models\_admin/user\_keys/logs/dashboard/settings/chat + common），
   `__init__.py` 聚合导出保持 `admin_views.XxxView` 引用兼容（参考 new-api 按资源分 handler）。
8. **宽松判胜 + 透传**：竞速比"谁先开始出流"（首个结构 chunk，含空 role 块），
   胜出后默认不掐流（`stream_first_content_timeout=0`），正文/思考到达速度是模型特性
   （对齐 new-api 透传语义，见 audit R5）。
9. **协议转换统一内部格式**：`/v1/responses`、`/v1/messages` 入口转成内部 chat 格式，
   出口再转回各协议 SSE（`responses_api` / `anthropic_api`），竞速/日志/限流完全复用。

## 请求路径（聊天）

```
POST /v1/chat/completions  或  POST /c/<slug>/v1/chat/completions
  解析渠道（URL 前缀 > body.channel > 平台默认）
  验证 Bearer（UserApiKey, sha256）
  验证模型 enabled（限定在该渠道内）
  用户 Key 限流（rate_limit>0 才计数）
  动态并发闸门（sysconfig.max_concurrent_requests，事件循环计数）
  build_routes(channel) → [代理+Key]*N + [直连+Key]（RPM 原子 claim + 排除先于 claim）
  race (asyncio.FIRST_COMPLETED) —— 宽松判胜：首个结构合法 chunk 即 Winner
    ├ 其余任务 cancel + httpx 连接关闭
    ├ 每线路统计写库经 run_db 移出事件循环
    └ 写 RequestLog（含每条线路明细）
  返回用户（SSE async 生成器逐块转发 / JSON）
    失败路径落库一律 _safe_finish/_safe_save 兜底，绝不逃逸炸 ASGI
```

