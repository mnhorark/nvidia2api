# nvidia2api 架构全面审查报告

> 审查日期：2026-09-05 · 基线 commit `db612ef` + 工作树未提交改动
> 范围：整体架构、后端分层、服务层逐模块、API 层、前端、测试、部署、仓库卫生与安全
> 方法：全量文件阅读 + 关键缺陷逐条人工复核（标注 ✅ 已复核）／`pytest` 实跑

---

## 一、结论摘要

项目已从 `AGENTS.md` 描述的"单 NVIDIA 聚合器"完整演进为**多通道（Channel）AI API 网关**，核心链路（竞速、限流、透传、记账）质量显著高于一般同规模项目，服务层注释密度和"事故驱动"的可追溯性（几乎每个非平凡分支都带 req_id 或日期）是真实运维过的痕迹，不是生成物。

- **测试基线**：`cd backend && python -m pytest -q` → **473 passed / 0 failed**（Python 3.14.6、Django 6.1、56s），无 skip/xfail。
- **架构判断**：分层清晰、依赖方向单一（views → services → models），SQLite 并发策略自洽且成体系。**主要风险不在设计，而在三处结构性约束没有被显式契约化**：单进程假设、零丢失原则的多套实现、事件循环上的同步 ORM。
- **缺陷清单**：1 个确认的计费泄漏（✅）、1 个确认的请求体就地改写（✅）、若干热路径效率问题、若干部署/仓库卫生问题。详见第九节。

一句话：**核心链路可信，外围治理欠账**。欠的都是低成本高收益的收口，不需要重构。

---

## 二、系统全景

### 2.1 部署拓扑

```
单一容器（all-in-one）
├─ 构建期：Node 22 → Next.js 16 `output:"export"` → /app/static/frontend
└─ 运行期：uvicorn config.asgi:application --port 8000（单进程，无 --workers）
           ├─ /v1/*  /c/<slug>/v1/*   数据面（OpenAI / Anthropic / Responses 三协议）
           ├─ /api/admin/*            管理面（静态 Token 鉴权）
           ├─ /healthz  /metrics      可观测性
           └─ /*                      Django 视图同源托管前端静态产物
           SQLite WAL @ ./data:/app/data
```

Windows 本地开发走 `start.bat`（生产模式同源托管 / `dev` 模式前后端分离 :3000 + :8000）。

**关键约束：整个系统的正确性依赖"单进程"这一隐含前提。** 进程内全局状态包括：

| 状态 | 位置 | 多进程后果 |
|---|---|---|
| 并发请求闸门 `_active_count` | `api/openai_views.py:37-38` | `max_concurrent_requests` 被静默乘以 worker 数 |
| 上游 socket 阀门 `_upstream_active` | `api/openai_views.py:71-72` | 同上，全局阀门失效 |
| 登录失败限速桶 | `api/admin_views/common.py:104-136` | 爆破防护按 worker 数稀释 |
| Dashboard usage 缓存 | `api/admin_views/dashboard.py:240-261` | 数据不一致（仅展示层） |
| `model_registry` / `sysconfig` TTL 缓存 | `services/model_registry.py:31`、`services/sysconfig.py:35-36` | 跨进程最长 3s 陈旧（可接受） |

`Dockerfile:24` 的 CMD 恰好是单进程，所以**当前部署是对的**；但代码层没有任何断言或文档阻止有人加 `--workers 4`。这是我最想强调的一条结构性风险。

### 2.2 请求生命周期（数据面）

```
POST /v1/chat/completions   （或 /v1/responses、/v1/messages）
  │
  ├─ _authorize            Bearer → SHA-256 → UserApiKey；enabled / RPM / claim_quota(预占1)
  ├─ _parse_body           字节上限（默认 32MB，可配）→ JSON 对象校验
  ├─ 入口协议归一          responses_to_chat_body / messages_to_chat_body
  │
  └─ _run_authed
       ├─ 模型解析         model_registry.resolve（全局别名）/ resolve_in_channel（/c/<slug>/）
       ├─ 思考参数归一     thinking.build_upstream（一次计算，同时供上游体与审计日志）
       ├─ 上游体构造       _build_upstream_body：黑名单剔除 + extra_body 平铺 + tool_choice 归一
       ├─ 形态钳制         message_shape.clamp_message_shapes / message_fixups.dedupe_tool_call_ids
       ├─ 工具名别名       tool_alias.shorten_function_names（>60B 名字，出口还原）
       ├─ 线路构建         load_balancer.build_routes → Route[]（代理+Key，恰好 1 条直连）
       ├─ 上游并发闸门     _reserve_upstream(len(routes))，被裁线路退回其 RPM 名额
       ├─ RequestLog 落库  status=pending
       │
       ├─ 非流式           race_chat → asyncio.run(_race) → 首个"有效响应"胜出 → 取消其余
       └─ 流式             _stream_response（async gen）
             ├─ race_stream 竞速窗口包成心跳循环（`: keep-alive` 注入，防 agent idleTimeout 掐线）
             ├─ winner 诞生 → 日志预填 TTFT，status 仍 pending
             ├─ _drain 转发泵：逐 chunk 单次解析（StreamTap），超时档位切换 + 心跳
             ├─ 出口协议转换：iter_chat_sse_as_responses / _as_anthropic
             ├─ 静默截断检测：无 finish_reason 且无 [DONE] → 未出内容则换线重试，
             │                已出内容则显式 error 帧（绝不伪造 [DONE]）
             └─ settle：记账 + 渠道健康入账；客户端断开在 finally 强制收尾
```

这条链路的**设计成熟度体现在细节**：闸门裁剪与 RPM 名额回滚成对出现（`openai_views.py:462-469`、`752-759`）、winner 诞生时不置 success 而等 drain 结束（`800-806`，避免 0ms 脏记录）、`claim_quota` 预占 1 token 在所有失败路径显式退还。

### 2.3 分层与依赖方向

```
api/（views）──► services/（业务与协议）──► apps/core/models（ORM）
                        │                          │
                        └──────► services/crypto ◄─┘   ← 唯一倒挂：models.save() 调 crypto
```

