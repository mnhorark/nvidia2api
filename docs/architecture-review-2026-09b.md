# nvidia2api 架构复审 · 第二轮（2026-09-07）

> 基线：当前工作树（31 个已修改 + 3 个未跟踪，`git diff --shortstat` = 1236 insertions / 353 deletions）。
> 前一份是 `docs/architecture-review-2026-09.md`（2026-09-05，基线 `db612ef`，461 行，含第十一节修复记录）。
> 本轮不是重述它 —— 它有三条结论已被后续提交推翻、有一条被本轮**加强到相反的方向**。
>
> 方法：三路并行模块深审（协议层 / API 与鉴权 / 基础设施与部署）+ 主线我自己走。
> **所有 P0 与关键 P1 我都做了独立一手复核**，标注见每条末尾。
>
> 验证标记：`[我]` 我亲自读源码/跑命令确认 · `[复]` 子代理报告、我复核了关键代码路径 · `[推]` 逻辑推演，未构造触发

---

## 一、结论摘要

核心链路（竞速、限流、透传、记账）的设计质量仍然是这个项目最高的部分，09-05 那份"核心可信、外围欠账"的判断成立。
但本轮挖出一件当时没看见的事，它改变了整个系统的风险形状：

> **`ASGI_THREADS` 是死配置，而全项目的每一个入口都是同步视图。**
> 结果不是"admin 面共用一条线程"，而是**数据面 + 管理面 + 健康检查全部串行在同一条 OS 线程上**。
> 叠加本轮把三条判死超时与非流式 read 全部改成 0（不限制），
> **一条挂死的上游请求 = 整个网关无响应**，包括 `/healthz`。

09-05 那份报告把单进程假设列为"最想强调的结构性风险"，方向对，但把故障域写小了：
它担心的是"有人加 `--workers 4` 会静默失去保护"，而实际更近的危险是**当前单进程部署下，
一条线程就是全部**。`config/asgi.py` 里那段声称把线程池抬到 64 的注释，
描述的是一种根本不存在的执行器形态。

第二条主线：**记账诚实度在流式/异常路径上有系统性漏洞。**
`AdminChatView` 把中途暴毙的流记成 `success`；数据面视图的早退路径永久吞 quota 预占且不落日志；
630 条 `pending` 行（最早 9 天前）在拉低成功率与平均延迟。
这套系统最用心的地方就是"让日志反映真相"，而恰恰是这条在异常分支上漏了。

第三条：**"零丢失"原则现在有 6 套实现口径**（09-05 说 2 套），
其中 `responses_api` 内部自己就有两个互相矛盾的函数。
同一条上游流走 `/v1/chat/completions` 不丢字节、走 `/v1/responses` 或 `/v1/messages` 会丢。

一句话：**设计没问题，边界条件没契约化。** 本轮缺陷清单里没有任何一条需要重构，
但 P0 那五条都在"用户看到的数字是错的 / 服务会整体不可用"这一档。

**测试基线**：`python -m pytest tests -q` → **567 passed / 0 failed**（20 文件、142 类）。
`npx tsc --noEmit` 干净，`next build` 13 条路由全部静态预渲染。

---

## 二、系统全景（更新版）

### 2.1 部署拓扑

```
单一容器（all-in-one）
├─ 构建期：node:22-slim → Next.js 16 output:"export" → /app/static/frontend
└─ 运行期：uvicorn config.asgi:application（单进程，无 --workers）
           ├─ /v1/*  /c/<slug>/v1/*   数据面（OpenAI chat / Responses / Anthropic Messages + count_tokens）
           ├─ /api/admin/*            管理面（静态 Token）
           ├─ /healthz  /metrics      可观测性
           └─ /*                      Django 视图同源托管前端产物
           SQLite WAL @ ./data:/app/data
```

对外协议面 5 个端点 × 2 个前缀 = 10 条数据面路由；管理面 34 条（`api/urls.py`）。

### 2.2 线程模型 —— 本轮最重要的更正

```
uvicorn 事件循环（1 条）
   └─ Django ASGIHandler._get_response_async
        └─ 同步视图 → sync_to_async(view, thread_sensitive=True)
             └─ asgiref SyncToAsync.__call__
                  └─ executor = single_thread_executor      ← ThreadPoolExecutor(max_workers=1)
```

一手证据：

- `asgiref/sync.py:402` `single_thread_executor = ThreadPoolExecutor(max_workers=1)`；
  `:481` 在 `thread_sensitive=True` 且无 `ThreadSensitiveContext` 时选中它；
  `:470` 即便有 context，新建的也是 `max_workers=1`。**thread-sensitive 路径上没有任何一处能 >1。** `[我]`
- Django `ASGIHandler._get_response_async`：`if not iscoroutinefunction(wrapped_callback): wrapped_callback = sync_to_async(wrapped_callback, thread_sensitive=True)` `[我]`
- `inspect.iscoroutinefunction` 实测全部入口：`chat_completions` / `responses` / `anthropic_messages` /
  `anthropic_count_tokens` / `list_models` / `AdminChatView.get` / `AdminChatView.post` /
  `liveness` / `admin_health` / `metrics` → **全为 False** `[我]`
- `grep -r ASGI_THREADS <asgiref 包目录>` 与 `<django 包目录>` → **各 0 命中** `[我]`

唯一逃出这条线程的是流式生成器的**迭代体**：`_stream_response` 是 async generator，
`StreamingHttpResponse` 检测异步迭代器置 `is_async=True`，`ASGIHandler` 用
`async with aclosing(aiter(response))` 在事件循环上消费 `[我]`。
所以同步视图**构造**响应对象（鉴权、解析、`build_routes`、上游预留、落 pending 日志）
占住那条线程，之后才释放。

**推论（重要且反直觉）**：新起的流式请求同样进不去 —— 因为它的入口视图也是同步 `def`。
只有**已经在途**的流不受影响。 `[推]`（前提由上面三条一手事实支撑）

`config/asgi.py:6-11` 的注释说"asgiref 的 thread-sensitive 执行器默认只有
`min(32, cpu+4)` 个线程"——那是 `thread_sensitive=False` 时 `loop.run_in_executor(None, ...)`
的默认池大小，同步视图永远走不到那条分支。注释描述的是错的池子。 `[我]`

### 2.3 分层与依赖方向

```
api/（views）──► services/ ──► apps/core/models（ORM）
                     ▲                │
                     └────────────────┘  ← 唯一倒挂：models.save() 调 services.crypto
```

