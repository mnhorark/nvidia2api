# 前端 UI/UX 与架构审查（2026-09-07）

> 范围：`frontend/` 全部 27 个源文件（8794 行）逐行精读 + 构建配置 + Django 托管侧。
> 方法：架构分层（数据层 / 状态 / 契约 / 产物）、UI/UX 逐页、可访问性、一致性、
> 对照 AGENTS.md §2/§31~§37/§54~§57 做覆盖度核对。
> 与 `docs/frontend-review.md`（2026-09-01）的关系：那份是首轮，结论"整体优秀"。
> 本轮在它基础上重读，**发现首轮漏掉的 8 个正确性缺陷**，并修正其中 1 条不实结论
> （见 §6）。首轮已修的骨架屏 / 重试按钮 / 仅清理不同步本轮确认在位。

---

## 一、总体判断

架构选型是对的，执行有洞。

**对的部分**（不重复改）：

- 零数据层依赖（无 react-query / axios / shadcn / 图表库），1.3MB 产物、最大 chunk 224KB。
  个人运维台这个取舍正确，不要动。
- `lib/api.ts` 单文件承载请求封装 + 全部契约类型 —— 前后端契约单一来源，可维护性高。
- 在途 GET 去重（`api.ts:67-86`）、幂等 GET 单次重试（:113-126）、`AbortController` 超时兜底
  （:105-107）、`X-Channel` 统一注入（:101-102）—— 数据层基线高于同规模项目平均。
- 设计令牌齐全，`prefers-reduced-motion` 降级（`globals.css:109`）、`:focus-visible` 描边（:81）
  都在位。
- 静态资源 `Cache-Control: public, max-age=31536000, immutable` + HTML `no-cache`
  （`frontend_views.py:124-126`）—— 托管侧缓存策略是对的。

**问题集中在三处**：

1. **正确性**：8 项缺陷。其中 7 项是当下就会发生的（显示的数据是错的 / 操作打到错的渠道 /
   密钥永久丢失 / 功能入口不存在），1 项（§2.5）是潜伏的契约耦合。
   这类问题在 UI 审查里最容易被当成"体验小瑕疵"划过去，但它们改变的是**用户看到的数字本身**。
2. **一致性**：同一个应用里"全选""错误条""脱敏判断""是否就地更新"各页规则不同，
   且已有正确范式（models 页）与错误范式（keys/proxies 页）并存 —— 说明是缺约束，不是缺能力。
3. **工具链**：`lint` 脚本就是 `tsc --noEmit`，**没有 ESLint**。代码里所有
   `// eslint-disable-next-line react-hooks/exhaustive-deps` 都是装饰性的 —— 没有规则在跑，
   注释在"豁免"一个不存在检查。hook 依赖类 bug 零拦截。也没有 Prettier。

---

## 二、P0 正确性缺陷（8 项，逐条带证据）

### 2.1 仪表盘用量图"缓存命中后永不更新"

`app/(console)/dashboard/page.tsx:289-315`

```ts
async function loadUsage(r: string): Promise<UsageResponse> {
  const u = await api.get<UsageResponse>(...);
  usageCacheRef.current.set(r, u);   // ← 只写缓存
  return u;                          // ← 从不 setUsage
}
...
if (cached) {
  setUsage(cached);                          // 上屏的是 60s 前的旧数据
  await Promise.all([loadStats(), loadUsage(r)]);  // 新数据进了 Map，被丢弃
  return;
}
```

非缓存分支由调用方 `setUsage(u)`，缓存分支的返回值没人接。后果：

- 切回一个已缓存的时间尺度 → 渲染旧数据，后台拉到新数据 → 丢掉。
- 10s 轮询（:337-347）只调 `loadStats()`，不碰 usage。
- 净效果：**在仪表盘上坐 30 分钟，用量图停在首次加载的数字**，而 KPI 卡在动。
  用户会认为"图坏了"或"确实没流量"。

这是上一轮加缓存时引入的 —— stale-while-revalidate 写成了 stale-forever。

修法：缓存分支接住 `loadUsage` 的返回值并 `setUsage`（带 seq 守卫）。

### 2.2 删除当前渠道后，控制台静默切到默认渠道

`app/(console)/channels/page.tsx:219-227` 的 `remove()` 只 `await load()`，
既不 `setChannel(...)` 也不派发 `nvidia2api:channel-change`。