唯一的依赖倒挂是 `apps/core/models.py:5` 引入 `services.crypto`（模型在 `save()` 里加密/生成脱敏提示）。这使 `crypto.py` 事实上位于 ORM 之下，属于可接受的实用主义，但值得在文档里写明。

---

## 三、数据模型层（`apps/core/models.py`，430 行）

9 张表，围绕 **Channel 作为租户边界**重构了原始 spec：

| 模型 | 角色 | 关键设计 |
|---|---|---|
| `Channel` | 上游端点（base_url + chat/models path + auth scheme） | `split_endpoint`/`join_url` 支持粘贴完整端点；`is_default` 互斥在 `save()` 内事务保证；渠道级熔断列 `consecutive_failures`/`cooldown_until`；三个"降级行为开关"`allow_duplicate_keys`/`disable_key_invalid`/`disable_proxy_unhealthy` 服务公共/匿名渠道 |
| `ChannelKey` | 渠道下的上游 Key | `api_key` TextField 存 Fernet 密文；`api_key_hint` 预存脱敏值（列表页零解密）；`status` + `minute_window_start/count` + `cooldown_until` 承载限流状态机 |
| `ProxyGroup` / `Proxy` | 代理池，按渠道隔离 | 端点四元组唯一约束；`enabled` 默认 False；健康列 `latency_ms`/`consecutive_failures`/`cooldown_until` |
| `AIModel` | 模型 + 对外别名 | `alias` + `aliases[]` 多对外名；`route_priority` 解跨渠道重名；`proxy_group` 模型级线路绑定；`endpoint` 模型级端点覆盖（走 /responses 的模型） |
| `UserApiKey` | 平台对外发放的 Key | **只存 SHA-256 hash**；RPM + token 额度（`quota`/`used_quota`）双闸门 |
| `RequestLog` | 请求全生命周期 | `routes` JSON 记录每条线路结局；`client_thinking`/`upstream_thinking`/`request_summary` 三个诊断 JSON；`first_token_ms`/`cached_tokens` |
| `SystemSetting` | 运行时参数 | `(channel, key)` 唯一，实现"参数按渠道隔离" |

**索引覆盖良好**：`ChannelKey(status,last_used_at)`、`Proxy(enabled,status)`、`AIModel(enabled,model_name)`、`RequestLog(created_at,status)` 都对应真实查询形态。

### 模型层问题

1. ⚠️ **`Proxy.password = CharField(max_length=128)`（`models.py:242`）存的是 Fernet 密文**（≈明文 1.4× 再 base64 膨胀 + `enc:v1:` 前缀）。SQLite 不校验长度所以现在无害，但同类问题在 `ChannelKey.api_key` 上已经修过（迁移 0017 改 TextField），代理这条**漏了**。迁 PostgreSQL 会直接写入失败。
2. `ChannelKey` 无数据库层唯一约束（`models.py:185-186`，为 zen/kilo 允许重复 Key 而刻意放开），去重完全依赖应用层 `bulk_import_keys` 的解密比对——正确，但意味着库里可以有真重复行，运维查询时要意识到。
3. `RequestLog` 无外键级联清理策略，靠 `cleanup.clean_old_logs` 分批删（`services/cleanup.py:54-62`，设计合理），但**没有任何调度器周期触发它**（见第八节）。

---

## 四、服务层逐模块（`backend/services/`，25 文件 ≈ 6.9k 行）

质量最高的一层。按职责分四个子层。

### 4.1 协议转换层

| 模块 | 行数 | 职责 | 评价 |
|---|---|---|---|
| `responses_api.py` | 1397 | Responses ⇄ chat 双向（含 SSE 双向翻译） | 功能正确、契约最完整，但**是全项目最大文件**，`_translate_event` ~195 行、`iter_chat_sse_as_responses` ~315 行 |
| `anthropic_api.py` | 651 | Messages ⇄ chat 三向转换 | 结构清晰，`finally` 里 `aclose()` 内层生成器（`:594-597`）这类细节到位 |
| `thinking.py` | 722 | 任意客户端方言的思考参数归一 → 按模型族下发 | 策略表驱动，可维护性好，但见缺陷 #2 |
| `reasoning_decrypt.py` | 465 | 上游加密思考内容的跨块重组解密 | 零丢失（解不开就原样透传）+ 64KB 缓冲上限防 DoS |
| `stream_pipeline.py` | 182 | `StreamTap` 单点解析观察器 | 好设计：把旧管线每 chunk 最多 6 次 `json.loads` 收敛到 1 次，且"只观察不改写" |
| `tool_stream.py` | 232 | 上游畸形 `tool_calls` 增量规整（恒挂，无开关） | 三态合并逻辑正确，见小瑕疵 |
| `tool_alias.py` | 170 | >60B 工具名确定性别名化 + 出口还原 | 纯函数、无状态 |
| `message_shape.py` / `message_fixups.py` | 127 / 75 | AI SDK 方言形态钳制 / 跨轮重复 tool_call id 唯一化 | 小而准，"无问题则零改动"契约明确 |

**协议层的统一信条是"零丢失"**，但它有三套不同实现口径：
- 字节级原样透传（`stream_pipeline`、`tool_stream` 无 tool_calls 路径）
- JSON 降级保留（`message_shape` 把未知块 `json.dumps` 成字符串而非丢弃）
- 密文不解则原样带过（`reasoning_decrypt`）

三者各自都对，但**同名 helper 语义分叉**是真实隐患：`responses_api._content_to_text`（`:301`）对非文本块**静默丢弃**，`message_shape._content_to_text`（`:34`）则 JSON 降级保留。✅ 已复核两处定义。

### 4.2 请求编排层

**`race_engine.py`（795 行）—— 系统心脏。** 实现与 spec 第十九~二十三节完全对齐，且避开了 spec 第四十七节点名的"串行重试"反模式：