`grep` 实证：`services/` 内**零**处 import `api`；`apps/` 内只有 `models.py:5 → services.crypto`
与两个 management command。依赖方向干净，和 09-05 一致，未恶化。 `[我]`

服务层内部耦合度（import 计数）：`race_engine` 13、`responses_api` 5、`key_service` 4 ——
`race_engine` 是最耦合的模块，与它"系统心脏"的地位相称，但也意味着它的异常路径影响面最广。 `[我]`

---

## 三、缺陷清单

### P0

#### A1 `ASGI_THREADS` 是死配置，且它掩盖了"全部入口共用一条线程"

见 §2.2。两个后果叠在一起才致命：

1. 吞吐上限：每个请求的同步段（鉴权 + 解析 + `build_routes` 的若干条 SQL + 上游预留 + 落库）
   串行执行。`build_routes` 单次含多次条件 UPDATE 领取 RPM。
2. 故障域：见 A2。

`config/asgi.py:6-11` 与 `config/settings.py:166-172` 两处注释都记录了"单线程"这件事，
但都把它当成"admin 面的已知代价"，没有一处说明**数据面也在里面**。
我上一轮亲手写进 `settings.py:167` 的那句"冻结所有同步视图——含 /api/admin/* 与 /healthz"
低估了范围，需要改成"含数据面入口"。 `[我]`

#### A2 三条判死超时 + 非流式 read 同时为 0 ⇒ 一条挂死请求冻结整个网关

- `settings.py:172` `UPSTREAM_READ_TIMEOUT=0` → `race_engine.py:143-145` 把 0 映射成 `None`
  → `httpx.Timeout(connect=10, read=None, write=None, pool=None)`
- `sysconfig.py:105-114` `stream_idle_timeout=0`、`stream_content_idle_timeout=0`、`stream_max_duration=0`
- `_drain`（`openai_views.py:1233-1243`）两个档位都是 `if x and x > 0` → 0 短路，**永不抛 TimeoutError**，
  每 20s 发一条 `: keep-alive` 无限循环 `[复]`
- `_race`（非流式）没有任何总墙钟上限；`stream_first_byte_timeout`（默认 180）只作用于流式竞速窗口 `[复]`

组合结果：一条保持 TCP 但零字节的非流式上游 → 永久占住那条唯一线程 →
**所有后续请求（含流式入口、含 `/healthz`）排队**。`docker-compose.yml:11` 探针 5s 超时 →
容器被判不健康。 `[推]`（前提均已一手确认，仅"是否触发 restart"未实测）

流式侧的对应形态：僵尸流累积到 `max_concurrent_requests`（默认 500）后
`_try_acquire_request` 对**所有**新请求返 429 `server_overloaded`，且僵尸流自己不会退出，**不可自愈**。 `[推]`

仍然有界的超时（对照）：connect 10s / 竞速首字节 180s / 心跳 20s / 代理测速 10s /
`/models` 探测 30s / 内容拒绝探针 10+15s / SQLite busy 30s。 `[复]`

#### A3 数据面视图早退路径永久吞掉 quota 预占，且不产生任何 RequestLog

`_authorize`（`openai_views.py:353-385`）默认 `consume_quota=True` → `claim_quota` 原子
`used_quota += 1`。其 docstring `:357-358` 写着"失败路径显式退还"。

但 `record_usage(..., reservation=1)` 只出现在 `openai_views.py:496 / 560 / 570 / 613` ——
**全部在 `_run_authed` 内部**。视图层在 `_authorize` 成功之后、`_run_authed` 之前的早退
一个都没退：

```python
user_key, err = _authorize(request)
if err: return err
body, err = _parse_body(request)
if err: return err          # ← 预占已发生，从不退还
```

同类出口：`_parse_body` 的 400/413（`:637-639`、`:650-652`、`:666-668`）、
`responses` 的 `input is required`（`:654-655`）、`anthropic` 的 `messages is required`（`:670-671`）、
`_run_authed` 的 `model and messages are required`（`:433-435`）、`ChannelNotFound`（`:440-442`）、
`model is None`（`:443-444`）。

这些位置都在 `RequestLog.objects.create`（`:484`）**之前**，所以既吞额度又零留痕。
这正是 09-05 修掉的 `count_tokens` 吞额度缺陷，在生成端点的错误分支上换了个形态复现。
只影响 `quota > 0` 的 Key（`api_key_service.py:120-121` 在未启用额度时直接放行不加数）。 `[我]`

#### A4 `AdminChatView` 把中途暴毙的流记成 `success`，异常对象被完全丢弃

`admin_views/chat.py:193-194` 初始化 `stream_ok=False` / `truncated_stream=False`，
二者只在 `:206-207`（`async for` **正常**结束）赋值。`_drain` 抛异常时 `finally`（`:208`）
看到 `truncated_stream` 仍为 False → `:214` `log.status = "success"`。
异常继续传播到外层 `except Exception`（`:248`），其"已交付"分支（`:250-255`）
只更新 `duration_ms` 后 `_safe_save()` + `return` —— **不改 status、不记 error_type、无 `logger.exception`**。

对照：未交付分支（`:256-258`）确实置了 `failed` + `stream_idle_timeout`/`stream_error`。
所以是"已经吐过字节的那条路"专门会伪装成功。 `[我]`

#### A5 Anthropic 历史思考块：写错作用域 + 被上游过滤删除，注释承诺的能力完全不成立

`anthropic_api.py:223-227`：

```python
# 零丢失：thinking/redacted_thinking 块并入 reasoning_content，
# extended thinking 多轮回路依赖历史思考块原样可达上游
if thinking_parts:
    out["reasoning_content"] = (out.get("reasoning_content") or "") + "".join(thinking_parts)
```

`out` 在这里是**chat body**（同一函数里 `out` 承载 `stop`/`max_tokens`/`tools`/`thinking`，
且 `messages` 是另建的列表），所以：

1. 所有消息的思考块被累加进 body 顶层一个字符串，**逐消息归属丢失**；
2. `reasoning_content` ∈ `thinking.THINKING_PARAM_KEYS`（`thinking.py:22-28`），
   而 `_DROP_FOR_UPSTREAM = thinking.THINKING_PARAM_KEYS | {...}`（`openai_views.py:206`），
   `:235` 的字典推导把它过滤掉。

净效果：注释承诺"原样可达上游"，实际一个字节都到不了。
09-05 报告 §P3 记的是"body 级拼接丢失归属"（形态问题），低估了——后果是**完全丢失**。 `[我]`

---

### P1

#### B1 `AUTO_MIGRATE=0` 会连带关掉单进程守卫

