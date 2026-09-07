"""前端源码级回归守卫。

为什么用源码断言而不是运行时测试：本项目前端零测试依赖（无 vitest / jest /
testing-library），`lint` 脚本也只是 `tsc --noEmit`。而这些缺陷全都是
"看起来能跑、但显示的数据是错的"那一类——类型系统抓不到，构建也抓不到。
在引入 JS 测试框架之前，源码级不变量守卫是唯一能把它们钉死的手段。

断言刻意写成**结构/语义**形式（函数体里引用了谁、某个标识符被读了几次），
而不是逐字匹配格式，这样 Prettier 重排不会误伤。

前端目录不存在时（纯 API 部署）整个模块跳过。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
APP = FRONTEND / "app"
COMPONENTS = FRONTEND / "components"
LIB = FRONTEND / "lib"

requires_frontend = unittest.skipUnless(FRONTEND.is_dir(), "前端源码树不存在")


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
# `(?<!:)` 是为了不把 `https://…` 里的双斜杠当成行注释起点
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _code(src: str) -> str:
    """把注释**等长替换成空格**，保留偏移量与行结构。

    两件事都需要它：

    1. 守卫断言的是"实际执行的东西"。而这些修复的注释里恰恰写着被禁的旧写法
       （"此前用 includes(\"*\") 反推掩码口径"、"原本用的是 bg-[#0a0a0f]"），
       不剥注释就会因为解释性文字误报——第一版跑就撞上三例。
    2. `_func_body` 要按括号配对定位函数体，而 JSDoc 里的 `1)` `2)` 会被当成
       闭合括号，把定位切进注释文本里（Modal 的 dismissable 文档就触发了这个）。

    等长替换而不是删除，是为了让偏移量仍然对得上原文，`_func_body` 可以直接吃它。
    """
    def blank(m: re.Match) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in m.group(0))

    return _LINE_COMMENT.sub(blank, _BLOCK_COMMENT.sub(blank, src))


def _func_body(src: str, signature: str) -> str:
    """按括号/大括号配对切出函数体。signature 例："function toggleAll()"。

    先跳过**整个参数列表**再找函数体的 `{`：这些组件的参数是解构对象 +
    类型字面量（`function Modal({ open }: { open: boolean }) {`），
    直接从第一个 `{` 开始数会切到参数对象或类型字面量上——第一版就栽在这里，
    返回了 `{ text, streaming }` 这种东西。
    """
    src = _code(src)  # 注释里的 `1)` `2)` 会骗过括号配对
    at = src.index(signature)
    i = src.index("(", at)
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    open_at = src.index("{", i)
    depth = 0
    for j in range(open_at, len(src)):
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:j + 1]
    raise AssertionError(f"函数体未闭合：{signature}")


def _block(rel: str, marker: str, span: int = 900) -> str:
    """取某个源码锚点之后的一段，用于"这段里必须/不得出现 X"。

    不直接用 `src.index()`：它在锚点被无害重排后抛 `ValueError`，
    测试**报错**而不是**失败**——读 CI 的人会以为是守卫坏了，
    而不是被守的不变量破了。这里换成带说明的 assert。
    一律先过 `_code()`：锚点可能只存在于注释里。
    """
    code = _code(_read(rel))
    assert marker in code, (
        f"{rel} 里找不到锚点 {marker!r}——被守的写法大概被改名或挪走了，"
        f"请同步更新这条守卫（不要直接删）")
    return code[code.index(marker):code.index(marker) + span]


def _source_files() -> list[Path]:
    return sorted(list(APP.rglob("*.tsx")) + list(APP.rglob("*.ts"))
                  + list(COMPONENTS.rglob("*.tsx")) + list(LIB.rglob("*.ts")))


@requires_frontend
class NoNativeDialogsTests(unittest.TestCase):
    """原生 confirm/prompt/alert 必须全部迁到应用内组件。

    原生框是系统浅色样式，贴在深色控制台里视觉断裂；更关键的是它无法承载
    "输入名称确认"这种强确认——而删除渠道会级联删掉 Key/代理/模型/日志。
    """

    def test_pages_do_not_call_native_confirm_or_prompt(self):
        offenders: list[str] = []
        for f in _source_files():
            if f.name == "ui.tsx":
                continue  # 降级兜底那一处允许
            code = _code(f.read_text(encoding="utf-8"))
            if re.search(r"(?<![\w.])(confirm|prompt|alert)\s*\(", code):
                offenders.append(f.relative_to(FRONTEND).as_posix())
        self.assertEqual(offenders, [], f"仍在调用原生对话框：{offenders}")

    def test_confirm_fallback_is_deliberate_not_silent(self):
        """宿主未挂载时降级回原生 confirm —— 绝不能静默返回 false，
        否则按钮点了像没反应，比样式丑严重得多。"""
        src = _read("components/ui.tsx")
        body = _func_body(src, "export function confirmDialog")
        self.assertIn("window.confirm", body)
        self.assertNotIn("Promise.resolve(false)", body)


@requires_frontend
class DashboardUsageCacheTests(unittest.TestCase):
    """仪表盘用量缓存：TTL 必须真的生效，重校验结果必须写回 state。

    回归的缺陷有两层：`USAGE_CACHE_TTL_MS` 声明后从未被读取（缓存整页生命周期
    不过期），且缓存命中分支只把新数据塞进 Map 就丢弃返回值。合起来的表现是
    "KPI 每 10s 在跳、用量图永久停在首次加载的数字，点刷新也不动"。
    """

    def test_ttl_constant_is_actually_read(self):
        # 必须用 _code：修复说明的注释里就写着 "USAGE_CACHE_TTL_MS 却从未被读取"，
        # 数原文会得到一个虚高的次数，守卫就白写了。
        code = _code(_read("app/(console)/dashboard/page.tsx"))
        uses = len(re.findall(r"\bUSAGE_CACHE_TTL_MS\b", code))
        self.assertGreaterEqual(
            uses, 2,
            "USAGE_CACHE_TTL_MS 只出现一次 = 只声明未使用，缓存永不过期")

    def test_cache_hit_branch_writes_usage_back_to_state(self):
        branch = _block("app/(console)/dashboard/page.tsx", "if (cached) {")
        self.assertIn(
            "setUsage(", branch,
            "缓存命中分支必须把后台重校验的结果写回 state，否则是 stale-forever")

    def test_cache_entries_carry_a_timestamp(self):
        src = _read("app/(console)/dashboard/page.tsx")
        self.assertRegex(
            src, r"Map<string,\s*\{[^}]*at:\s*number[^}]*\}>",
            "缓存值必须带抓取时间戳，TTL 才可能生效")


@requires_frontend
class KeysMaskHeuristicTests(unittest.TestCase):
    """编辑 Key 不得再用字符串猜测"这个值是不是脱敏串"。

    后端 crypto.mask_secret 自称"脱敏口径的单一事实来源"；前端用
    includes("*") 反推它，等于把同一份口径抄第二遍且无同步机制。
    """

    def test_no_mask_character_heuristic(self):
        code = _code(_read("app/(console)/keys/page.tsx"))
        self.assertNotIn('includes("*")', code)
        self.assertNotIn("includes('\u2022\u2022')", code)

    def test_edit_form_uses_dedicated_draft_state(self):
        code = _code(_read("app/(console)/keys/page.tsx"))
        self.assertIn("apiKeyDraft", code)
        body = _func_body(code, "function openEdit")
        self.assertIn("setApiKeyDraft", body,
                      "打开弹窗必须清空 draft，否则上次输入会提交到新行")


@requires_frontend
class ModalDismissableTests(unittest.TestCase):
    """Modal 必须能禁止外部关闭，且机密弹窗要用上它。"""

    def test_modal_supports_dismissable(self):
        code = _code(_read("components/ui.tsx"))
        self.assertIn("dismissable", code)
        body = _func_body(code, "export function Modal")
        # Esc 监听与遮罩点击都要受控
        self.assertRegex(body, r"if \(!open \|\| !dismissable\) return")
        self.assertIn("dismissable ? onClose : undefined", body)

    def test_modal_traps_and_restores_focus(self):
        """没有焦点陷阱时，Tab 会一项项跳过用户根本看不见的背景内容。"""
        body = _func_body(_read("components/ui.tsx"), "export function Modal")
        self.assertIn("trapTab", body)
        self.assertIn("restoreRef", body)

    def test_one_time_key_modal_cannot_be_dismissed_by_accident(self):
        block = _block("app/(console)/api-keys/page.tsx",
                       "open={!!createdKey}", span=400)
        self.assertIn("dismissable={false}", block,
                      "只显示一次的密钥，误按 Esc 就是永久丢失")


@requires_frontend
class SelectionScopeTests(unittest.TestCase):
    """全选/反选/表头基准必须作用于 filtered（当前筛选结果）。

    models 页一直是正确写法；keys / proxies 按全集算，于是筛出 3 行、
    点表头全选会选中上千条隐藏行，然后一次批量删除就把它们全删了。
    """

    def test_toggle_all_uses_filtered_not_full_list(self):
        for page, full in (("app/(console)/keys/page.tsx", "keys"),
                           ("app/(console)/proxies/page.tsx", "proxies")):
            with self.subTest(page=page):
                body = _func_body(_read(page), "function toggleAll")
                self.assertIn("filtered", body)
                self.assertNotIn(f"{full}.map", body)
                self.assertNotIn(f"{full}.length", body)

    def test_invert_selection_uses_filtered(self):
        for page, full in (("app/(console)/keys/page.tsx", "keys"),
                           ("app/(console)/proxies/page.tsx", "proxies")):
            with self.subTest(page=page):
                body = _func_body(_read(page), "function invertSelection")
                self.assertIn("filtered", body)
                self.assertNotIn(f"for (const p of {full})", body)
                self.assertNotIn(f"for (const k of {full})", body)

    def test_header_checkbox_compares_against_filtered(self):
        for page, full in (("app/(console)/keys/page.tsx", "keys"),
                           ("app/(console)/proxies/page.tsx", "proxies")):
            with self.subTest(page=page):
                code = _code(_read(page))
                # 直接断言形状，而不是靠 ariaLabel 定位再截一段：
                # 页面上 ariaLabel 出现很多次，第一个未必是表头那个。
                self.assertIn(
                    "checked={filtered.length > 0 && selected.size === filtered.length}",
                    code, "表头全选框的比较基准必须是 filtered")
                self.assertNotIn(f"checked={full}.length > 0", code)


@requires_frontend
class ProxyFilterPredicateTests(unittest.TestCase):
    """列表过滤、"匹配 N" 计数、区间选择必须共用同一个谓词。

    此前 kwMatched 只比 name/host，列表过滤额外比 group_name，
    于是按分组名搜索时计数说"匹配 0"、下面却列出十几行。
    """

    def test_single_shared_predicate(self):
        code = _code(_read("app/(console)/proxies/page.tsx"))
        self.assertIn("function matchKw", code)
        # 定义 1 处 + 列表过滤 + 区间选择；"匹配 N" 直接取 filtered.length，
        # 所以最少 3 处。低于 3 说明有人又抄了一份内联谓词。
        self.assertGreaterEqual(len(re.findall(r"\bmatchKw\(", code)), 3,
                                "matchKw 必须被过滤与区间选择共同使用")

    def test_no_duplicate_inline_keyword_filters(self):
        code = _code(_read("app/(console)/proxies/page.tsx"))
        # group_name 只应出现在 matchKw 里，不该在别处再抄一份谓词
        self.assertEqual(code.count('group_name || ""'), 1,
                         "关键字谓词被复制成多份，迟早会跑偏")


@requires_frontend
class SubmitGuardCoverageTests(unittest.TestCase):
    """每个带表单弹窗的页面都必须有在途防重。

    proxy-groups 曾是唯一漏掉的一页：双击保存 = 两个同名分组（名称无唯一约束）。
    """

    FORM_PAGES = [
        "app/(console)/keys/page.tsx",
        "app/(console)/proxies/page.tsx",
        "app/(console)/models/page.tsx",
        "app/(console)/channels/page.tsx",
        "app/(console)/api-keys/page.tsx",
        "app/(console)/proxy-groups/page.tsx",
    ]

    def test_form_pages_import_submit_guard(self):
        for page in self.FORM_PAGES:
            with self.subTest(page=page):
                src = _read(page)
                self.assertIn("useSubmitGuard", src,
                              f"{page} 有表单但没有提交防重")

    def test_import_actions_are_guarded_too(self):
        """批量导入此前没有在途状态：双击就是两次导入。"""
        for page in ("app/(console)/keys/page.tsx",
                     "app/(console)/proxies/page.tsx"):
            with self.subTest(page=page):
                src = _read(page)
                body = _func_body(src, "async function doImport")
                self.assertIn("importing", body)


@requires_frontend
class SingleSourceStatusLabelsTests(unittest.TestCase):
    """状态中文名只能有一份。

    dashboard 里曾另有一份与 ui.tsx badgeLabels 逐字相同的 statusLabels，
    后端新增状态时只会改到其中一份，另一份静默漏翻成裸英文枚举。
    """

    def test_dashboard_does_not_redefine_a_label_map(self):
        code = _code(_read("app/(console)/dashboard/page.tsx"))
        self.assertNotIn("const statusLabels", code)
        self.assertIn("statusLabel", code, "仪表盘应复用 ui.tsx 的 statusLabel")

    def test_ui_exports_the_single_map(self):
        code = _code(_read("components/ui.tsx"))
        self.assertIn("export const STATUS_LABELS", code)
        self.assertIn("export function statusLabel", code)


@requires_frontend
class DesignTokenAdherenceTests(unittest.TestCase):
    """颜色必须走令牌，别留游离文件。"""

    def test_no_zinc_palette_or_hardcoded_bg_outside_tokens(self):
        offenders: list[str] = []
        for f in _source_files():
            code = _code(f.read_text(encoding="utf-8"))
            if re.search(r"text-zinc-\d|bg-\[#0a0a0f\]", code):
                offenders.append(f.relative_to(FRONTEND).as_posix())
        self.assertEqual(offenders, [], f"游离于设计令牌之外：{offenders}")

    def test_viewport_and_theme_color_declared(self):
        """不导出 viewport 时 Next 16 会告警；没有 themeColor 时移动端
        地址栏是白的，接在 #0c0d0f 的深色页面外面非常跳。"""
        src = _read("app/layout.tsx")
        self.assertIn("export const viewport", src)
        self.assertIn("themeColor", src)