于是 `localStorage[nvidia2api_channel]` 仍指向已删除的 slug，后续每个请求都带
`X-Channel: <已删除>`，而后端 `services/channel_service.py:92-94`：

```python
if slug and str(slug).strip():
    logger.warning("unknown channel %r, falling back to default", slug)
return default_channel()
```

**不报错，回落默认渠道。** 前端 layout 只在挂载时（:55-58）和收到 channel-change 事件时
（:76-87）才重新校验 slug —— 两个时机都没被触发。

结果：侧边栏仍显示被删渠道的名字（`channels` state 已被 `load()` 更新，但
`channel` state 没变，`ChannelSwitcher` 的 `active` 查不到 → 显示"选择渠道"，
而 `<main key={channel}>` 用的是死 slug，页面不重挂载），
实际所有增删改查都打在**另一个渠道**上。

对管理台来说"静默改错作用域"比"报错"严重得多。刷新页面才自愈。

修法：`remove()` 里若 `c.slug === current`，删除成功后显式 `setChannel(新的 current)`。
更稳的做法是后端对未知 slug 返回 400 而不是静默回落 —— 但那是后端契约变更，需单独评估。

### 2.3 用户 Key 的额度一旦设定就再也改不了 / 去不掉

`app/(console)/api-keys/page.tsx:196-210`

```tsx
{k.quota > 0 ? (
  <span onClick={() => setQuotaEdit(k)}>…</span>   // 唯一入口
) : (
  <span className="text-faint">不限</span>          // 无 onClick
)}
```

额度编辑器挂在 `quota > 0` 分支里。所以：

- 创建时给了额度 → 用完了 → 想放开成"不限" → **UI 里没有这条路**。
- 创建时留 0（不限）→ 后来想给这个 Key 加额度 → 同样没有这条路。

`rate_limit` 同理：只在创建弹窗（:247-256）出现，列表页无任何编辑入口。
AGENTS.md §36 要求"可以设置：请求频率限制 / 允许模型"，目前只有"创建时一次性设置"。

（`allowed_models` 是端到端缺失：`UserApiKey` 模型里根本没这个字段，不是前端漏做。）

修法：整行可点，或加一个"编辑" IconButton 打开统一编辑弹窗（名称/限流/额度/启用）。

### 2.4 一次性密钥弹窗可被 Esc / 点遮罩关掉 → 密钥永久丢失

`app/(console)/api-keys/page.tsx:278`

```tsx
<Modal open={!!createdKey} title="API Key 创建成功" onClose={() => setCreatedKey(null)}>
```

`components/ui.tsx:199-206` 的 Modal 无条件绑定了 Esc 关闭，:210-212 的遮罩
`onClick={onClose}` 无条件生效。而这个弹窗展示的是**只显示一次的明文密钥**
（弹窗自己的文案：「完整 Key 只会显示这一次，请立即复制保存」）。

误按 Esc、或习惯性点一下旁边空白 → 密钥没了，只能删掉重建。

顺带修正首轮报告的一处不实结论：`docs/frontend-review.md:34` 写「Modal busy 禁关」，
但 `Modal` 组件**没有 `busy` 参数**（`grep busy components/ui.tsx` 无结果）。
所有弹窗在请求在途时都能被 Esc/遮罩关掉 —— 只是关掉后请求照样完成、结果落到已卸载的 state。
按钮级 `loading` 挡住了重复提交，没挡住"中途关窗"。

修法：给 `Modal` 加 `dismissable?: boolean`（或 `busy?: boolean`），
true 时不绑 Esc、遮罩点击不关闭、隐藏 X。密钥弹窗设 `dismissable={false}`，
只留"我已保存"。

### 2.5 编辑 Key 时"是否被脱敏"用字符串猜测（前端复刻了后端掩码契约）

`app/(console)/keys/page.tsx:161-164`

```ts
if (editItem.api_key && !editItem.api_key.includes("••")
    && !editItem.api_key.includes("*")) {
  body.api_key = editItem.api_key;
}
```

`editItem` 来自 `setEditItem(k)`（:451），**k 是后端序列化好的整行，含脱敏后的 `api_key`**
（`serializers.py:62` → `mask_key` → `crypto.mask_secret`）。编辑表单在 `editItem.id`
存在时不渲染 Key 输入框（:590），所以这个字段全程是"从行数据带进来的脱敏串"，
全靠上面这个字符启发式判断"用户是不是真改了 Key"。