`apps/core/apps.py:33-37`：`_is_server_process()` 第一件事是读 `AUTO_MIGRATE`，
`0/false/no/off` → `return False`。而 `ready()` 在 `:59-60` 用它作为**守卫的前置门**，
`acquire_singleton_lock()` 在其之后（`:65-68`）。

所以这个名义上只控制"迁移时机"的开关，实际语义是"关掉守卫 + 关掉迁移"。
`:32` 的注释只写了后者。更糟的是 `apps.py:78` 代码自己会 `os.environ["AUTO_MIGRATE"]="0"`。 `[我]`

另外 `process_guard.py:7-13` 的"依赖单进程的状态"表**漏了** `dashboard._usage_cache`
（`api/admin_views/dashboard.py:331-347`），README 同一张表同样漏。 `[复]`

#### B2 非流式重试不带排除集，会立刻抽回同一条死线路

`openai_views.py:523-524` 的 `build_routes(channel, proxy_group=..., endpoint=...)`
**没有** `exclude` / `exclude_proxies`；流式路径 `:759-761` 两个都传。
`load_balancer.py:53-57` 的 docstring 明确说这两个参数就是为"避免立刻又抽到同一死线路"设计的。 `[复]`

#### B3 "零丢失"现在有 6 套口径，其中 2 处真丢字节

| 函数 | 位置 | 未知/非文本块 |
|---|---|---|
| `responses_api._content_to_text` | `:301-310` | **丢弃**（只认 text/output_text） |
| `responses_api._message_to_item` | `:358-395` | **丢弃**（list 分支无 else，file/input_audio/refusal 蒸发） |
| `responses_api._input_item_to_message` | `:573-637` | JSON 降级保留（显式保留 file/audio/refusal） |
| `message_shape._content_to_text` | `:34-54` | JSON 降级保留 |
| `anthropic_api._tool_result_text` | `:110-128` | JSON 降级保留（且分隔符是 `\n`，另两处是 `""`） |
| `anthropic_api._blocks_to_content` | `:45-107` | **丢弃**（if/elif 链无 else） |

同一文件内部自相矛盾：`_message_to_item`（丢）vs `_input_item_to_message`（保留）。
触发形态：`[{type:text},{type:file}]` 的 user 消息打到 `/responses` 上游 → file 蒸发，无 400、无日志。 `[复]`

更严重的对等性缺口：`responses_api.py:1203-1204` 与 `anthropic_api.py:449-450` 都是
`except Exception: continue`，把解析失败的 `data:` 帧**从客户端流里删掉**；
而 chat 出口（`race_engine.iter_sse:685-692`）原样 yield。
**同一条上游流走三种协议面，丢不丢字节不一样。** `[复]`

#### B4 `_translate_event` 的 catch-all 把内部 bug 变成协议污染

`responses_api.py:745-746` 顶层 catch-all 返回 `None`，调用方 `iter_responses_sse:1045-1054`
把 `None` 一律解读为"翻译器不认识这个事件"→ 走透传过滤 →
**原始 Responses 事件对象被注入 chat 流**。
翻译器内部任何 `AttributeError` 都表现为客户端流里混进一坨大 JSON，而不是 500。
这正是 `req_df22e4f` 案加两层过滤器要防的形态，现在从异常路径重新可达。 `[复]`

#### B5 `pending` 行滞留污染统计，且两个"平均延迟"口径不同

实测当前库 `request_log` status 分布：`success 27042 / failed 2609 / pending 630 / error 142`；
最早 pending 为 `2026-08-29 08:18`（滞留 9 天）；pending 中 590/630 的 `duration_ms=0`。 `[我]`

- 成功率：`dashboard.py:80,96` 与 `:236` 的分母都含 pending → 系统性压低
- 平均延迟：`DashboardView` 用未过滤的 `Avg('duration_ms')`（`dashboard.py:79-80,97`，含 0 值），
  `DashboardUsageView` 用 `Sum/COUNT filter=~Q(duration_ms=0)`（`:196-197`，排除 0 值）
  → **同一页两个"平均延迟"定义不同，且互相看不见**
- 产生 pending 永不移除的根因：非流式 `_run_authed` 的 `finally`（`:624-627`）
  **只释放信号量与上游额度，不结算 log、不退额度**；流式的 `finally`（`:1131-1149`）有强制 settle。
  两边不对称是 A3/A4/B5 的共同根因。 `[复]`
- 无任何后台对账：`cleanup.py` 只按 `created_at` 年龄删，不结算
- `status='error'` 那 142 行是历史遗留（现行代码只写 success/failed/pending），
  且 `models.py:385` 注释仍写 `# success / error`，与实际取值域不符 `[复]`

#### B6 `claim_rpm_slot` 的 `False` 同时表示"配额耗尽"和"数据库写不进去"

`key_service.py:228-231`：

```python
except Exception as exc:  # noqa: BLE001
    logger.warning("claim_rpm_slot %s failed (swallowed): %s", key_id, exc)
    return False
```

调用方 `build_routes` 无法区分两者。SQLite 写争用期间**所有 Key 看起来都像被限流**
→ `no_available_route` → 503，日志里与真实限流混在一起。
（这条是我在生产库上误跑探针时撞出来的，见 §五 方法失误。） `[我]`

反向 `release_rpm_slot:247` 同样吞异常：claim 成功但 release 撞锁 → 该槽位在本分钟窗口内
**永久虚耗**，正是这个函数（`:235-241` docstring）存在的理由要防的事，在拥塞时以小规模复现。 `[我]`

#### B7 代理一旦被标 `unhealthy`，无人工复检就永久退出调度

`proxy_service.py:233` 的 `schedulable_proxies` 无条件跳过 UNHEALTHY，**不看 `cooldown_until` 是否过期**；
只有 `report_proxy_result(success=True)`（`:187-193`）会恢复 HEALTHY，而它只在人工点测速时触发
（`admin_views/proxies.py:125,136,153`）。
对照：`channel_key` 冷却与 `channel` 熔断都是时间比较式自愈（`key_service.py:163`、`channel_health.py:35`）。
代理这一条是唯一的例外。 `[复]`

#### B8 `secret_access_log` 没有任何清理路径

`cleanup.py:19,47` 只处理 `RequestLog`。审计表单调膨胀，且它是全库增长最慢但永不清理的表。 `[复]`

#### B9 `_stream_response` 生成器若从未被启动，`_active_count` 与 RPM claim 双双永久泄漏