@requires_frontend
class ChatReasoningTests(unittest.TestCase):
    """思考流的两个缺陷：结束后不收起、think 变体不剥离。"""

    def test_both_think_tag_spellings_are_stripped(self):
        code = _code(_read("app/(console)/chat/page.tsx"))
        # 单一拼写的旧正则不得复活
        self.assertNotIn("<thinking>", code,
                         "只认 thinking 的旧正则会漏掉 think 变体，标签会字面渲染进正文")
        self.assertRegex(code, r"<\(think\|thinking\)>",
                         "思考标签正则必须同时覆盖两种拼写")

    def test_reasoning_block_collapse_is_controlled(self):
        """`useState(autoOpen)` 只在挂载时取一次值，而思考块必然在
        sending=true 期间挂载 —— 于是"结束自动收起"从未生效。"""
        body = _func_body(_code(_read("app/(console)/chat/page.tsx")),
                          "function ReasoningBlock")
        self.assertNotIn("useState(autoOpen", body)
        self.assertIn("userOpen", body)


@requires_frontend
class ChannelScopeTests(unittest.TestCase):
    """删除当前渠道必须把全局 slug 切走。

    后端对未知 X-Channel 是静默回落默认渠道（channel_service.resolve），
    不切就会让控制台顶着已删渠道的名字、把后续增删改查打到另一个渠道上。
    """

    def test_remove_reassigns_channel_when_deleting_active(self):
        src = _read("app/(console)/channels/page.tsx")
        body = _func_body(src, "async function remove")
        self.assertIn("getChannel()", body)
        self.assertIn("setChannel(", body,
                      "删除当前渠道后必须显式切走 slug，否则静默换作用域")