口径核对（避免夸大）：后端掩码是 `前10 + 8×* + 后4`，短串退化为 `前4 + ****`
（`services/crypto.py:54-64`），**两种形态都必含 `*`**，所以这个启发式对当前后端是成立的，
今天不会把掩码串写回库。真正的问题是三条：

1. **契约被复制成了两份且没有同步机制。** 后端把 `mask_secret` 明确写成
   "脱敏口径的单一事实来源"（docstring 原话），而前端用 `includes("*")` 反向猜这个口径。
   后端哪天改成 `…` 或 `····`，前端这条守卫立刻失效，掩码串被当新 Key 写库 ——
   后果不是显式报错，是"这把 Key 从此 401 → 标 invalid → 掉出竞速池"。
2. **`"••"` 这一支已经是上一代掩码的残留**，当前后端任何路径都不会产出 U+2022。
   一条永远为真的判断挂在守卫里，读代码的人会以为还存在这种格式。
3. **反方向的真实缺陷**：守卫是"含 `*` 就当作没改"。用户新建 Key 时若真实 Key 值里
   含 `*`（部分网关/自托管上游确实如此），输入框 `required` 放行、
   `body.api_key` 却被丢掉 → POST 不带 Key → 建出匿名线路或后端报"缺少凭据"，
   而界面看起来用户已经填过了。

修法：不要从行数据带 `api_key` 进编辑态。编辑态 `api_key` 初值 `""`、
用显式 `apiKeyDirty` flag 决定是否提交，删掉字符启发式。

### 2.6 思考块"结束后自动收起"从未生效

`app/(console)/chat/page.tsx:404-405`

```ts
function ReasoningBlock({ text, autoOpen }: { text: string; autoOpen?: boolean }) {
  const [open, setOpen] = useState(autoOpen ?? false);   // ← 只在挂载时取一次
```

调用点 :315 `<ReasoningBlock text={m.reasoning} autoOpen={sending} />`。
思考块必然在 `sending === true` 期间挂载 → `open` 初始为 true；
流结束后 `autoOpen` 变 false，但 `useState` 不会因 prop 变化重置。

**结果：每个回答的思考过程永远展开着**，长思考模型（kimi-k3 这类）把对话区撑成
一屏滚动条，正文被挤到底部。这个 autoOpen 参数是死代码。

修法：`useEffect(() => { if (!autoOpen) setOpen(false) }, [autoOpen])`，
或干脆 `open = userToggled ?? sending`。

### 2.7 `<think>` 变体不会被剥离

`app/(console)/chat/page.tsx:17`

```ts
const THINK_RE = /\s*<thinking>([\s\S]*?)<\/thinking>/i;
```

:21 的注释写着 "content may still hold `<think>` tags"，但正则只认 `<thinking>`。
DeepSeek / Qwen / Kimi 系实际吐的是 `<think>...</think>`。这些模型的正文里
会带着原始 `<think>` 标签直接渲染给用户（`whitespace-pre-wrap`，:317）。

修法：`/<(?:think|thinking)[^>]*>([\s\S]*?)<\/(?:think|thinking)>/gi`，
或按渠道协议分派。

### 2.8 代理分组页是唯一没有提交防重的表单

`app/(console)/proxy-groups/page.tsx:43-59` —— 全文件没 import `useSubmitGuard`，
保存按钮（:170-172）也没有 `loading`。双击 / 连按回车 = 两次 POST。
其余 5 个带表单的页面（keys / proxies / models / channels / api-keys）都上了 guard。

---

## 三、P1 一致性陷阱（同应用不同规则）

### 3.1 「全选」在三个页面作用域不同 —— 且两个是错的

| 页面 | toggleAll / invertSelection 依据 | 表头 checkbox 比较基准 | 行渲染依据 |
|---|---|---|---|
| models | `filtered` ✅ (:144-158) | `filtered.length` ✅ (:272-273) | `filtered.slice` |
| keys | `keys`（全集）❌ (:255-269) | `keys.length` ❌ (:384-385) | `filtered.slice(0,200)` |
| proxies | `proxies`（全集）❌ (:217-231) | `proxies.length` ❌ (:514-515) | `filtered.slice(0,200)` |