`openai_views.py:505` 在生成器**创建之前**就置 `semaphore_released_by_stream = True`，
于是 `:624-627` 的 `finally` 不再执行 `_bump_active(-1)`；而 `:506` 调用 async generator
函数不执行函数体，退还全押在"生成器被迭代过"。
若 `:515` 的 `StreamingHttpResponse(...)` 抛错或中间件丢弃响应 → `_active_count` 永久 +1。 `[推]`

同类：`_run_authed` 全程无 RPM release 兜底，`:473` 领取后到终态之前的任何非
`(NoRouteAvailable, AllRoutesFailed)` 异常都会漏掉最多 `max_routes_per_request` 个槽位。
最要紧的触发条件恰好是仓库自己写下的 TODO：一旦有人照 `settings.py:167-170` 把数据面视图改 async，
`race_chat` 的 `asyncio.run()` 在已运行循环里必抛 → **每个非流式请求泄漏 8 个槽位**，
表现为各 Key 迅速被误判 `rate_limited`。 `[推]`

#### B10 登录限速可被 XFF 任意绕过

`admin_views/common.py:115-125` 把 `X-Forwarded-For` 首跳拼进桶键，而 XFF 完全由客户端控制；
每请求换一个值即每次落入新桶，`_LOGIN_FAIL_LIMIT=10/60s` 形同虚设。
`:116-121` 的注释自认"不作为防爆破唯一手段"。补充：桶是进程内 dict（`:113`），
且 `_login_fail_exceeded` 只在凭据比对失败后调用（`login.py:49`），比对本身无限速前置。
实际防线只有 `settings.py:219-242` 的默认凭据启动门禁。 `[复]`

---

### P2（维持原分级，本轮复核现状未变者从略）

- **列表无分页**：keys / proxies / models / user-keys / channels / proxy-groups 全量序列化。
  实测当前库：`channel_key` 1319 行 → 模拟序列化 **514769 B**；`proxy` 1269 行 → **717928 B**。
  `request_log` 30360 行但有分页 + `.defer`。GZip 压线上字节约 10×，服务端构造成本仍 O(N)。 `[复]`
- **`/healthz` 不执行 SQL** → SQLite 瞬时锁不会 503（`ensure_connection` 对已开连接是 no-op），
  但反面是 DB 真 wedged 时它仍报 `database: True`，**假健康**。
  而它作为同步视图，会被 A2 的单线程冻结拖死 —— 两个性质并存。 `[复]`
- **`thinking` 能力表硬编码**（`thinking.py:140-202`），`resolve_capability` 签名无 `channel` 参数；
  `sysconfig` 的三个思考相关开关无法表达能力字段。且 `qwen` 条目挂在裸子串上（`:157-174`），
  任何名字含 qwen 的模型都被强制走"预算→档位换算、effort/budget 互斥"；
  表的**顺序是承重的**（`kimi-k3` 必须在 `kimi` 前）但无机制防插错位置。 `[复]`
- **`run_db` 的 `to_thread` 分支零测试覆盖**：`loop_offload.py:43` 在 `settings.TESTING` 时短路回同线程，
  所以 567 个用例一次都没执行过生产线程路径与跨线程 thread-local SQLite 连接。 `[复]`
- **内容拒绝探针不计费、不限流、不入账**（`race_engine.py:368-379`）：用用户的 Key 发真实 `"hi"`，
  不 claim RPM、不建 RequestLog、不 `_mark_*`。 `[复]`
- **依赖无 lockfile**：`requirements.txt` 13 包全有上界但界很宽
  （`Django>=5.0,<7.0`、`cryptography>=42.0,<49.0`），无 hashes；前端有 `package-lock.json` + `npm ci`。
  `django-filter` 疑似未使用（不在 `INSTALLED_APPS`）。 `[复]`
- **无周期 housekeeping**：`ready()` 只做拿锁 + migrate，全仓库无 scheduler/后台线程。
  日志清理三个入口全是被动的（容器启动 CMD 一次 / 管理命令 / 管理 API）。 `[复]`
- **CI 无 lint、无安全扫描、不起容器打 `/healthz`**；前端 job 有产物断言。 `[复]`
- **`_stream_first_valid` 的 CancelledError 分支少关一层 `req_cm`**（`race_engine.py:566-568`），
  其余 4 条退出路径都是两件套。不泄漏 fd（`cm.__aexit__` 关连接池），但 response 对象未显式关闭。 `[复]`
- **`chat_to_responses_payload` 把"无 finish_reason"判为 `incomplete` 且不写原因**
  （`responses_api.py:678-681,691-692`）；`iter_chat_sse_as_responses` 的 `emit_terminator`
  在 `finish` 为假时**仍无条件发 `[DONE]`**（`:1175-1181`），掩盖截断 ——
  而同一时刻 chat 出口正在发显式 error 帧。两个出口对同一事件的告知能力不对等。 `[复]`
- **`anthropic_api:581-589` 重复 finish 帧会覆盖 `stop_reason`**，与紧邻注释"重复 finish 帧忽略"相反。 `[复]`
- **`tool_stream._merge_stream_field` 在"前缀回缩"形态下产出非法 JSON**（`:61-67` + `:123-126`）。
  与 09-05 拒绝修改的"尾巴重复增量"是不同形态，这个无歧义地损坏。 `[推]`（未在任何实测畸形清单里见过）
- **吞异常清单**：14 处属零丢失设计（判定失败即原样透传，处置正确），
  6 处会吞真 bug —— 按危害排序：`chat.py:248`（A4）、`responses_api.py:745`（B4）、
  `responses_api.py:1203` + `anthropic_api.py:449`（B3 真丢字节）、
  `openai_views.py:921/993/1024`（静默关掉调度器学习：坏代理坏 Key 永不扣分）、
  `reasoning_decrypt.py:276`（吞掉 `normalize_reasoning_format` → Kilo/OpenRouter 的
  `delta.reasoning` 不再归一化，客户端看不到思考且无痕迹）、
  `tool_alias.py:125`（别名还原失败 → 客户端拿到内部别名，回传下一轮匹配不到工具）。 `[复]`
- **`_drain` 的超时档位切换表达式不含 `has_error`**（`openai_views.py:1208-1211,1226-1229`），
  而 `stream_pipeline.py:161-163` 里 error 帧会置 `sent_signal=True` 锁死换线重试。
  两个口径在 error 帧上分叉；当前超时全 0 所以无行为差异，一旦有人调大就显形。 `[复]`

---

## 四、与 09-05 那份的差异

### 4.1 旧结论已被后续提交推翻（读旧报告的人需要知道）