- `_race` / `race_stream_winner` 用 `asyncio.wait(FIRST_COMPLETED)` 循环，**完成 ≠ 成功**：每个完成先校验，失败则继续等其余，成功才判胜（`race_engine.py:398-429`、`596-650`）。
- `is_valid_response` 明确拒绝裸 200（`:81-95`）；`is_valid_stream_chunk` 采用**宽松判胜**并给出理由（思考模型静默数十秒不应拖垮竞速窗口，`:98-126`），同时保留"裸 `[DONE]` 不算胜"的防呆。
- **取消与资源回收是真做过的**：winner 出现后 cancel + gather 其余；同批次并列完成的其它胜者候选连接也显式关闭（`:620-637`，注释直说"否则每次竞速泄漏数个 fd"）；`finally` 兜底取消未完成任务（`:651-660`）。
- **竞速 prelude 重放**：判胜首帧之前消费的行（usage 预告帧、心跳注释、自定义事件）按原序重放，不蒸发（`:497-534`、`718-721`）。
- **绝不伪造 `[DONE]`**：静默截断通过 `final_state["saw_done"]` 上报，由视图决定重试或报错（`:677-694`）。

风险点（非缺陷，是脆弱性）：
- `_stream_first_valid`（`:444-576`）手工管理 `cm.__aenter__/__aexit__` 与 `req_cm` 两级上下文，6 条退出路径，注释里已经出现"不得触碰未进入的 req_cm"这种防御性说明——**这是应该改用 `AsyncExitStack` 的代码**。
- `race_chat = asyncio.run(...)`（`:581`）：从同步视图调用没问题，但没有任何"已在事件循环中"的守卫，未来谁在 async 上下文里调它就是 RuntimeError。
- `_classify_content_rejection`（`:339-379`）：全线路 400 时用**用户的 Key** 向真实上游发 `"hi"` 探针。分类价值确实高（注释记录了被误导数小时的事故），但代价是烧配额 + 最长 40s 额外延迟，且超时硬编码。建议加开关与配额豁免。
- 每条线路每请求新建 `httpx.AsyncClient`（`:209`、`:447`）：代理隔离下连接池复用本就困难，但这个取舍**没有写在文档里**，读代码的人只会以为是漏了。

**`load_balancer.py`（113 行）—— 小而正确。** `route_count = min(len(proxies)+1, len(keys), max_routes)` 精确实现 spec 第十四节的 `代理 ≤ Key-1` 拓扑；"先排除后 claim"（`:84-106`）避免重试风暴白烧 RPM；claim 失败/组合排除不消耗线路配额、继续用后续 Key 回填（`:89-93`）。

问题在于**契约跨模块**：`build_routes` 内部 claim 了 RPM，而 `release_rpm_slot` 的调用责任在 `openai_views`（三处）。Route 上带 `claimed` 标志位来提醒调用方——这是靠约定维持的正确性，容易在新增调用点时漏掉。

### 4.3 资源管理层

| 模块 | 职责 | 关键设计 |
|---|---|---|
| `key_service.py` | 渠道 Key 生命周期、RPM 原子领取、状态机、批量导入 | **条件 UPDATE 领取**（`claim_rpm_slot`），因为 SQLite 的 `select_for_update` 是 no-op——注释明确写了这点（`:183-185`） |
| `api_key_service.py` | 用户 Key 鉴权、RPM、token 额度预占/结算 | `claim_quota` 原子预占 1 token，`check_quota` 降级为"仅 UI 用"；负数 token 钳制防上游"退款"（`:137-141`） |
| `proxy_service.py` | 代理 CRUD/导入/**启用上限**/健康计数 | `set_enabled` 里**故意写一次 Channel 行**去抢 SQLite 写锁，把 check-then-act 串行化，"宁可拒绝不可超限"（`:131-155`）——这是理解 SQLite 才写得出的方案 |
| `proxy_checker.py` | 异步并发测速 + 公网 IP/地理 | "任何 HTTP 响应（含 429）都证明隧道活着"，防单一 IP 源限流毒化整个池（`:15-22`） |
| `channel_service.py` | 渠道解析（`X-Channel`/`?channel=`/默认） | **`lookup` vs `resolve` 是安全契约**：`/c/<slug>/` 必须用 `lookup`，未知 slug 不得静默回落到默认渠道（`:84-88`） |
| `channel_health.py` | 渠道级熔断 | 失败分类学最讲究的一块：`no_available_route` 不计入（防自激）、流超时视为"模型延迟"既不计也不清、401/403/429 是"账号还活着"的证据 → **重置**计数（`:61-92`） |
| `model_registry.py` | 全局别名路由表 | 信号即时失效 + 3s TTL 兜底（对付 `queryset.update()` 绕过信号） |
| `upstream_service.py` | 渠道级上游 HTTP（模型列表/同步/探测） | curl_cffi Chrome 指纹回落对付 Cloudflare（`:45-51`） |
| `cleanup.py` | 日志保留期清理 | 分批 PK 删除，避免长时持有写锁 |

### 4.4 基础设施层

- **`loop_offload.py`（45 行）—— 全项目最重要的 45 行。** `run_db = asyncio.to_thread` 把同步 ORM 写挪出事件循环，起因是"整站冻结"事故：SQLite `busy_timeout` 最长 30s 阻塞在循环线程上 → 健康检查超时 → 容器被重启。事务块内自动回落同线程，异常时**失败关闭**（返回 True = 不卸载）。文档质量全层最佳。
- `sysconfig.py`（267 行）：约 25 个运行时参数注册表，按渠道隔离，3s TTL 读缓存 + 信号失效。平台级参数锚定到 `is_default` 渠道，防"写 A 读 B"。
- `crypto.py`（81 行）：Fernet + `enc:v1:` 前缀 + 明文回落；`decrypt_secret` 解密失败返回**空串而非密文**，防止把加密块当 Key 发给上游（`:66-73`）。

---

## 五、API 层（`backend/api/`）

### 5.1 两个鉴权平面

- **数据面**：`Authorization: Bearer sk-nvidia2api-*` → SHA-256 → `UserApiKey`。库里只有 hash，平台无法回显用户 Key。
- **管理面**：`Authorization: token <ADMIN_TOKEN>`，静态共享密钥，支持 `ADMIN_TOKENS` 逗号分隔多值**轮换**（旧值保留期内并存）。比较用 `hmac.compare_digest`。

