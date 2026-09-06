# 数据库设计

SQLite，默认文件 `data/db.sqlite3`（可用 `DATABASE_PATH` 覆盖）。后端启动时自动 `migrate`，永远不需要手动建表。

## ER 概览

```
Channel 1 ──── n ChannelKey
       1 ──── n Proxy
       1 ──── n ProxyGroup ──── n Proxy
       1 ──── n AIModel
       1 ──── n RequestLog
       1 ──── n SystemSetting

UserApiKey 1 ──── n RequestLog      # 用户 Key 跨渠道共享
```

除 `user_api_key` 外，所有业务表的唯一性约束都是「渠道内唯一」。

## 表结构

### channel

| 字段 | 类型 | 说明 |
|---|---|---|
| name | varchar(64) UNIQUE | 显示名 |
| slug | varchar(64) UNIQUE | URL 与 `X-Channel` 用的标识 |
| base_url | varchar(512) | 上游 base；`save()` 时会剥离掉粘进来的 `/chat/completions` |
| chat_path | varchar(128) | 默认 `/chat/completions` |
| models_path | varchar(128) | 默认 `/models` |
| key_prefix | varchar(32) | 仅作提示用，不做强校验 |
| auth_scheme | varchar(16) | `bearer` / `x_api_key` / `none` |
| default_rpm | int | 该渠道新建 Key 的默认 RPM |
| enabled / is_default | bool | `is_default` 全库唯一（保存时自动取消其他） |

派生（非字段）：`chat_url` = base_url + chat_path，`models_url` = base_url + models_path。

### channel_key（原 nvidia_api_key，迁移 0005 重命名）

| 字段 | 类型 | 说明 |
|---|---|---|
| channel_id | FK → channel | |
| name | varchar(128) | |
| api_key | varchar(256) | 唯一约束 `(channel, api_key)`；脱敏只在序列化层做 |
| status | varchar(16) | `available` / `rate_limited` / `error` / `disabled` / `invalid`（有索引） |
| rpm_limit | int | 默认 40 |
| minute_window_start | datetime | 当前计数窗口起点 |
| minute_request_count | int | 窗口内已用 |
| success_count / failure_count | int | 累计 |
| cooldown_until | datetime | 冷却截止（429/网络错误） |
| last_used_at | datetime | LRU 排序键 |
| last_error | varchar(256) | 最近一次错误摘要 |

### proxy_group / proxy

Proxy 字段：`protocol`(`socks5|socks5h|http|https`)、`host`、`port`、`username`、`password`、`group_id`、`country/region/city/isp`、`enabled`、`status`(`unknown|healthy|degraded|unhealthy|disabled`)、`latency_ms`、`public_ip`、`last_check_at`、`success/failure_count`、`consecutive_failures`、`cooldown_until`。

Proxy / ProxyGroup 均带 `channel_id`。唯一约束 `(channel, protocol, host, port, username)`
与 `(channel, name)`，索引 `(enabled, status)`——因此同一个代理地址可在不同渠道各存一份。

### model

带 `channel_id`；唯一约束 `(channel, model_name)`，索引 `(enabled, model_name)`。
只有 `enabled=true` 的模型在**其所属渠道**内对外暴露——同名模型可在不同渠道共存。

### user_api_key

存 `key_hash`(SHA-256，UNIQUE，索引)、`key_prefix`（展示用前 22 位）、`enabled`、`rate_limit`（**0 = 不限**）、`total/success/failed_requests`、`minute_*` 窗口字段、`last_used_at`。

### request_log

| 字段 | 说明 |
|---|---|
| channel_id | FK → channel，删除渠道时置 NULL |
| request_id | `req_xxx`，索引 |
| model / status / http_status / error_type | |
| duration_ms | 总耗时（流式为 [DONE] 时调正） |
| first_token_ms | 首 chunk 耗时 TTFT（流式） |
| winner_route_type / winner_key_name / winner_proxy_name / proxy_public_ip | Winner 线路 |
| is_stream / routes_count | |
| prompt/completion/total/cached_tokens | token 指标（stream 时来自 `usage`，需 `stream_options.include_usage`） |
| routes | JSON：本次竞速每条线路的 `{name,kind,key_name,proxy_name,status,latency_ms,error,http_status}` |