| 旧结论 | 现状 |
|---|---|
| §8.2 `backend/.mimosa/` 仍有 4 文件被跟踪 | **已修**，`git ls-files \| grep mimosa` 为空 `[复]` |
| §8.2 `data/gateway.log` 未被 ignore | **已修**，`.gitignore:10` 整目录级，`git check-ignore` 实证 `[复]` |
| §8.1 requirements 全 `>=` 零上界 | **已修**，13 包全带上界 + 实测版本注释；pytest 移到 dev `[复]` |
| §七缺口#1 数据面用户 Key 无效→401 无测试 | **已修**，`test_error_envelope.py:120-136` `[复]` |
| §P0#1 `count_tokens` 吞额度 | **端点本身已修**（`consume_quota=False`），但同一缺陷类在生成端点错误分支复现 = 本轮 A3 |
| §P3 `anthropic_api:226` "body 级拼接丢失归属" | 低估了，实际是完全丢失 = 本轮 A5 |
| §2.1 "单进程假设是最想强调的结构性风险" | 方向对，故障域写小了 = 本轮 A1/A2 |

### 4.2 旧报告列了、至今未修的

`§八` 容器无 `USER`（root 运行）· `§九#8` 列表分页 · `§九#10` 周期 housekeeping ·
`§七缺口#3` `test_keys.py:116-117` 空壳测试（真测试在 `:120`，同名不同类，假阳性诱饵仍在）·
`§七缺口#4` 竞速心跳注入与客户端断开强制结算**仍无专属测试** ·
`§九#13` `proxy_checker.check_all` 在 async 里 `Proxy.objects.all()` 未过 `run_db` ·
`§十` 未做清单第 9、10 项（`_stream_response` 下沉、零丢失规范成文）——
后者本轮从"2 套口径"恶化到"6 套"（B3）。

### 4.3 本轮净新增（旧报告完全没有的）

A1 `ASGI_THREADS` 死配置 · A3 早退吞额度 · A4 AdminChat 伪装成功 · A5 anthropic 思考块完全丢失 ·
B1 `AUTO_MIGRATE` 关守卫 · B2 非流式重试不排除死线路 · B6 claim/release 的 False 语义坍缩 ·
B7 unhealthy 不自愈 · B8 审计表无清理 · B9 生成器未启动的永久泄漏 · B10 XFF 绕过 ·
`_translate_event` 异常→协议污染 · 三协议出口丢字节不对等 · `run_db` 生产路径零覆盖。

---

## 五、验证方法与诚实标注

**做对了的**：所有 P0 与关键 P1 我都独立复核了源码路径，没有直接采信子代理结论。
其中 A1/A2/A3/A4/A5/B1/B6 是我一手确认，B2/B3/B4/B5/B7-B10 是子代理报告 + 我复核关键代码。
标 `[推]` 的三条（A2 的 restart 后果、B9 的生成器未启动、`tool_stream` 前缀回缩）
**未构造实际触发**，不要当成已复现故障读。

**我的一次方法失误**：想量 `build_routes` 的单线程占用时长时，直接在生产库上跑了带
`transaction.set_rollback(True)` 的探针。两个错误：(1) 与正在运行的网关抢 SQLite 写锁，
刷出 90KB `database is locked`；(2) 事务内第一条 claim 失败后整个 atomic 就废了，
后续计时全是垃圾数。写入已回滚、`PRAGMA integrity_check` = ok、`channel_key` 1319 行无异常。
**测量作废**，但正是这次失败暴露了 B6 —— 所以结论留下了，方法不留下。
以后测这类东西只能对临时库，或者在测试事务里跑。

**我自己上一轮引入的债**：`backend/tests/test_frontend_guards.py`（本轮新增的 24 条前端守卫）
有两处脆弱，是子代理在审查基础设施时反过来指出我的：

1. 三处用 `src.index("...")` 定位（`"if (cached) {"`、`"open={!!createdKey}"`、`"ariaLabel="`），
   前端一次无害重排就 `ValueError` 崩测试而不是干净失败；
2. 四条断言用了**未剥注释**的原文（`assertNotIn("const statusLabels", src)` 等），
   前端加一条含这些字样的注释就能让它假通过。

这两条我自己写的时候踩过第 2 类坑（第一版误报 3 例）并修了 `_code()`，
但没把 `_code()` 贯彻到全部断言。已列入待修。

---

## 六、建议执行顺序

| 序 | 动作 | 一手依据 | 成本 |
|---|---|---|---|
| 1 | **A2 止血**：`stream_max_duration` 给一个非 0 默认（如 30min），`UPSTREAM_READ_TIMEOUT` 保留 0 但给非流式加总墙钟 | `_drain` 两档位 0 短路 | 20 分钟 |
| 2 | **A3 退款**：视图层早退统一 `try/except` 退预占，或把 `claim_quota` 从 `_authorize` 挪到 `_run_authed` 内 log 创建之后 | `openai_views.py:634-640` | 30 分钟 |
| 3 | **A4 记账**：`chat.py` 的 `finally` 用"是否抛异常"而非 `truncated_stream` 判定；外层 except 补 `logger.exception` | `chat.py:193-214,248-255` | 20 分钟 |
| 4 | **A1 更正**：删掉 `asgi.py` 的 `ASGI_THREADS`（或改成真有效的 `loop.set_default_executor`）+ 改正 `settings.py:167` 的范围描述 | `asgiref/sync.py:402` | 15 分钟 |
| 5 | **B1 契约**：守卫从 `_is_server_process()` 里解耦出来，`AUTO_MIGRATE` 只管迁移 | `apps.py:33-37,59-60` | 20 分钟 |
| 6 | **A5 决策**：Anthropic 历史思考块要么改成逐消息承载并加入 `_DROP_FOR_UPSTREAM` 豁免，要么删掉那段误导性注释并承认不支持 | `anthropic_api.py:223-227` + `openai_views.py:206` | 需决策 |
| 7 | **B5 对账**：非流式 `finally` 补强制 settle（对齐流式那套）；启动时把超时未结算的 pending 归一为 failed | `openai_views.py:624-627` | 1 小时 |
| 8 | **B6 语义**：`claim_rpm_slot` 区分"耗尽"与"DB 错误"（返回三态或抛特定异常） | `key_service.py:228-231` | 30 分钟 |
| 9 | **B7 自愈**：`schedulable_proxies` 在 UNHEALTHY 且 `cooldown_until` 已过期时放行复检 | `proxy_service.py:233` | 20 分钟 |
| 10 | **B3/B4 收口**：`_content_to_text` 归一为单一模块 + 显式命名语义；`_translate_event` 的 catch-all 改为"记日志 + 明确丢弃"而非返回 None | 6 套口径 | 半天 |
| 11 | **A1 根治**：数据面视图改 async + `race_chat` 去掉 `asyncio.run`（**必须先做第 12 项，否则触发 B9 的槽位泄漏**） | §2.2 | 1-2 天 |
| 12 | 补心跳注入 / 断开结算 / `run_db` 生产线程路径的专属测试 | §七缺口#4 | 半天 |