@requires_frontend
class RenderCostGuardsTests(unittest.TestCase):
    """钉住"大列表交互不再整片重渲染"这组性能不变量。

    为什么用源码断言而不是浏览器 profiling：前端零 JS 测试依赖，没有
    vitest / playwright 组件级 harness，而这类改动一旦回退**不会有任何功能
    测试变红**——12 列 × 200 行 ≈ 6000 个元素重新协调只是"点一下卡一下"，
    没人会为此写断言。所以把结构本身钉住。
    """

    def test_data_table_header_is_sticky(self):
        """12 列表格展开到 200 行后纵向滚动会完全失去列对应关系。"""
        code = _code(_read("components/ui.tsx"))
        body = _func_body(code, "export function Th")
        self.assertIn("sticky top-0", body)
        # sticky 表头必须有实色背景，否则行会从表头底下透出来
        self.assertRegex(body, r"bg-\[#")

    def test_data_table_loading_is_skeleton_not_spinner(self):
        """加载态换成骨架行：首屏表格就有形状，列宽与滚动位置不跳。"""
        body = _func_body(_code(_read("components/ui.tsx")), "export function DataTable")
        self.assertIn("TableSkeleton", body)
        self.assertIn("aria-busy", body)

    def test_data_table_has_no_magic_colspan(self):
        code = _code(_read("components/ui.tsx"))
        self.assertNotIn("colSpan={50}", code)
        self.assertNotIn("colSpan={100}", code)

    def test_big_list_rows_are_memoized(self):
        """keys / proxies 的行必须是 memo 组件，且父组件传的是引用恒定的 actions。

        否则父组件任何 setState（勾一个复选框、切一个开关、busyId 变化）都会把
        200 行整片重渲染——这就是"点一下卡一下"。
        """
        for page, comp in (("app/(console)/keys/page.tsx", "KeyRow"),
                           ("app/(console)/proxies/page.tsx", "ProxyRow")):
            with self.subTest(page=page):
                code = _code(_read(page))
                self.assertIn(f"memo(function {comp}", code,
                              f"{page} 的行组件没有 memo 化")
                self.assertIn("rowActions", code)
                self.assertIn("useMemo(", code)
                # 行组件必须只通过 props 拿数据/动作，不能直接闭包父组件的 setter，
                # 否则 memo 立刻失效。用文本范围而不是括号配对：
                # `memo(function X({...}: {...}) {` 这种参数形态让配对定位不可靠。
                start = code.index(f"memo(function {comp}")
                end = code.index("export default function", start)
                row_src = code[start:end]
                for leaked in ("setSelected", "setBusyId(", "setEditItem("):
                    self.assertNotIn(leaked, row_src,
                                     f"{comp} 直接闭包了 {leaked}，memo 会失效")

    def test_list_filters_are_memoized(self):
        for page in ("app/(console)/keys/page.tsx",
                     "app/(console)/proxies/page.tsx",
                     "app/(console)/models/page.tsx"):
            with self.subTest(page=page):
                code = _read(page)
                self.assertIn("const filtered = useMemo(", code,
                              f"{page} 的过滤没有 memo：每次 setState 都重算全集")
                self.assertIn("const visible = useMemo(", code,
                              f"{page} 的窗口切片没有 memo")

    def test_log_poll_refreshes_silently_and_incrementally(self):
        """日志页自动刷新必须是静默增量。

        原来每轮都 setLoading(true)，DataTable 就把整张表换成骨架行——等于每 5 秒
        把列表清空一次再长回来；而且为了保住"加载更多"展开的窗口按同等大小重拉，
        滚到 1000 行后就是每 5 秒重传重渲染 1000 行。
        """
        code = _code(_read("app/(console)/request-logs/page.tsx"))
        self.assertRegex(code, r"load\(true\)",
                         "轮询没有走静默分支")
        self.assertIn("const incremental = silent", code)
        # 手动刷新按钮不能把 MouseEvent 当成 silent 参数
        self.assertNotIn("<Button onClick={load}", code,
                         "onClick={load} 会把事件对象当成 silent（真值）")