keys 页搜 "主账号" 命中 3 条、屏幕上 3 行，点表头全选 →
`new Set(keys.map(...))` 选中全部 1800 条。BatchBar 显示"已选 1800 项"，
然后删除。models 页证明正确写法就在同一个仓库里。

### 3.2 「匹配 N」和实际行数会不一致

`proxies/page.tsx:330-336` 的 `kwMatched` 只匹配 name/host；
:101-108 的 `filtered` 额外匹配 `group_name`。按分组名搜时，
计数说"匹配 0"，下面却列出 12 行。代码注释（:99）把这写成有意保留旧语义 ——
但同一个筛选框旁边显示的两个数字互相矛盾，不是语义问题，是错。

### 3.3 任何一次 `load()` 都清空用户的选择

`proxies:71`、`keys:82`、`models:62` 无条件 `setSelected(new Set())`。
而 `testOne` / `fetchIp` / `toggleCancelUnhealthy` / `testAll` 都调 `load()`。
所以：勾 30 个代理 → 点其中一个"测速" → 选择清空。批量操作前想先测一条，选择就没了。

修法：`load()` 保留 `selected ∩ 新集合`（只摘掉已不存在的 id）。

### 3.4 原生 `confirm()` / `prompt()` 与自研 Modal 并存

11 处 `confirm()`（api-keys / keys×3 / proxy-groups / models×2 / proxies×2 / settings / channels）
+ 1 处 `prompt()`（`keys:298` 改 RPM）。

原生框是浅色系统样式，贴在一个深色玻璃拟态控制台上，视觉断裂明显；
且 `prompt` 收一个 RPM 数字，无校验 UI（校验在 JS 里，:303-307）。

最严重的是 `channels:220` —— 删除渠道会级联删掉其下 Key / 代理 / 模型 / 日志，
全平台破坏性最强的操作，用的是一个 OK/Cancel 原生框。AGENTS.md §57 要求 Confirm 状态，
这种量级应当输入渠道名确认。

### 3.5 单行操作是否就地更新，各页不同

`upsertRow` 上一轮只落在 keys + proxies。models（`setEnabled:113-123`、`remove`、`save`）、
api-keys（`setEnabled`、`saveQuota`）、channels、proxy-groups 每次单行改动仍重拉全表。
models 列表可达上千行。

### 3.6 其它可见的不一致

- `not-found.tsx:3` 用 `bg-[#0a0a0f]` + `text-zinc-300/500/600` —— 全仓库唯一游离在
  设计令牌之外的文件。
- 删除按钮 `IconButton` 在 models:362 / api-keys:226 / proxy-groups:128 **缺 `title`**，
  keys / proxies / channels 都有。悬停无提示。
- `DataTable` 空态/加载态 `colSpan={50}` / `colSpan={100}`（`ui.tsx:344,351`）——
  魔法数，列数变化时靠"反正够大"兜着。
- 错误条 JSX 在 8 个页面各抄一份（`rounded-lg border border-err/25 bg-err/10 px-3 py-2 …`），
  只有 dashboard 那份带重试按钮。

---

## 四、P2 可访问性

| # | 位置 | 问题 |
|---|---|---|
| 1 | `request-logs:259-261` | 展开行是裸 `<tr onClick>`，无 `role="button"` / `tabIndex={0}` / `onKeyDown`。**键盘用户完全无法查看任何一条日志明细** —— 这是该页的主功能 |
| 2 | `channel-switcher:36-58` | 下拉触发器无 `aria-haspopup` / `aria-expanded`；菜单无 `role="listbox"`；无 Esc 关闭、无方向键、无焦点管理。只有 outside-mousedown |
| 3 | `ui.tsx:186-238` Modal | 无焦点陷阱、打开时不移动焦点、关闭后不归还焦点给触发元素。Tab 会跑到遮罩后面的页面上 |
| 4 | `login:66-79` | 两个输入框只有 placeholder，无 `<label>` / `aria-label`。placeholder 不是标签（无对比度保证、输入即消失） |
| 5 | `dashboard:586-612` 图表 | 纯 div 柱，hover-only tooltip（`ChartTip:110` 用 `group-hover:block`）。键盘不可聚焦、无 `aria`、整张图无文本替代 |
| 6 | `chat:298-331` | 流式输出不在 live region 里，读屏软件收不到增量 |
| 7 | `ui.tsx:399-430` Toggle | `role="switch"` + `aria-checked` 有 ✅，但无 `aria-label`，全靠上下文；表格里连续多个开关读起来是"开关 未命名" |