第 11 项是唯一的结构性改动，也是唯一需要先把测试补齐才敢动的 ——
它同时是 A1 的根治和 B9 的触发器，顺序错了会把"冻结一条线程"换成"泄漏 RPM 配额"。

---

## 七、修复执行记录（2026-09-07 同日）

按第六节顺序落地了 **1–9 项**（第 10、11、12 项需要决策或先补测试，见 7.4）。
**全量回归 587 passed / 0 failed**（基线 567 → +20 守卫），`manage.py check` 无问题，
`makemigrations --check` 无待生成迁移。

### 7.1 已修

| # | 项 | 改动 | 守卫 |
|---|---|---|---|
| 1 | **A2** 僵尸请求无界 | 新增运行时参数 `upstream_total_timeout`（默认 3600）并在 `_race` 的 `asyncio.wait` 上落地；`stream_max_duration` 默认 0 → 3600。**两条静默判死超时按用户要求保持 0** —— 总墙钟不是 idle 判定，持续吐字的慢模型不会被它杀 | `TotalWallClockTests` 3 例（挂死线路被限时结束并记 `total_timeout` / 0 仍是不限制 / 默认值必须为正且 idle 仍为 0） |
| 2 | **A3** 早退吞额度 | 预占从 `_authorize` 拆出为 `_reserve_quota`，由视图在**所有可早退校验之后**调用；`_authorize` 只保留只读 402 闸门（行为不变）。`_run_authed` 内 3 处早退补 `_refund_reservation`。顺带删掉已无意义的 `consume_quota` 参数 | `EarlyReturnQuotaLeakTests` 4 例（畸形 body / 缺 messages / 模型不存在 / 反向"闸门不能被挪走"） |
| 3 | **A4** playground 伪装成功 | `chat.py` 内层加 `except BaseException: aborted=True; raise`，`finally` 三分支结算；收尾 yield 从 `finally` 移出；外层"已交付"分支补 `logger.exception` + 显式 error 帧 + 交付量三列 | `test_stream_dying_after_delivery_is_not_recorded_success` |
| 4 | **A1** 死配置与错误故障域 | 删 `ASGI_THREADS`（asgiref/Django 均不读取）；`config/asgi.py` 重写为记录真实模型（`sync.py:402` 硬编码 `max_workers=1`、所有入口皆同步视图）；`settings.py` 与 `.env.example` 的故障域描述从"admin + healthz"更正为"整个网关，含新起的流式请求" | 源码级说明（无行为可断言） |
| 5 | **B1** 守卫被 AUTO_MIGRATE 连带关掉 | `apps.py` 拆 `_is_server_process()`（只看命令行形态）与 `_auto_migrate_enabled()`；守卫不再受迁移开关影响 | 沿用 `ServerProcessDetectionTests` 6 例（原实现下 `AUTO_MIGRATE=0` 会让守卫消失，现无路径可关） |
| 6 | **B6** claim 语义坍缩 | 新增 `RpmClaimUnavailable`；DB 层判不出来时抛而非 `return False`；`build_routes` 捕获后提前结束本轮并留**可与"配额耗尽"区分**的日志 | `R14_RpmClaimUnavailableTests` 3 例（含反向"真耗尽仍返回 False 不抛"） |
| 7 | **B7** unhealthy 永久判决 | `schedulable_proxies` 退化为标准熔断 half-open：`cooldown_until` 过期即重新 eligible，真死的代理快速失败并被重新冷却。读路径不写状态 | `ProxyUnhealthyRecoveryTests` 4 例（含反向"冷却中仍排除"与渠道级豁免不变） |
| 8 | **B2** 重试抽回死线路 | 非流式重试循环累积 `excluded` / `excluded_proxies` 并传给 `build_routes`，与流式路径同口径 | `R16_NonStreamRetryExcludesDeadRoutes` 2 例（含反向"内容被拒仍不重试"） |
| 9 | **B5 + B9** 非流式无兜底 | 新增 `_force_settle_non_stream`：异常逃逸时结算 pending 日志 + 退额度 + 记 `record_result`；`semaphore_released_by_stream` 交接标志**移到响应构造成功之后**，关掉 `_active_count` 永久泄漏窗口 | `R17_NonStreamForceSettleTests` 3 例 |
| — | 存量 pending | 新增 `manage.py settle_stale_pending`（**默认 dry-run**，`--apply` 才写；分批 500 避免长持写锁；`--older-than` 默认 60 分钟，必须大于任何真实请求时长） | dry-run 实测命中 **629 条** |

### 7.2 过程中被实测纠正的一处（诚实记录）

`R17` 的额度守卫**第一版是空转的**：我直接调 `_run_authed` 断言 `used_quota == 0`，
但预占现在发生在视图的 `_reserve_quota` 里 —— 绕过视图就等于绕过被测对象，
断言恒真。改成走 `chat_completions` 视图并加"预占确实发生过"的前置断言后，
证伪跑才如期失败（`used_quota=1`）。
**教训：守卫必须证明"没有修复时会红"，我这次差点又交一条假守卫。**
本轮 9 项全部做了证伪跑（临时改回旧实现 → 跑 → 还原），结果见 7.1 的守卫列。

### 7.3 未做（需要决策或前置条件）

- **第 11 项（数据面视图改 async + 去掉 `race_chat` 的 `asyncio.run`）**：A1 的根治。
  它同时是 B9 的触发器 —— 本轮已把 B9 的**日志与额度**部分用兜底结算堵上，
  但"改 async 后 `asyncio.run` 必抛"这条路径仍依赖第 12 项的测试先就位。
  在没有覆盖竞速心跳注入与客户端断开强制结算的测试之前动它，等于在最关键链路上无网重构。
- **第 10 项（零丢失规范成文 / `_content_to_text` 六套口径归一）**：设计决策，不是 bug 修复。
  需要先定"哪个出口允许 JSON 降级、哪个必须原样带过"。