两套 header scheme 不同（Bearer vs token），互不可能误认。

### 5.2 防护与限流

- 全局并发闸门 + 上游 socket 阀门（保底 1 条线路，防"裁成 0 线路 → no_available_route 重试风暴"的挤兑设计，`openai_views.py:93-116`）。
- 每用户 RPM 与额度都在**入口原子领取**，不依赖前端。
- 登录失败限速（10 次/60s，按 `REMOTE_ADDR|XFF` 首跳）。
- 生产默认凭据门禁：检测到默认密码/Token/SECRET_KEY 且 `DEBUG=false` 且未显式 `ALLOW_DEFAULT_CREDENTIALS=true` → **启动直接 RuntimeError**（`settings.py:207-232`）。这是配置层最有价值的一段。
- 前端托管的路径穿越防护是分层实现：raw 与 unquote 双形态拒绝 `..`/绝对路径，再 `resolve().relative_to(root)` 收敛，命中返回 404 而非回落 SPA index（`frontend_views.py:56-98`）。

### 5.3 API 层债务

1. **错误响应没有统一信封** —— 三种形态并存：完整 OpenAI `{error:{message,type,param,code}}`、管理端简版 `{error:{message,code}}`、DRF 风格 `{detail:...}`，以及 `logs.py:69` 的 `{'error': 'log_not_found'}`（`error` 是**字符串**）。客户端无法稳定读 `error.message`。
2. **除日志外全部分页缺失** —— keys / proxies / models / user-keys / channels 都是整表序列化。目前靠"列表页用 `api_key_hint` 零解密"+ GZip 硬扛 2000+ Key，属于把债压在性能技巧上。
3. **写路径基本不用 serializer** —— 手写 `_parse_int/_parse_bool/_require_*`（`common.py:33-102`）。DRF serializer 只读。可维护性一般，但 `_parse_bool` 正确避开了 `bool("false")` 陷阱。
4. `?reveal=1` 可让管理员取回**完全解密的上游 Key**（`keys.py:80-82`），仅 Token 一道门，无二次确认、无审计日志。这是全系统最敏感的单点。
5. `ProxyFetchIpView` 是 `ProxyTestView` 的裸别名（`proxies.py:125-126`），语义靠实现巧合。

### 5.4 配置层

- **ASGI-only**（无 `wsgi.py`），`ASGI_THREADS=64` 抬高 asgiref 默认。
- SQLite：`OPTIONS.timeout=30` + `CONN_MAX_AGE=60` + 每次连接注入 `WAL / synchronous=NORMAL / busy_timeout=30000 / foreign_keys=ON`。
- **`DJANGO_ALLOW_ASYNC_UNSAFE=true` 全局打开**（`settings.py:44`）——为了竞速引擎能从 async 里调同步 ORM。代价是 Django 的跨线程 ORM 守卫全进程失效，正确性完全依赖 `run_db` 的纪律。这是有意识的取舍，但值得在 README 里显式声明。
- `CsrfViewMiddleware` 不在 MIDDLEWARE 里；无任何 `SECURE_*`/HSTS。当前纯 header-token 鉴权、无 session cookie，实际风险低，但公网 HTTPS 部署时是短板。
- 日志仅 console handler，无文件/轮转/请求 id 关联格式。容器内 OK，`start.bat` 裸跑就只进控制台。
- `apps.py:ready()` 只做一件事：检测到是服务进程才自动 `migrate`，并把 `AUTO_MIGRATE` 置 0 防 reload 子进程抢写锁。**没有任何后台线程/调度器**。

---

## 六、前端（`frontend/`，Next.js 16 + React 19 + Tailwind）

**零数据层依赖**：无 swr/react-query/axios，无 shadcn——`lib/api.ts`（468 行）自研 fetch 封装，`components/ui.tsx`（484 行）自研原语，图表用 `<div>` 高度百分比手搓。对一个内部控制台来说这套选择是克制的。

值得肯定的工程细节：
- `API_BASE_URL` 用 `??` 而非 `||`，保住 Docker 注入的空串（同源相对路径）。
- 60s `AbortController` + **幂等 GET 自动重试一次、写操作永不重试**。
- in-flight GET 去重 Map（按 `path|token|channel|timeout`）。
- 渠道切换用 `<main key={channel}>` 整页重挂载，配合 `setChannel` 的 no-op 守卫防 `loadChannels→setChannel→event→loadChannels` 死循环。
- 源码里**零 `any`**，后端聚合字段显式 nullable + `num()`/`fmtNumInt()` 兜底防白屏。
- 竞态处理：请求序号 ref 丢弃过期响应、轮询 in-flight 守卫、`document.hidden` 暂停。
- chat 页 SSE 绕开通用 `request()`（它会 `.text()` 缓冲），手写 `ReadableStream` 解析 + 50ms 节流。

问题：
1. **`401 || 403` 一律清 Token 跳登录**（`lib/api.ts:132`）。任何业务性 403 都会把管理员踢下线。应只对 401 生效。
2. 认证头构造在 `api.ts:99-102` 与 `chat/page.tsx:122-127` **两处重复**，SSE 路径是漂移高发点。
3. 表格机制三份拷贝：`toggleOne/toggleAll/invertSelection` + `RENDER_WINDOW=200` 窗口化在 keys/proxies/models 三页近乎逐字重复；每个 CRUD 页各自实现 `load()` 的 loading/error/try/finally。缺 `useResource`/`useTableSelection`。
4. `proxy-groups` 的保存**没有 submit guard**（`:43-59`），双击可建重复分组；`api-keys` 两个模态共用一个 `saving` 标志。
5. 无 `loading.tsx`；`Modal` 无焦点陷阱；`channel-switcher` 下拉纯鼠标（无 `role=menu`/方向键）；12 处 `confirm()`/`prompt()` 与自研 Modal 体系混用。
6. 巨石页面：dashboard 755 / proxies 693 / keys 598 / channels 556 行。
7. `lint` 脚本 = `tsc --noEmit`，**仓库里没有任何 ESLint 配置**，所以源码里那些 `eslint-disable react-hooks/exhaustive-deps` 注释是无政府状态的装饰。