做得对的地方也记一下：`toaster.tsx:53-55` 有 `role="status" aria-live="polite"`；
`Checkbox` 有 `ariaLabel` 透传且各页都传了；`:focus-visible` 全局描边在位；
`StatusDot` 的 ping 动画受 `prefers-reduced-motion` 管辖。基线不是零，是"有意识但没做完"。

---

## 五、P3 架构与工具链

### 5.1 没有 ESLint

`package.json:9` —— `"lint": "tsc --noEmit"`。

代码里存在 `// eslint-disable-next-line react-hooks/exhaustive-deps`
（`dashboard:332`、`chat:65`），但没有 ESLint 在跑。这些注释：

- 不产生任何效果；
- 向读者暗示"这里作者知道依赖不全且已备案"，而实际上没有任何东西校验过。

`exhaustive-deps` 恰好是这个代码库最需要的一类检查 —— 各页大量 `useCallback(load, [])`
+ `useEffect(load, [load])`，闭包捕获过期 state 是这类写法的典型事故面。

也没有 Prettier：`channels/page.tsx:125-143` 的 `submit(async () => {` 包进去后
内部代码没重排，缩进明显错位（`const body = {` 在 4 空格、外层 `try` 在 6 空格）。

### 5.2 状态与数据层

- **无全局 store 是对的**，但渠道信息现在有三份副本且会漂移：
  `localStorage`（权威）、`layout` 的 `channel` state、各页自己再 GET
  `/api/admin/channels` 取名字（`keys:77-84` 每次 load 都拉一遍渠道列表，
  就为了一个 `channelName`）。缺一个 `ChannelContext`。
- **chat 页绕过 `request()`**（`chat:120-135`）自己拼 fetch：重复实现 Authorization 头、
  X-Channel 注入、401/403 跳转。SSE 确实不能用 `request()`（它 `res.text()` 全量读），
  但认证头至少应抽成 `authHeaders()` 共用。现在 `lib/api` 改鉴权方式会静默漏掉 chat。
- **无 `loading.tsx` / `template.tsx`**，每页各自 `useState(loading)` + 各自骨架。
  dashboard 有骨架屏，其余 9 页只有 `DataTable` 中间一个转圈。
- **无 `export const viewport`**（`app/layout.tsx` 只有 metadata）——
  Next 16 会警告；也没有 `themeColor`，移动端浏览器地址栏是白的，接在深色页面外面很跳。

### 5.3 重复代码（可抽但未抽）

| 重复项 | 份数 | 位置 |
|---|---|---|
| `statusLabels` / `badgeLabels` 状态中文映射 | 2 | `dashboard:222-235`、`ui.tsx:143-156` —— 两份完全相同的表，已经可以各自漂移 |
| `selected: Set<number>` + `toggleOne`/`toggleAll`/`invertSelection` | 3 | keys / proxies / models，逐字抄 |
| `RENDER_WINDOW = 200` + `windowSize` + 加载更多 + 重置 effect | 3 | keys / proxies / models |
| 错误条 JSX | 8 | 各页 |
| `fmtNum` / `fmtNumInt` / `fmtQuota` | 3 | `dashboard:237-249`、`api-keys:23-27` —— 逻辑几乎相同 |
| 行内 `busyId: number \| null` 单值在途态 | 5 | keys / proxies / channels / models / api-keys |

`busyId` 单值还有个功能副作用：同时测两行时，第一行的 spinner 会被第二行顶掉。

### 5.4 响应式

全仓库只有 **13 处** 断点工具类（6 lg / 4 md / 2 sm / 1 xl）。
侧边栏 `layout.tsx:98` 是 `fixed w-56` + `main` 的 `ml-56`，**没有任何断点让它收起**。
375px 视口下导航吃掉 224px，内容区剩 151px。没有汉堡按钮、没有抽屉、没有 `md:hidden`。

AGENTS.md §2 写的是"桌面端优先，同时兼容移动端"。目前是"桌面端优先，移动端不可用"。

另外 12 列的代理表默认 `min-w-max`（`ui.tsx:336`），**7 个 `DataTable` 里只有
request-logs 一页传了 `fill`**（`request-logs:236`）—— 其余全是横向滚动，
且**表头不 sticky**：展开到 200 行后完全失去列对应关系。

