# 后端模块

```
backend/
├── manage.py
├── config/          # settings / urls / asgi
├── apps/core/       # models + AdminConfig.ready() 自动迁移
├── services/        # 纯业务，零 Django View
├── api/             # Admin API + OpenAI API
└── tests/           # 182 个用例（导入/限流/竞速/并发/启停上限/思考强度/多渠道/额度/熔断）
```

## services/

| 文件 | 职责 |
|---|---|
| `channel_service.py` | 渠道解析：`resolve_from_request()` 按 `X-Channel` 头 / `?channel=` 选渠道；`ensure_default_channel()` 懒建默认渠道；`test_channel()` 连通性探测 |
| `key_service.py` | bulk_import（去重/自动命名，**按渠道**）、`claim_rpm_slot`（条件更新）、`report_failure` 状态机（401→invalid，429→rate_limited+60s 冷却，其余 60s 冷却）、脱敏 |
| `proxy_service.py` | `bulk_import_proxies`、`set_enabled`（**强制 enabled ≤ 该渠道 keys-1**）、`report_proxy_result`（连续 3 败 → unhealthy + 冷却）、`schedulable_proxies`（排除 cooldown/unhealthy，按延迟排序） |
| `proxy_checker.py` | `check_proxy`（走代理访问 ipinfo.io，测延迟+IP+地理）、`check_all(channel)`（Semaphore(20) 并发） |
| `load_balancer.py` | `build_routes(channel)`：线路数 = min(启用代理数+1, 可用 Key 数, max_routes)；第 i 个代理配第 i 个 Key，最后一条直连 |
| `race_engine.py` | 竞速与流式竞速（见 race-engine.md）；上游 URL 与鉴权头由 `route.key.channel` 决定 |
| `api_key_service.py` | `generate_key`（sk-nvidia2api-36hex）、hash 查询、可选限流（rate_limit>0 时每分钟窗口） |
| `upstream_service.py` | 上游 HTTP：`auth_headers()` 按渠道鉴权方式生成头、`list_models_raw`、`sync_models` 幂等 upsert、`probe` |
| `thinking.py` | 思考强度参数：`parse()` 把各种客户端写法收敛成 `ThinkingSpec`，`to_upstream()` 按 `thinking_passthrough` / `thinking_strip_models` 决定是否下发 |
| `model_registry.py` | 对外名解析：主别名 + 附加别名（多别名）→ 模型映射；`resolve()` 让任意对外名都能路由到同一个上游模型 |
| `responses_api.py` | Responses API（`/v1/responses`）请求/响应格式与 chat 的自动互转 |
| `anthropic_api.py` | Anthropic `/v1/messages` 协议转换与鉴权 |
| `channel_health.py` | 渠道连续失败自动熔断 + 冷却状态管理（`channel_cooldown_failures` / `channel_cooldown_seconds`） |
| `crypto.py` | 敏感字段（Key / 代理密码）AES-GCM 加密（Fernet），密钥取 `ENCRYPTION_KEY` 或由 `SECRET_KEY` 派生；旧明文自动回落兼容 |
| `cleanup.py` | 请求日志清理（按 `log_retention_days` 保留天数） |
| `sysconfig.py` | 运行时参数注册表：**按渠道隔离**；`get(key, channel)` / `set_params(updates, channel)` / `reset_params()` |

## api/

- `api/urls.py`：所有端点注册
- `api/auth.py`：`admin_required`/`AdminRequiredMixin`（`Authorization: Token <ADMIN_TOKEN>`）、`openai_error()` 统一错误格式
- `api/admin_views.py`：后台全部视图（登录/Channels/Keys/Proxies/Groups/Models/UserKeys/Logs/Dashboard/Usage/Settings/Chat），资源接口统一按 `current_channel(request)` 过滤
- `api/openai_views.py`：`/v1/*` 与 `/c/<slug>/v1/*`，全局 `BoundedSemaphore(MAX_CONCURRENT_REQUESTS)`，OpenAI 错误码
- `api/serializers.py`：所有序列化器；NVIDIA Key 默认脱敏，仅 `?reveal=1` 时返回明文

## 日志

所有关键动作打到 logger `nvidia2api.*`，级别由 `LOG_LEVEL` 控制。敏感值（完整 NVIDIA Key、代理密码）不会出现在日志里。

## 自动迁移

`apps/core/apps.py` 的 `ready()` 里：

```python
if not any(cmd in sys.argv for cmd in ("migrate", "makemigrations", "test", "pytest")):
    call_command("migrate", run_syncdb=True, verbosity=0)
```

删除数据库后再启动服务，表会自动重建，避免 `no such table` 类事故。