---

## 七、测试

16 文件 / 7584 行 / **473 用例全绿**，无 skip、无 xfail。149 处 mock 全部落在 I/O 边界（httpx、上游响应），不是断言造假。

对照 spec 第六十/六十一节逐条核验，覆盖是**真实存在**的：

| 要求 | 覆盖 |
|---|---|
| RPM 限流并发安全 | ✅ `test_keys.py:120-135` TransactionTestCase + 20 线程 + rpm=5 断言恰好 5 次领取成功 |
| 代理启用上限 | ✅ `test_proxies.py:54-92`（n−1 精确、禁用永远允许、渠道隔离）+ `:154-183` 4 线程并发启用 |
| 线路构建 | ✅ `test_balancer.py` 11 例，含 5Key/4代理→5线路、10Key/20代理→10、排除语义、claim 失败回填 |
| 竞速胜出 + 取消败者 | ✅ `test_race.py:110-122` 断言 winner 下标与 losers 的 `CancelledError` 标志 |
| 流式 winner + 透传 | ✅ `test_race.py:126-163` + `test_transport_passthrough.py`（逐字节、不伪造 `[DONE]`、未知事件不丢）+ 静默截断 5 个 async 用例 |
| 三协议互转 | ✅ `test_anthropic.py` 14 例、`test_channels.py:752-1037` Responses 双向翻译 ~15 例 |
| 额度/配额 | ✅ `test_quota.py` 13 例含 2 个并发；缓存 token 不计费、402、原子领取/退还/负数钳制 |
| 思考解密与透传 | ✅ 最深的一块：`test_reasoning_passthrough.py` 47 例 + `test_thinking.py` 48 例（含 `TransactionTestCase` 线上报文级） |

**缺口（诚实列出）**：
1. `/v1` 数据面**用户 Key 无效 → 401** 这条路径无断言（实现存在于 `openai_views.py:143-145,354-356`，测的都是 admin/metrics 的 401）。
2. `Proxy.password` 落库加密无直接测试（Key 侧有）。
3. `test_keys.py:116-117` 是一个 `pass` 空壳同名测试——真测试在 `:120` 存在，所以无害，但属于假阳性诱饵。
4. 工作树里新增的**竞速心跳注入**与**客户端断开强制结算**两条路径无专属测试（最接近的 `test_ops.py:324-595` 只覆盖 `_drain` 心跳）。
5. `anthropic_count_tokens` 端点只有 helper 单测，没有端点级测试——所以缺陷 #1 逃过了 473 个用例。

---

## 八、部署与仓库卫生

### 8.1 部署

- `Dockerfile` 两阶段构建正确，`npm ci` 锁前端依赖；**但无 `USER` 指令（root 运行）**、无镜像内 HEALTHCHECK（只在 compose 里）。
- `requirements.txt` **13 行全是 `>=`，零上界、零 lockfile** —— 生产构建不可复现，`cryptography`/`httpx`/`Django` 任一 minor 升级都可能静默改变 Fernet 格式或上游传输行为。且 `pytest`/`pytest-django` 混在运行时依赖里进了生产镜像。
- CI 三个 job（backend pytest / frontend typecheck+build+产物断言 / docker build push:false）——**没有 lint、没有安全扫描、从不实际起容器打 `/healthz`**。
- **无周期任务**：`ready()` 不启调度器，日志清理只在容器启动 CMD 里跑一次（`Dockerfile:24`），`start.bat` 连这一步都没有。长期不重启的实例 `RequestLog` 无限增长。
- `/healthz` 把存活绑定到 DB 可达（`health_views.py:39-40`）：SQLite 瞬时锁 → 503 → 容器重启，写压力下是自伤路径。

### 8.2 仓库卫生与安全 ✅

- **`backend/.mimosa/` 仍有 4 个文件被 git 跟踪**（`git ls-files` 实证：finding-ledger events ×2、hook-state、hook-status）。commit `0fbeed8` 声称"移出版本库并 gitignore"，但 `.gitignore` 不会取消跟踪。需要 `git rm --cached` + 提交。内容含本机绝对路径，未发现密钥。
- **`data/gateway.log` 未被忽略**：`.gitignore:8-11` 只写了 `data/*.sqlite3*`，`data/` 整体处于 untracked 状态，一次 `git add data/` 就会把日志提交进去。
- **磁盘上有真实密钥，但从未进过 git**（`git log --all -- config.json` 为空）：根目录 `config.json` 含真实第三方 provider Key；`backend/.cache/debug-body/` 是捕获的上游 4xx 真实报文。都已被 ignore，风险仅在误 `-f` 添加或打包外传。建议轮转 + 移出仓库树。
- 跟踪文件密钥扫描干净：只有 `nvapi-xxxx`、`sk-nvidia2api-xxxx` 这类占位符和测试假数据。
- 根目录 9 个 `diag*.py` 一次性调试脚本 + 空目录 `fpv-ascii/`，均已 ignore，建议删除。

### 8.3 文档

`README.md` 与 `docs/`（12 篇，含 architecture / race-engine / channels / database / api-openai / api-admin / audit-2026-08 / frontend-review）**基本与当前多通道架构同步**，不是 NVIDIA-only 时代的化石。具体漂移：
- `README.md:91` 写"337 个测试"，实际 473。
- `docs/api-openai.md:127-131` 错误码表缺已提交的 `upstream_content_rejected`。
- `docs/architecture.md:5` 定位句仍写"面向 NVIDIA API 的聚合代理平台"。
- `docs/deployment.md` 描述已删除的 frontend 容器（历史遗留）。

### 8.4 在途工作（工作树 +402/−20）

三组内聚改动，全部在 473 绿之内：
1. `POST /api/admin/keys/cleanup-invalid` 端点 + 前端按钮 + 3 测试 —— 完整。
2. 内容拒绝分类消费（终止无谓重试、502 如实分类）+ 竞速窗口心跳 + 客户端断开强制结算 —— 前两项完整，**后两项缺测试**。
3. 设置页 `filterVisible` 同时作用于 GET 与 PATCH 回填 —— 修的是"保存后隐藏参数重新冒出"，完整。