### 5.5 死代码 / 无效声明

- `globals.css:28` `font-feature-settings: "ss01", "cv11"` 是 Inter 的 OpenType 特性，
  但 `tailwind.config.ts:26-31` 的 sans 是系统字体栈，从没加载 Inter。两条特性恒无效。
- `globals.css:69-78` `.glass` / `.panel` 两个类，全仓库零引用。
- `channels/page.tsx:159` `async function switchTo` 内部无任何 await。
- `keys/page.tsx:161-164` 的掩码启发式（见 2.5）。
- `chat/page.tsx:405` 的 `autoOpen`（见 2.6）。

---

## 六、对首轮报告（`docs/frontend-review.md`）的修正

首轮有两处需要更正，避免后续基于它决策：

1. **`:34` 「Modal busy 禁关」不成立。** `Modal` 组件没有 `busy` 参数，
   Esc 与遮罩点击在任何状态下都直接关闭。见 §2.4。
2. **`:27` 「仪表盘 10s 轻量轮询 + seqRef 竞态防护」描述不完整。**
   轮询只刷 stats，usage 图不参与轮询；叠加 §2.1 的缓存命中丢更新，
   实际行为是"KPI 每 10s 更新、用量图永不更新"。首轮把它记为亮点，是漏判。

首轮已落地的 4 项（骨架屏、重试按钮、同步并清理、仅清理不同步）本轮确认在位、无回归。

---

## 七、对照 AGENTS.md 的覆盖度

| 需求 | 状态 |
|---|---|
| §32 页面清单（dashboard / keys / proxies / proxy-groups / models / api-keys / logs / settings） | ✅ 全在，另多 channels + chat 两页 |
| §33 Key 表字段（名称/Key/状态/40 分钟/本分钟/成功率/最后使用/操作） | ✅ 全在，多"成功/失败"列 |
| §34 代理表 + 批量导入/测速/启停/删除/移动分组 | ✅ 全在 |
| §35 模型：同步 / 添加 / 启用 / 禁用 | ✅ 全在 |
| §36 API Key：创建 / 删除 / 禁用 / 使用统计 | ✅ |
| §36 API Key：**请求频率限制**（可后续调整） | ⚠️ 仅创建时可设，事后无入口（§2.3） |
| §36 API Key：**允许模型** | ❌ 端到端未实现（`UserApiKey` 无该字段） |
| §37 日志筛选：模型 / 状态 | ✅ |
| §37 日志筛选：**时间 / API Key / 线路 / 代理分组** | ❌ 前端无 UI，后端 `logs.py:47-58` 也只支持 model+status —— 两层都缺 |
| §31 Dashboard 全部指标 + 实时并发 + Key/Proxy 状态分布 | ✅ |
| §56 代理池顶部「启用 N / 上限 M / 还可启用 K」 | ✅（`proxies:417-440` 四张状态卡） |
| §57 Loading / Success / Error / Empty / Confirm 五态 | ⚠️ 五态都在，Confirm 用原生框（§3.4），Error 无重试的页面 7 个 |
| §54 深色科技感 / 玻璃拟态 / 状态 Badge / Lucide | ✅ 执行质量高 |
| §2 移动端兼容 | ❌ 见 §5.4 |

---

## 八、修复优先级建议

**第一批（正确性，改动小、风险低）**

1. `dashboard` 缓存分支接住 `loadUsage` 返回值并 `setUsage`（seq 守卫）
2. `channels.remove` 删除当前渠道后显式切走 slug
3. `keys` 编辑态不再携带脱敏 `api_key`，改显式 dirty flag
4. `chat` `ReasoningBlock` 受控收起 + `THINK_RE` 覆盖 `<think>`
5. `proxy-groups` 补 `useSubmitGuard`
6. `api-keys` 额度/限流改为整行可编辑
7. `Modal` 加 `dismissable`，一次性密钥弹窗禁用外部关闭

**第二批（一致性，需要跨页统一）**

8. keys / proxies 的全选、反选、表头基准改为 `filtered`（对齐 models）
9. `load()` 保留仍存在的选中项
10. `kwMatched` 与 `filtered` 共用同一个谓词函数
11. 抽 `useTableSelection` / `useRenderWindow` / `ErrorBanner` / `ConfirmDialog`，
    11 处 `confirm()` 迁到自研框，渠道删除加输入名确认