- **A5（Anthropic 历史思考块）**：两条路互斥 —— 改成逐消息承载并给 `reasoning_content`
  加 `_DROP_FOR_UPSTREAM` 豁免，或者删掉那段承诺"原样可达上游"的注释并承认不支持。
  选哪条取决于是否真要支持 extended-thinking 多轮回路，这是产品决策。
- **B3/B4（三协议出口丢字节不对等、`_translate_event` 异常→协议污染）**：与第 10 项同源，一并决策。
- **B8（`secret_access_log` 无清理）**、列表分页、`/healthz` 假健康、容器 root 运行：维持原分级。

### 7.4 部署侧注意

- **正在跑的实例仍是改动前的代码**，且持有 `data/.gateway.lock`。
  重启后新代码才会生效；重启前确认没有第二个实例挂同一个 `data/` 卷。
- `settle_stale_pending` **未对生产库执行**（dry-run 显示 629 条）。
  它是对存量数据的批量改写，需要明确授权：
  `python manage.py settle_stale_pending --apply`
- 本轮**无新增迁移**（未改任何模型字段）。

### 7.5 code-review 追加修复（同日，第二轮）

`/code-review` 对本批工作树跑了 5 路并行审查 + 逐条置信度评分，去重 14 条疑点，
过 ≥80 阈值的 2 条**都是本会话自己写进去的**。另有 3 条 70-75 分被阈值卡掉但一并修了。

| 项 | 分数 | 问题 | 修复 | 守卫 |
|---|---|---|---|---|
| F1 | 80 | `_run_authed` 的 `finally` 无条件读 `started`，而它原本只在建日志之后才绑定 → 建日志前的任何异常逃逸让兜底先抛 `UnboundLocalError`，**掩盖原始异常且退款完全不执行**（正是它声称要防的场景）。AST 复验：`started` 首次 Store 在 524、Load 在 700 | 与 `log`/`crashed`/`request_id` 一起在函数顶部预绑定 `started` | `R18_FallbackSettleBeforeLogTests` 2 例 |
| F2 | 80 | 把 `AUTO_MIGRATE` 从 `_is_server_process()` 拆出时，删掉了 reloader 子进程跳过单实例锁的**唯一**豁免通道 → `manage.py runserver` 的子进程与父进程抢同一把锁 → `AlreadyRunning` → **只剩一个不服务的文件监视器**（README:92 与 deployment.md:55 的文档化启动路径失效）。Django 源码复验：`django.setup()` 在 `ManagementUtility.execute` 里对父子进程都执行，只按 `runserver` + 无 `--noreload` 判定，不看 `RUN_MAIN` | 新增 `_is_reloader_child()` 与 `_should_acquire_singleton_lock()`（纯函数，便于断言）；契约不削弱——无 `RUN_MAIN` 的第二个独立实例照常抢锁被拒 | `R19_ReloaderChildGuardExemptionTests` 5 例（含"父进程照常抢锁""第二实例仍被拒""一次性命令不抢""uvicorn --reload 无需豁免"） |
| F3 | 75 | `_mark_timed_out` 只写 report 不调 `_mark_failure` → 挂满总墙钟的线路在调度打分上**完全隐身**：`_score` 按 `(failure_count, lru)` 升序，它 failure_count 不涨、`last_used_at` 停在 1 小时前，于是**同一条僵尸线路被每轮优先抽到**。与 61fdb4a（死线 Key 统计）、911393c 立的不变量直接冲突 | 改 async，逐条 `await _mark_failure(r, "total_timeout", 0)`，与 `first_byte_timeout` 同口径（http_status=0 才会连代理一起记账） | `R20_TotalTimeoutAccountsHealth` |
| F4 | 75 | `stream_idle_timeout` 的新描述把兜底归给"心跳探测"，而该机制已在 `e32bd94` 移除；现在的 `stream_heartbeat_interval` 只向**客户端**发 keep-alive，对上游死亡零判定能力。运维信了就不会收紧配置，可占满整条线程一小时。另 `stream_pipeline` 把观测列口径指向 `docs/database.md`，实际写在 `docs/race-engine.md` | 描述改为点名唯一真实兜底 `stream_max_duration` 并显式警告心跳不是判死机制；交叉引用改对 | — |
| F6 | 40→已修 | 429 server_overloaded 出口在 try 之外，走不到 finally 兜底 → 拥塞期每个被拒请求永久吞 1 token 且不落日志（**存量**，旧实现同样漏），使 _authorize 新写的「泄漏面收敛为 0」成为假陈述 | 该出口显式 _refund_reservation，让 docstring 的断言变成真的 | R21_ServerOverloadedRefundsTests |
| F5 | 70 | `test_generation_path_still_reserves_quota`（d3bc0a3 立的反向守卫）在本批重构后**空转**：`used_quota` 无论有没有预占都会是 0（失败退款打成 -1 再被负值归零钳住），`total_requests` 来自 RPM | 改为**直接观测 `claim_quota` 是否被调用**，不靠结果值反推；并补配对的"count_tokens 绝不 claim"守卫 | 两条，证伪跑确认删除 `_reserve_quota` 后变红 |

**5 条全部做了证伪跑**（临时改回旧实现 → 跑 → 确认变红 → 还原）。F1/F2/F3 三条一起
neutralize 时分别命中 2 / 1 / 1 个失败，F5 命中 1 个。

**被阈值卡掉、经复核后判定不改的**：`I13`（0 分，`excluded` 是每请求局部变量，
不存在跨请求黑名单，且默认 `retry_count=0` 根本不消费）、`I8`（25 分，
`break` 在 DB 已不可用时是比逐把 Key 各等 30s 更优的降级，且那个装饰性写
下一轮 claim 的 Case 1 会自愈）、`I6`（20 分，纯 docstring 陈旧，无错误行为）。

**已知未修（评分 60-72，属真实但影响有限或属存量）**：
`I3` 兜底判据读内存 `log.status` 而非 DB 行，`log.save()` 自身抛错时漏接（窗口极窄）·
`I4` `settle_stale_pending` 只归一日志不退还配对额度，且抹掉了 pending 识别依据 ·
`I10` 默认配置下唯一能抛 TimeoutError 的是 max_duration，但处理侧仍硬编码
`stream_idle_timeout` 标签与"0 秒未收到数据"文案 ·
`I12` 总墙钟是每次竞速而非每请求，`retry_count` 最大 5 时上限实为 6 小时 ·
`I14` 前端 §3.1+§3.3 叠加后 `selected` 可含 `filtered` 之外的 id。