---

## 九、缺陷清单（按严重度）

### P0 / P1

**#1 `POST /v1/messages/count_tokens` 每次调用永久吞掉 1 token 额度** ✅ 已复核
`openai_views.py:662-671`：`_authorize()` 内部执行 `claim_quota` 预占 1 token，但该视图直接 `return JsonResponse(...)`，**既不走 `record_usage` 结算，也不退还预占**。对设了 `quota` 的用户 Key，Claude Code 类客户端每轮上下文计数都在慢性扣额度，且日志无对应 RequestLog，排查时完全不可见。
修法：`_authorize` 增加 `reserve=False` 参数（或该端点改用 `check_quota` 只读校验），或返回前 `record_usage(user_key, reservation=1)` 退还。
测试缺口正是它存活的原因——补一个端点级断言 `used_quota` 不变的用例。

**#2 `thinking.to_upstream` 就地改写客户端请求体** ✅ 已复核
`thinking.py:605-608`（及 `626-627`）：`out["reasoning"] = spec.raw_reasoning` 后紧接 `out["reasoning"]["budget_tokens"] = spec.budget`。而 `spec.raw_reasoning` 就是客户端 body 里那个 `reasoning` 字典对象本身（`_parse_reasoning` 在 `:381` 直接 `return eff, enabled, value`，`_flatten` 在 `:501` 原样赋值）。
后果：网关合成的 budget 值**回写进了客户端原始 body**。由于 `build_upstream` 在 `openai_views.py:435` 先于审计取值执行，`client_thinking`（`:450-456`）与 `_request_summary(body,...)`（`:474`）记录下来的"客户端实际发了什么"被污染——恰是这套诊断系统存在的意义所在。
修法：`out["reasoning"] = dict(spec.raw_reasoning)`。

**#3 管理端 `?reveal=1` 解密全量上游 Key，无审计、无二次因子**
`keys.py:80-82`。单个静态 `ADMIN_TOKEN` 一旦泄漏 = 整个上游 Key 池明文外流。缓解只有启动凭据门禁。
建议：reveal 操作写审计日志（复用 RequestLog 或新表）、或引入独立的更高权限密钥。

**#4 单进程假设未契约化**
见 2.1。建议：`ready()` 里检测 `WEBWORKERS>1`/多进程指纹直接拒绝启动，并在 README 显著位置写明；长期方案是把两个全局计数器搬到 Redis/DB。

### P2

**#5 依赖零锁定** —— `requirements.txt` 全 `>=`，无 lock。至少 `pip-compile` 出 `requirements.txt` + `requirements-dev.txt` 分离。

**#6 零丢失原则的口径分叉** —— `responses_api._content_to_text:301` 丢弃非文本块 vs `message_shape._content_to_text:34` JSON 保留。同名不同义是未来丢数据的温床。统一到一个模块并显式命名语义。

**#7 `Proxy.password` 长度列存密文** —— `models.py:242`，与已修的 `ChannelKey.api_key` 同类，PostgreSQL 迁移即爆。补一个改 TextField 的迁移。

**#8 列表接口无分页** —— keys/proxies/models/user-keys。2000+ 行时响应体与内存双压。

**#9 错误信封三套并存** —— 含 `logs.py:69` 的 `error` 为字符串。收敛到一个 `openai_error`/`admin_error` helper。

**#10 无周期 housekeeping** —— 日志保留、Key/代理冷却推进、健康复检都只在请求或人工操作时被动发生。需要一个 `manage.py` 周期任务或容器内轻量循环（注意别破坏单进程假设）。

**#11 `model_registry.resolve()` 每请求重建索引** —— `:119-120` 缓存了候选列表却没缓存派生索引；`resolve_in_channel`（`:130-148`）每请求 2 查询 + 全量 Python 扫描，完全无缓存。这是每个 `/v1` 请求都走的热路径。

**#12 `bulk_import_keys` 为去重解密全部存量 Key** —— `:33-40`，O(m) 次 Fernet；`upstream_service.sync_models` 逐模型 `get_or_create`（`:98-104`）N+1 写；`proxy_service.bulk_import_proxies` 逐行 `.exists()`（`:111-113`）。都是管理端一次性操作，量大时卡顿。

**#13 事件循环上的同步调用残留** —— `race_engine` 在循环里直接 `sysconfig.get`（`:222,224,459,733`，靠 3s TTL 兜住）与 `decrypt_secret`（`:173-182`）；`proxy_checker.check_all` 在 async 函数里 `Proxy.objects.all()`（`:109`）。当前都不致命，但 `DJANGO_ALLOW_ASYNC_UNSAFE=true` 让这些违规**永远不会被框架抓到**。

### P3

- `reasoning_decrypt.py:71` `except (InvalidToken, ValueError, Exception)` —— 元组里的 `Exception` 让前两项失去意义，等于吞掉一切编程错误。
- `key_service._cooldown_for` 硬编码 429→60s、`invalid_response`→30s（`:302-304`），与"参数全可配"的整体信条不一致；`tool_alias.MAX_NAME_LEN=60`、`proxy_checker` 并发 20、探针超时同理。
- `crypto.mask_secret` 与 `key_service.mask_key` 逐字重复（docstring 自己承认）。
- `tool_stream.py:117-118` 死分支（`state.args` 永不为 None）；`tool_alias` 里 `visit()` 闭包两处拷贝、`changed[0]` 设而不读。
- `anthropic_api.py:226-227` ✅ 已复核：把所有消息的 thinking 块拼进**body 级**单个 `reasoning_content`，丢失逐消息归属——多轮 extended-thinking 历史的潜在正确性问题。
- `anthropic_api.py:485`：`stop_reason` 之后把 OpenAI 原始 chunk 直接塞进 Anthropic SSE 流（零丢失优先于协议严格，但严格客户端会炸）。
- `responses_api.py:40` `max→high` 静默降档；`:279` 硬编码 `include=["reasoning.encrypted_content"]`；`:678-681` 缺 `finish_reason` 被判 `incomplete`。
- `race_engine.py:242`、`responses_api.py:348` 等 `except Exception: pass`（零丢失设计的一部分，但会连带吞掉真 bug）。
- `sysconfig.get` 未知 key 抛 `KeyError`（`:194`）——调用点打错字就是 500；`_cast` 失败静默回落默认值且不留日志。
- 前端：`proxy-groups` 缺 submit guard、`Modal` 无焦点陷阱、`colSpan={50}/{100}` 魔法数、`keys/page.tsx:148` 用 `!includes("••")` 猜"这是不是脱敏值"来决定是否提交编辑后的 Key（含 `*` 的真 Key 会被误丢）。