12. `upsertRow` 推广到 models / api-keys / channels
13. `statusLabels` 合并进 `ui.tsx` 单一来源
14. `not-found.tsx` 回到令牌体系；缺失的 `title` 补齐

**第三批（工具链 + 可访问性 + 移动端）**

15. 装 ESLint（`next/core-web-vitals` + `react-hooks`），把 2 处装饰性 disable 注释变成真豁免
16. 装 Prettier，一次性格式化（`channels` 缩进错位在范围内）
17. `ChannelContext` 取代各页重复 GET 渠道列表；`authHeaders()` 给 chat 复用
18. 日志行可键盘展开、Modal 焦点陷阱、渠道切换器键盘化、登录页加 label、图表加文本替代
19. 侧边栏移动端抽屉 + 表头 sticky
20. `export const viewport` + `themeColor`

**需要产品决策、不属于"修 bug"的两项**

- §37 的日志筛选（时间 / API Key / 线路 / 分组）要前后端一起做，后端要先支持这些查询参数
- §36 的"允许模型"要动 `UserApiKey` 表 + 鉴权链路

---

## 九、验证基线

- `npx tsc --noEmit` → 通过（本轮审查起点）
- 产物：`frontend/out` 1.3MB，最大 chunk 224KB / 156KB / 112KB（React + Next 运行时，
  无第三方数据层与图表库）
- 本报告全部结论均带 `file:line`，可逐条复核；未做任何代码改动。

---

## 十、修复落地（同日，2026-09-07）

§8 的第一批 + 第二批全部落地，第三批里的高价值项（可访问性、viewport、死代码）一并做完。
`§36 允许模型` 与 `§37 日志多维筛选` 未做 —— 两者都是前后端贯通的功能，不是修 bug。

### 10.1 审查后追加发现的一处（比原报告更严重）

`§2.1` 写的是"60s 内命中缓存后丢弃重校验结果"。动手时才发现 **`USAGE_CACHE_TTL_MS`
声明之后从未被读取过**（`grep` 全文只有声明那一行），缓存 Map 在整个页面生命周期内
根本不过期。所以实际表现不是"最多旧 60 秒"，而是"首次加载后永久冻结"。
修法因此不是只补 `setUsage`，而是缓存值改成 `{ data, at }` + `freshUsage()` 真正判 TTL
+ 重校验结果写回 state + 轮询发现过期就静默重拉。

### 10.2 已修清单

| 项 | 改动 |
|---|---|
| §2.1 | `dashboard/page.tsx`：缓存带时间戳、TTL 生效、命中分支写回 state、轮询补拉过期区间（带 seq 守卫） |
| §2.2 | `channels/page.tsx`：`remove()` 删除当前渠道后显式 `setChannel(新 current)`；`load()` 返回数据供其判定 |
| §2.3 | `api-keys/page.tsx`：限流/额度两格整格可点 + 新增编辑按钮，统一编辑弹窗（名称/限流/额度）；`quotaEdit` → `editKey` |
| §2.4 | `ui.tsx`：`Modal` 新增 `dismissable`；一次性密钥弹窗 `dismissable={false}`；8 个表单/导入弹窗统一 `dismissable={!saving}` |
| §2.5 | `keys/page.tsx`：删掉 `includes("*")` 掩码启发式，改独立 `apiKeyDraft` + `openEdit/closeEdit`；Key 输入框改受控 |
| §2.6 | `chat/page.tsx`：`ReasoningBlock` 改 `userOpen ?? streaming` 受控收起，且只有"正在流式的最后一条"会展开 |
| §2.7 | `chat/page.tsx`：`THINK_PAIR_RE` 同时覆盖 think / thinking 两种拼写，`stripThink()` 循环剥多段 |
| §2.8 | `proxy-groups/page.tsx`：补 `useSubmitGuard` + 按钮 loading |
| §3.1 | `keys` / `proxies`：`toggleAll` / `invertSelection` / 表头基准全部改 `filtered`（对齐 models） |
| §3.2 | `proxies`：抽出模块级 `matchKw()`，过滤 / 计数 / 区间选择三处共用；`kwMatched = filtered.length` |
| §3.3 | `keys` / `proxies`：`load()` 改为保留仍存在的选中项（只摘掉已不存在的 id） |
| §3.4 | 新增 `confirmDialog()` + `ConfirmHost()`（挂在根布局），11 处原生 `confirm` 与 1 处 `prompt` 全部迁移；渠道删除要求**输入渠道名**确认，日志清理要求输入保留天数；批量改 RPM 从 `prompt` 换成带 number 输入的弹窗 |
| §3.5 | `models` / `api-keys` / `channels`：补 `upsertRow`，单行操作不再重拉全表（`makeDefault` 例外，注释说明原因） |
| §3.6 | `not-found.tsx` 回到令牌体系并改用 `Link`；models / api-keys / proxy-groups 的删除按钮补 `title` |
| §4.1 | `request-logs`：展开控件改成真 `<button>`（`aria-expanded` + `aria-label`），行的鼠标点击保留 |
| §4.2 | `channel-switcher`：`aria-haspopup` / `aria-expanded` / `role=listbox`+`option` / 方向键 + Home/End + Esc / 焦点归还触发器 |
| §4.3 | `Modal`：焦点陷阱（Tab 环绕）、打开时移入焦点、关闭时归还 |
| §4.4 | `login`：两个输入框补 `sr-only` label + `name`；顺手合并了重复的 `@/components/ui` import |
| §5.2 | 8 个页面的错误条统一成 `ErrorBanner`，全部带上此前只有仪表盘才有的**重试**按钮 |
| §5.4 | `app/layout.tsx`：补 `export const viewport` + `themeColor`（Next 16 告警消除） |
| §5.5 | 删除 `font-feature-settings`（Inter 特性配系统字体栈，恒无效）、未引用的 `.glass`/`.panel`、`switchTo` 的无效 `async` |
| §6 | 首轮报告的两处不实/漏判已在本报告更正，代码层面 `Modal busy 禁关` 现在真正存在了 |