#### 索引策略（2026-09 重排，迁移 0024 / 0025）

本表**每请求都写**，且平均 **20KB/行**（`routes` 竞速明细 + `request_summary`
诊断 JSON 占大头）。两个约束共同决定索引设计：读要覆盖管理端全部查询形态，
写不能无限膨胀。最终 5 条业务索引（+ `request_id`、`user_api_key_id`）：

| 索引 | 服务的查询 | 实测 |
|---|---|---|
| `request_log_usage_cover`<br>(created_at, status, model, channel, user_api_key,<br>5×token, duration_ms, first_token_ms) | 仪表盘 usage 分桶/汇总/分布、`/metrics`、日志清理 | 7~30 天 681→82ms |
| `request_log_channel_created`<br>(channel, created_at, status, duration_ms) | DashboardView「今日」聚合（每 10s 被轮询） | 58→0.85ms |
| `request_log_channel_id` (channel, id) | 日志页默认翻页 + COUNT | limit=100 82→5.7ms |
| `request_log_channel_status` (channel, status, id) | 日志页按状态筛选 | 87→19ms |
| `request_log_channel_model` (channel, model, id) | 日志页按模型筛选 | 44→5.4ms |

三条经验，**改索引前请先读**：

1. **服务列表的索引必须以 `id` 收尾。** 日志页恒 `ORDER BY -id` + LIMIT；索引少了
   `id`，planner 会先用 channel 前缀捞出该渠道**全部** rowid 再建 TEMP B-TREE 排序，
   实测比不加索引还慢（7.4ms → 35ms）。`RequestLogIndexPlanTests` 用
   `EXPLAIN QUERY PLAN` 断言计划里不得出现 TEMP B-TREE——这种退化不会让任何
   功能测试变红，只能靠计划守卫拦住。
2. **列表查询必须 `defer` 那四个肥 JSON 列。** 列表序列化器本就不输出它们，但 ORM
   默认 `SELECT *` 会整行读回（defer 省的是溢出页）。100 行 79.6ms → 2.1ms。
   守卫见 `test_list_query_defers_heavy_json_columns`。
3. **只改 `db_index` 不要用裸 `AlterField`。** SQLite 会走"建新表 → 整表拷贝 →
   删旧表 → 改名"的重建路径（500MB 实测 25s），而本表只增不减、容器启动又自动
   migrate，重建时间会随数据量线性恶化到分钟级。迁移 0025 用
   `SeparateDatabaseAndState`：状态侧照常 AlterField，数据库侧只做 `DROP INDEX`，
   0.63s 完成，`makemigrations --check` 验证状态无漂移。

同时删除了被上述索引前缀覆盖的 5 条旧索引（`created_at` 单列、`(created_at,status)`
复合、`channel_id` / `model` / `status` 单列），**净索引数与重排前持平**，写成本不升。

### system_setting

带 `channel_id`；唯一约束 `(channel, key)`。即**按渠道隔离**的运行参数表
（见 `sysconfig.RUNTIME_PARAMS` 注册表）。空 value 表示回落默认值。

## 并发策略

- SQLite 默认串行写。两个热点计数器都采用**条件更新**而非 `SELECT FOR UPDATE`：
  ```python
  base.filter(window_stale).update(count=1, window_start=now) or
  base.filter(count__lt=F("rpm_limit")).update(count=F("count")+1)
  ```
  单条 UPDATE 原子执行，无锁升级死锁，多线程下永不超过 RPM 上限（有并发测试验证）。
- `DATABASES.OPTIONS.timeout=30s`，写锁等待不至于立即报错。
- 未来迁移 PostgreSQL：所有表使用标准类型，无外键 CASCADE 之外的自定义逻辑，可直接 `migrate`。
