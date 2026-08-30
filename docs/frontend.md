# 前端

Next.js 16 (App Router, Turbopack) + TypeScript + Tailwind CSS + lucide-react。

## 结构

```
frontend/
├── app/
│   ├── layout.tsx              # 根布局，挂载 <Toaster />
│   ├── page.tsx                # / → /dashboard
│   ├── login/page.tsx
│   ├── not-found.tsx
│   ├── globals.css             # 深色玻璃拟态底样式
│   └── (console)/
│       ├── layout.tsx          # 侧栏 + 鉴权守卫（未登录推回 /login）
│       ├── dashboard/          # 仪表盘 + Token 图表（跟随当前渠道）
│       ├── channels/           # 渠道 CRUD + 一键切换 + 设为默认 + 连通测试 + 同步模型
│       ├── chat/               # 对话（流式 + 思考展示 + Token 统计）
│       ├── keys/               # 渠道 Key CRUD + 批量导入 + 测试 + 显隐
│       ├── proxies/            # 代理 CRUD/导入/测速/IP + 启用上限
│       ├── proxy-groups/       # 分组 CRUD
│       ├── models/             # 列表/同步/启停
│       ├── api-keys/           # 用户 Key CRUD（rate_limit 0=不限）
│       ├── request-logs/       # 日志列表 + 展开竞速明细
│       └── settings/           # 运行时参数编辑器
├── components/
│   ├── ui.tsx                  # Card/Button/Badge/Modal/DataTable/Toggle/… + NvidiaLogo
│   ├── channel-switcher.tsx    # 侧栏顶部渠道切换器（受控组件）
│   └── toaster.tsx             # 全局 Toast；`toast.success/error/info(...)`
└── lib/api.ts                  # fetch 封装 + 类型 + token/渠道管理
```

## 数据流

- `lib/api.ts` 的 `api.get/post/patch/del`：自动带 `Authorization: Token`，401/403 清空 token 并跳回登录
- **自动带 `X-Channel`**：从 localStorage 读当前渠道 slug 注入请求头，所以各页面组件
  不需要关心渠道，后端会据此过滤数据
- 列表响应兼容 `{results: []}`、`{data: []}`、裸数组（`asList`）
- 错误归一到 `Error.message`（读 `error.message` 或 `detail`）

## 渠道切换

状态源是 `lib/api.ts` 里的 `getChannel()/setChannel()`（localStorage key `nvidia2api_channel`）。

1. `(console)/layout.tsx` 拉取 `/api/admin/channels`，把渠道列表与当前 slug 交给
   `<ChannelSwitcher>`（受控组件，不自己发请求）
2. 用户点选 → `setChannel(slug)` 写入 localStorage
3. layout 用 `<main key={channel}>` 重挂载页面组件 —— **这是刷新数据的关键**：
   key 变化触发 unmount/remount，各页 `useEffect` 重新拉取，数据自然按新渠道加载

好处是不用给每个页面加渠道监听逻辑；代价是切换时会整页重挂载（表单草稿会丢，
但各页表单都是弹窗内的临时状态，可接受）。

`channels/page.tsx` 里的「切换到此渠道」按钮额外 dispatch 一次
`nvidia2api:channel-change` 事件，用于同页内即时反馈。

## 几个非显然的实现

**流式对话**（`chat/page.tsx`）：不用 fetch 的 SSE 库，直接 `reader.read()` + `\n\n` 分帧，自识别：

| 帧 | 动作 |
|---|---|
| `data: {"meta": …}` | 显示 Winner/线路/first_chunk |
| 普通 chunk | 累加 `delta.content` / `delta.reasoning_content`，原地重渲染 |
| `data: {"summary": …}` | 填入总耗时和 token 明细 |
| `data: {"error": …}` | 把错误写进气泡、toast 提示 |
| `data: [DONE]` | 结束 |

**思考内容**支持两种上游格式：`delta.reasoning_content` 字段、`<think>...</think>` 包裹在 content 里——两者都被剥成独立的可折叠区块。

**Token 图表**（仪表盘）：纯 SVG/CSS 柱状图，hover 查看明细，无第三方图表库。

**日志总行展开**：每行用 `Fragment key={request_id}` 包住两行 `<tr>`（数据 + 明细），不加 key 会触发 React 警告。

## 本地开发

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000
```

后端默认 `http://127.0.0.1:8000`，跨端口走 `NEXT_PUBLIC_API_BASE_URL`。