**全量回归 597 passed / 0 failed**（587 → +10），`check` 无问题，`makemigrations --check` 无待生成。

---

## 十二、性能优化执行记录（2026-09-07）

目标：全面优化前后端性能与 WebUI 流畅度。做法是先测再改 —— 所有数字都是
在 `data/db.sqlite3` 的**临时副本**上跑出来的（生产库正在被运行中的实例持有写锁，
上一轮我拿它试探针已经出过一次事故）。

### 12.1 基线（改前）

一个 `/v1` 请求的**同步视图体**要占住 asgiref 唯一那条 thread-sensitive 线程
115.6 ms。因为本项目每个入口都是同步视图（§2.2），这就是整站的吞吐上限：

```
理论上限 ≈ 9 req/s      ← 与上游快慢完全无关，上限是这条线程
```

`build_routes` 的成本分解（openrouter：339 Key / 300 启用代理 / max_routes=100）：

| 段 | 耗时 | SQL 条数 |
|---|---|---|
| `claim_rpm_slot × 100` | **123.6 ms** | **300** |
| `available_keys` | 6.3 ms | 1 |
| `schedulable_proxies` | 6.6 ms | 1 |

96% 在 RPM 领取上。另外实测把 100 次 claim 包进单个 `atomic()` 只从 128.9 降到
123.5 ms —— 说明瓶颈是**每条语句的固定开销**，不是事务提交，所以必须减语句数。
`defer(api_key)` 也无效甚至更慢：成本在 ORM 建行而非列宽，所以真正的解法是少取行。

### 12.2 改后

```
build_routes            111.5 →   9.34 ms  (p95 22.6)
resolve_in_channel        1.47 →   0.00 ms  (渠道级解析表缓存)
每请求同步视图体合计     115.6 →  10.47 ms
理论吞吐上限                9 →     96 req/s   (≈11×)
```

| 提交 | 改动 |
|---|---|
| `perf(scheduler)` | RPM 领取批量化：按「窗口过期重置」/「窗口内递增」各一条批量条件 UPDATE，用 `last_used_at == now` 作为本次领取标记回读精确身份（rowcount 只给数量）。100 把 Key 从 300 条语句降到 4 条。`available_keys` / `schedulable_proxies` 过滤排序下推 SQL 并支持 limit（代理侧 1:1 消耗可精确截断，Key 侧留 8 条余量）；去掉调度路径不需要的 `select_related("group")`；给每行挂上已知 channel，省掉 race_engine 读 `key.channel` 的惰性外键查询 |
| `perf(registry)` | `resolve_in_channel` 改渠道级解析表缓存，沿用信号失效 + 3s TTL；语义与旧的「alias/上游名 SQL 匹配 + 附加别名 Python 扫」逐条对应 |
| `perf(api)` | 列表专用序列化器裁掉列表页根本不渲染的字段：keys 143→99 KB（-31%）、proxies 180→97 KB（-46%）。前端类型同步改可选，否则声明必有、实际 undefined，tsc 一声不响 |
| `perf(frontend)` | 行组件 memo 化（keys/proxies）：12 列 × 200 行 ≈ 6000 元素，此前任何 setState 都整片重渲染，是"点一下卡一下"的直接来源；过滤与切片 useMemo；表头 sticky；加载态从 spinner 改骨架行；colSpan 魔法数改按列数计算 |
| `perf(frontend)` | 日志页自动刷新改静默增量：不再每 5 秒把整张表换成骨架行（看着像坏了），且"加载更多"到 1000 行后不再每 5 秒重传重渲染 1000 行 |
| `perf(admin)` | `sync_models` 的 N+1（每上游模型一次 get_or_create）与 `bulk_import_proxies` 的逐行 `exists()` 改为集合式；`check_all` 的存量查询移出事件循环 |

### 12.3 过程中被实测挡住的两个坑

1. **`bulk_create` 会绕过 `Proxy.save()` 的密码加密** —— 代理导入本来可以顺手
   也改成 bulk_create，但 `Proxy.save()` 负责 `encrypt_secret`，bulk_create 会把
   **明文密码写进库**，而 `decrypt_secret` 有明文回落所以运行期看不出问题。
   所以代理写入仍是逐行 create，守卫只断言 **SELECT** 条数而非总条数，
   并在测试里写明为什么。
2. **`bulk_create(ignore_conflict=)` 参数名写错** —— 正确是 `ignore_conflicts`。
   这个 typo 会在第一次真实同步时 TypeError，是写守卫用例时被抓到的。
   又一次说明：守卫要证明"没有它时会红"。

另外 tsc 直接抓住一处：`load` 加了 `silent` 形参后，`onClick={load}` 会把
MouseEvent 当成 `silent`（真值），手动刷新再也不显示加载态。

### 12.4 新增守卫

`NoNPlusOneOnBulkAdminPaths` 4 例（用 SQL 条数断言，CI 上耗时噪声太大）、
`test_concurrent_bulk_claims_do_not_exceed_rpm`（20 线程抢 rpm_limit=7，断言总领取
恰好 7）、`test_bulk_claims_respect_per_key_limits`、
`test_build_routes_uses_the_bulk_claim`（改回逐条不会让任何功能测试变红，单独钉一条）。
R14 的 DB 争用守卫改为同时 patch 两个领取入口 —— 只 patch 未被使用的那个会空转。

**全量回归 614 passed / 0 failed**（含渲染成本与后端批量路径的源码形态守卫），`manage.py check` 无问题，
`makemigrations --check` 无待生成迁移，`tsc --noEmit` 干净，`next build` 13 路由预渲染。

### 12.5 未做（需要决策或前置测试）

- **数据面视图改 async + 去掉 `race_chat` 的 `asyncio.run`**：这是 §2.2 那条结构性
  约束的唯一根治，能把"一条线程串行所有请求"变成真正的并发。本轮把那条线程上的
  单位工作压到 1/11，但**约束本身还在**。它必须先补竞速心跳注入与客户端断开强制
  结算的专属测试（§六 第 12 项），否则会把"冻结一条线程"换成"每个非流式请求
  泄漏 max_routes 个 RPM 槽位"。
- **`max_routes_per_request` 的按渠道覆盖值仍是 40/50/60/100**：代码默认已降到 8，
  但 SystemSetting 里的覆盖优先。100 条线路对竞速没有额外收益（§A2 论证过
  竞速冗余在这个倍数下是自伤），压到 8 还能再省 ~7 ms/请求。这是运维取值决策。
- 列表接口真分页、`/healthz` 与 DB 解耦、周期 housekeeping：维持原分级。