---

## 十、架构判断与建议优先级

### 与 `audit-2026-08.md` 的关系（诚实标注新增项）

上一轮审计（844 行）已覆盖：默认凭据出厂值、`?reveal=1` 明文回读上游 Key、管理 API 分页形状（当时判定"个人项目收益低，不强制"）、单进程事件循环被 SQLite 写锁卡死的主链修复。本轮**新增**的是：

- 缺陷 #1 `count_tokens` 额度泄漏（08 审计全文未出现 `count_tokens`）
- 缺陷 #2 `thinking.to_upstream` 就地改写客户端 body（未出现 `raw_reasoning`）
- 缺陷 #4 单进程假设未契约化 —— 且 08 审计 `:593` 把 `uvicorn --workers N` 当作**吞吐提升建议**，恰好是本轮判定为风险的那条路径
- 仓库卫生：`backend/.mimosa/` 4 文件仍被跟踪、`data/gateway.log` 未被 ignore 覆盖
- 测试基线从 08 审计收尾时的 **237 passed** 增长到本轮实跑的 **473 passed**（+100%），但覆盖缺口（数据面 401、`count_tokens` 端点级、`Proxy.password` 落库加密）随之暴露——用例增长集中在协议透传与思考链路，管理面与计费边界的盲区没被填

**做对了的（不要动）**
1. 竞速是真并发，且"完成≠成功"的判定顺序正确；取消与 fd 回收被当成一等公民。
2. SQLite 并发策略成体系：条件 UPDATE + `F()` + `run_db` 卸载 + WAL/busy_timeout + 3s TTL 缓存 + 写锁串行化技巧。没有假装它是 PostgreSQL。
3. 透传纯度：网关只做协议翻译与形态钳制，不发明客户端意图、不伪造 `[DONE]`、不静默丢字节。
4. 记账诚实：pending→success 的时机、流内 error 帧入账、截断分类、额度预占/退还——都在努力让日志反映真相。
5. 事故驱动注释（req_id + 日期）让每个非平凡分支都有存在理由，这是可维护性的真来源。

**结构性债务（需要决策，不是修 bug）**
1. **单进程假设** → 要么显式禁止多进程并写进文档/启动断言，要么把闸门外置。当前状态最危险：看起来能水平扩展，实际会静默失去保护。
2. **`openai_views.py` 1256 行** —— `_run_authed` ~200 行、`_stream_response` ~445 行。流式收尾/重试/心跳/结算这套状态机应该下沉成独立模块（如 `services/stream_orchestrator.py`），视图只留协议入口。现在 `AdminChatView` 还在重复一份竞速管道，是下一次漂移的来源。
3. **`responses_api.py` 1397 行** —— 按方向拆 `responses_up/down`。
4. **零丢失原则需要一份单一规范文档** —— 现在它散落在十几个文件的注释里，且实现口径已有三套。写清"什么情况下允许降级、什么情况下必须原样带过"，否则每个新协议出口都会重新解释一遍。
5. **思考能力表硬编码在 Python**（`thinking.py:135-197`）而其余一切按渠道可配 —— 新增模型族要改代码。

**建议执行顺序**
| 序 | 动作 | 成本 |
|---|---|---|
| 1 | 修 #1 count_tokens 额度泄漏 + 补端点级测试 | 10 分钟 |
| 2 | 修 #2 `dict(spec.raw_reasoning)` 拷贝 + 断言客户端 body 未被改 | 5 分钟 |
| 3 | `git rm --cached backend/.mimosa/...`、`data/` 整体入 ignore、删 `diag*.py`/`fpv-ascii` | 10 分钟 |
| 4 | `pip-compile` 锁依赖 + 拆分 dev/runtime requirements | 30 分钟 |
| 5 | 单进程假设：启动断言 + README 声明 | 30 分钟 |
| 6 | `Proxy.password` 改 TextField 迁移；列表接口分页 | 2 小时 |
| 7 | reveal 审计日志；错误信封统一 helper | 2 小时 |
| 8 | 周期 housekeeping（日志清理 + 冷却推进） | 3 小时 |
| 9 | `_stream_response` 下沉为服务模块，AdminChat 复用 | 半天 |
| 10 | 零丢失规范成文；`_content_to_text` 归一 | 半天 |

---

## 附：规模快照

| 维度 | 数值 |
|---|---|
| 后端 Python | ~19.7k 行（含测试 7.6k） |
| 服务层 | 25 文件 / ~6.9k 行 |
| API 层 | `openai_views.py` 1256 行为最大单文件 |
| 测试 | 16 文件 / 473 用例 / 全绿 / 56s |
| 前端 | 10 个控制台页面 + 3 个共享组件；最大页 755 行 |
| 运行时参数 | ~25 个，按渠道隔离，热生效 |
| 对外协议面 | OpenAI chat / OpenAI Responses / Anthropic Messages（+ count_tokens），均支持 `/v1` 与 `/c/<slug>/v1` 双前缀 |

---

## 十一、修复执行记录（2026-09-05 同日）

审查后按第九、十节清单落地。**全量回归 527 passed / 0 failed**（基线 473 → +54 用例），前端 `tsc --noEmit` 干净。

### 已修