### 10.3 新增回归守卫

前端零 JS 测试依赖（无 vitest/jest），而这些缺陷全是"看着能跑、数字是错的"那一类 ——
类型系统和构建都抓不到。在引入 JS 测试框架之前，用源码级不变量守卫钉死：

`backend/tests/test_frontend_guards.py`，**24 条**，覆盖上表每一类修复的反向情形
（原生对话框复活 / TTL 只声明不使用 / 缓存分支不写回 state / 掩码启发式复活 /
全选按全集算 / 谓词被复制成多份 / 表单页丢防重 / 状态映射出现第二份 /
令牌外硬编码色 / 只认一种思考标签拼写 / 删当前渠道不切 slug）。

写守卫过程中踩到两个坑，都记在测试文件注释里：

1. 断言必须跑在**剥掉注释**的源码上 —— 这些修复的注释里恰恰写着被禁的旧写法
   （`includes("*")`、`bg-[#0a0a0f]`），不剥就会因解释性文字误报（首跑中 3 条）。
2. `_func_body` 按括号配对定位函数体时，JSDoc 里的 `1)` `2)` 会被当成闭合括号，
   把定位切进注释文本（`Modal` 的 `dismissable` 文档正好触发，返回了 `{!saving}`）。
   因此 `_code()` 采用**等长空格替换**而非删除，保住偏移量。

### 10.4 验证

- `npx tsc --noEmit` → 通过
- `npx next build` → 13 条路由全部静态预渲染成功，无 viewport 告警
- `python -m pytest tests -q` → **567 passed**（原 543 + 新增守卫 24）
- 接口形状零变更：本轮全部改动都在前端，未新增/修改任何后端 API 字段

### 10.5 仍未做（需要决策，不是遗漏）

- **ESLint / Prettier**：`lint` 仍是 `tsc --noEmit`，两处
  `eslint-disable-next-line react-hooks/exhaustive-deps` 依旧是在豁免不存在的检查。
  装 `eslint-config-next` 需要联网装包，且会带来一轮全量格式重排 —— 等她点头。
- **移动端**：侧边栏仍是 `fixed w-56` + `ml-56`，无断点收起。要做抽屉 + 汉堡，
  是独立一轮，不适合混在缺陷修复里。
- **表头 sticky**：12 列表格横向滚动 + 展开 200 行后仍会失去列对应关系。
- **`busyId` 单值**：同时测两行时第一行的 spinner 会被顶掉。改成 `Set<number>` 即可，
  但涉及 5 个页面，留作单独一轮。
- **§36 允许模型 / §37 日志多维筛选**：前后端贯通的功能开发。