| # | 项 | 改动 | 守卫 |
|---|---|---|---|
| 1 | `count_tokens` 额度泄漏 | `_authorize(request, consume_quota=...)`；该端点改走只读 `check_quota`（**闸门保留，消耗归零**） | `test_quota.CountTokensQuotaTests` 5 例（含"额度耗尽仍 402"、"生成路径仍预占"反向守卫） |
| 2 | `thinking` 就地改写客户端 body | `_flatten` 存 `dict(raw)` + `to_upstream` 再 `dict(...)` 双保险；顺带把 `_GATEWAY_EFFORTS` 提为模块常量 | `test_thinking.ClientBodyPurityTests` 3 例 |
| 3 | 单进程契约未强制 | 新增 `services/process_guard.py`（OS 级文件锁 `data/.gateway.lock`，PID 写在锁定区间之后以避开 Windows `LockFile` 连读都拒的语义），`ready()` 服务进程上强制，`ALLOW_MULTI_PROCESS=true` 放行 | `test_process_guard.py` 10 例，含**真实跨进程子进程拒绝**与 `_is_server_process` 命令行形态矩阵 |
| 4 | 依赖零锁定 | `requirements.txt` 全量加上界 + 实测版本注释；`tiktoken` 从隐式可选提为正式依赖；pytest 移到 `requirements-dev.txt`；CI 与缓存路径同步 | — |
| 5 | `Proxy.password` 列宽 | 改 `TextField`，迁移 `0022` | `test_proxies.ProxySecretStorageTests` 5 例（含二次 save 不双重加密、270 字符长密码） |
| 6 | 错误信封四形态并存 | 新增 `api/errors.py`（`admin_error` + DRF `EXCEPTION_HANDLER`），**47 处调用点**收敛为同一信封；`logs.py` 的 `error` 为字符串的离群点已修；DRF 校验失败从"前端只能显示 HTTP 400"变成带 `param` 的可读消息 | `test_error_envelope.py` 11 例（含"非 APIException 绝不吞掉"） |
| 7 | `?reveal=1` 无审计 | 新增 `SecretAccessLog` 模型（迁移 `0023`）+ `services/audit_service.py` + `GET /api/admin/audit/secret-access`。只记动作与来源，**绝不记明文**；写失败不影响请求 | `test_audit.py` 8 例 |
| 8 | P3 快修 | `reasoning_decrypt` 的 `except (InvalidToken, ValueError, Exception)` 拆成"预期内静默 / 预期外留痕"；`_fernet_for` 先查缓存再算密钥；`mask_key` 委托 `crypto.mask_secret` 单一来源；`tool_stream` 删除永不成立的 `None` 分支并把三元式改写成显式分支；`sysconfig.get` 未知参数名给出最接近的已注册参数提示 | `test_transport_passthrough.ToolStreamArgsAccumulationTests` 6 例矩阵 |
| 9 | 仓库卫生 | `git rm --cached` 取消跟踪 `backend/.mimosa/` 4 文件；`data/` 整体入 `.gitignore`（原来漏 `gateway.log`）；删除 9 个 `diag*.py` 与空目录 `fpv-ascii/` | — |
| 10 | 文档漂移 | README 测试数 337→515→527、新增「单进程契约」小节、依赖安装命令改双文件；`docs/api-admin.md` 补错误信封/审计端点/`cleanup-invalid`；`docs/api-openai.md` 错误码表补全（含 `upstream_content_rejected` 与流式帧错误码）；`docs/architecture.md` 定位句改为多通道 | — |

### 过程中被实测推翻的两处判断（诚实记录）

1. **`Proxy.save()` 双重加密**——读代码时怀疑 `save()` 缺 `enc:v1:` 幂等检查会导致 PATCH 时密文套密文。实测 `crypto.encrypt_secret` 本身幂等（`crypto.py:48` 已判前缀），**不是缺陷**。已把它写成守卫用例（`test_second_save_does_not_double_encrypt`）而不是报成 bug。
2. **`tool_stream` 尾巴重复增量**——给累计逻辑补矩阵测试时，我原本断言"连续两帧相同尾巴增量"应被去重，测试失败后确认这是**不可单方面判定的语义歧义**（丢弃会误伤 `{"a":"xx","b":"xx"}` 这类合法重复值），且不在实测畸形形态清单里。改为用 `test_known_limit_duplicate_tail_delta_is_ambiguous` 把这个边界钉在文档上，不改行为。

### 审查后新发现（原报告未含，需决策）

**存量明文敏感字段**：加密在 `save()` 里做，意味着**加密功能上线前写入的历史行永远是明文**，而 `decrypt_secret` 有明文回落所以运行期看不出问题。实测当前库：

```
channel_key    1262 条：616 密文 / 346 明文
proxy          1212 条：712 密文 / 500 明文
```

即 AGENTS.md §三十九.4「代理用户名密码加密保存」实际是**部分失效**状态。已提供 `manage.py encrypt_secrets`（默认 dry-run、`--apply` 才写、分批 500、幂等、可按渠道收窄）+ 6 个守卫用例，**但未对生产库执行**——这是不可逆的批量数据改写，需要明确授权。

### 未做（清单第 9、10 项，需要单独决策）

- **`_stream_response` 下沉为服务模块**（~445 行状态机 + `AdminChatView` 复用）：这是全项目最 delicate 的一段，且工作树里还有未提交的竞速心跳/断开结算改动。在没有先把那两条新路径补上专属测试之前动它，等于在最关键的链路上做无网重构。建议顺序：补测试 → 再抽模块。
- **零丢失规范成文 / `_content_to_text` 归一**：`responses_api._content_to_text`（丢弃非文本块）与 `message_shape._content_to_text`（JSON 降级保留）语义分叉仍在。归一需要先决定"哪个出口允许降级、哪个必须原样带过"，这是设计决策不是 bug 修复。
- 列表接口分页、周期 housekeeping、`model_registry.resolve()` 每请求重建索引、`/healthz` 与 DB 耦合、容器 root 运行、`anthropic_api` 的 body 级 `reasoning_content` 拼接——均维持原报告分级，未动。

### 部署侧注意

- 迁移 `0022`/`0023` 已在验证过程中应用到 `data/db.sqlite3`（`integrity_check ok`，各表行数无异常）。两者均为附加式（列类型放宽 + 新表），对当前运行中的旧代码无影响。
- **正在跑的实例是改动前的代码且不持锁**；重启后新代码才会拿 `data/.gateway.lock`。重启前请确认没有第二个实例挂同一个 `data/` 卷。
