"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Ban, Check, Eraser, FlaskConical, Gauge, Pencil, Plus, RefreshCw, Search, Trash2, Upload, Wand2 } from "lucide-react";
import { api, asList, Channel, ChannelKey } from "@/lib/api";
import { useSubmitGuard } from "@/lib/use-submit-guard";
import {
  Badge,
  BatchBar,
  Button,
  Checkbox,
  DataTable,
  ErrorBanner,
  Field,
  fmtTime,
  IconButton,
  Input,
  Modal,
  PageHeader,
  safePct,
  Select,
  Td,
  Textarea,
  Th,
  confirmDialog,
} from "@/components/ui";
import { toast } from "@/components/toaster";

// 大列表渲染窗口：每次“加载更多”展开的行数（两千+ Key 全量渲染会卡死页面）
const RENDER_WINDOW = 200;

interface ImportResult {
  success?: number;
  duplicate?: number;
  invalid?: number;
  failed?: number;
  detail?: unknown;
  [k: string]: unknown;
}

interface RowActions {
  toggleOne: (id: number) => void;
  toggle: (k: ChannelKey) => void;
  test: (k: ChannelKey) => void;
  remove: (k: ChannelKey) => void;
  openEdit: (k: ChannelKey) => void;
}

/**
 * memo 过的行。
 *
 * 一个渠道可以有上千把 Key，默认渲染窗口 200 行 × 约 10 个单元格。父组件任何
 * setState（勾选、切开关、busyId 变化）原本都会把 200 行整片重渲染一遍——
 * 勾选一个复选框要等 6000 个元素重新协调，这就是"点一下卡一下"的来源。
 * 行只依赖自己的 k / selected / busy 与一个引用恒定的 actions 对象，
 * 于是每次交互实际只有 1-2 行重渲染。
 */
const KeyRow = memo(function KeyRow({
  k,
  selected,
  busy,
  actions,
}: {
  k: ChannelKey;
  selected: boolean;
  busy: boolean;
  actions: RowActions;
}) {
  const enabled = k.enabled ?? k.status !== "disabled";
  return (
    <tr className="transition-colors hover:bg-white/[0.025]">
      <Td>
        <Checkbox
          ariaLabel={`选择 ${k.name}`}
          checked={selected}
          onChange={() => actions.toggleOne(k.id)}
        />
      </Td>
      <Td className="font-medium text-gray-200">{k.name}</Td>
      <Td>
        <code className="block max-w-[220px] truncate font-mono text-xs text-faint" title={k.api_key}>
          {k.api_key}
        </code>
      </Td>
      <Td>
        <Badge status={k.status} />
      </Td>
      <Td className="text-mute">{k.rpm_limit ?? 40}/分钟</Td>
      <Td>{k.minute_request_count ?? 0}</Td>
      <Td>{safePct(k.success_count, k.success_count + k.failure_count)}</Td>
      <Td>
        <span className="text-ok">{k.success_count}</span>
        <span className="text-faint"> / </span>
        <span className="text-err/80">{k.failure_count}</span>
      </Td>
      <Td className="text-xs text-faint">{fmtTime(k.last_used_at)}</Td>
      <Td>
        <div className="flex items-center gap-0.5">
          <IconButton
            title={enabled ? "禁用" : "启用"}
            aria-label={enabled ? "禁用" : "启用"}
            disabled={busy}
            onClick={() => actions.toggle(k)}
          >
            {enabled ? <Ban size={14} /> : <Check size={14} />}
          </IconButton>
          <IconButton
            title="测试"
            aria-label="测试"
            disabled={busy}
            onClick={() => actions.test(k)}
          >
            <FlaskConical size={14} />
          </IconButton>
          <IconButton
            title="编辑"
            aria-label="编辑"
            onClick={() => actions.openEdit(k)}
          >
            <Pencil size={14} />
          </IconButton>
          <IconButton
            title="删除"
            aria-label="删除"
            danger
            onClick={() => actions.remove(k)}
          >
            <Trash2 size={14} />
          </IconButton>
        </div>
      </Td>
    </tr>
  );
});

export default function ChannelKeysPage() {
  const [keys, setKeys] = useState<ChannelKey[]>([]);
  const [channelName, setChannelName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  // 导入在途状态：此前没有，双击就是两次导入
  const [importing, setImporting] = useState(false);
  const [genCount, setGenCount] = useState(50);
  // 批量导入快捷生成：模式 = 匿名(无 Key) / public(复用同一 Key) / sk(前缀+任意后缀)
  const [genMode, setGenMode] = useState<"anon" | "public" | "sk">("anon");
  const [genKey, setGenKey] = useState("sk-");
  const [editItem, setEditItem] = useState<Partial<ChannelKey> | null>(null);
  // 新增表单里"用户本次真正输入的 Key"。与 editItem 分开存：
  // editItem 在编辑态是从列表行带进来的（含后端脱敏后的 api_key），
  // 若复用它判断"是否要提交 Key"，就得靠 includes("*") 反推后端掩码口径——
  // 那是把 crypto.mask_secret（后端 docstring 自称"脱敏口径的单一事实来源"）
  // 在前端复制第二份，口径一改就会把掩码串当新 Key 写库；反方向上，真实值
  // 含 * 的 Key 会被判成"没改"而静默丢弃。分开存就不存在这个猜测。
  const [apiKeyDraft, setApiKeyDraft] = useState("");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [saving, submit] = useSubmitGuard();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  // 本地搜索：按名称 / 掩码后的 Key 过滤（全量数据仍在 keys 里，统计、批量选择不受影响）
  const [q, setQ] = useState("");
  // 渲染窗口大小：行内操作触发的全量 load() 不重置它，仅搜索词变化时重置
  const [windowSize, setWindowSize] = useState(RENDER_WINDOW);

  // 过滤与切片都要 memo：一个渠道可以有上千把 Key，而勾选一行、切换一行开关
  // 都会 setState —— 不 memo 的话每次交互都要重新 filter 全集并重新 slice，
  // 更贵的是下面 200 行会整片重渲染。
  const needle = q.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      needle
        ? keys.filter(
            (k) =>
              (k.name || "").toLowerCase().includes(needle) ||
              (k.api_key || "").toLowerCase().includes(needle),
          )
        : keys,
    [keys, needle],
  );
  const visible = useMemo(
    () => filtered.slice(0, windowSize),
    [filtered, windowSize],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [k, ch] = await Promise.all([
        api.get("/api/admin/keys"),
        api.get<{ results: Channel[]; current: string }>("/api/admin/channels"),
      ]);
      setKeys(asList<ChannelKey>(k));
      // 保留仍然存在的选中项（见 proxies 页同样的说明）：行内测速/编辑都会重拉。
      setSelected((prev) => {
        if (prev.size === 0) return prev;
        const alive = new Set(asList<ChannelKey>(k).map((x) => x.id));
        const next = new Set<number>();
        for (const id of prev) if (alive.has(id)) next.add(id);
        return next;
      });
      const list = asList<Channel>(ch.results);
      setChannelName(list.find((c) => c.slug === ch.current)?.name ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 搜索词变化时回到初始窗口；load() 全量重拉不重置，避免已展开的行被收起
  useEffect(() => {
    setWindowSize(RENDER_WINDOW);
  }, [q]);

  /** 就地写入/更新一行：PATCH/POST 的响应就是权威整行序列化。
   *  单行操作重拉全表在千级 Key 下是 128KB + 全表扫描，纯浪费。
   *  只有"服务端批量改写"（导入/批量/清理失效）才需要 load()。 */
  function upsertRow(row: ChannelKey) {
    setKeys((prev) => {
      const idx = prev.findIndex((x) => x.id === row.id);
      if (idx < 0) return [...prev, row];
      const next = prev.slice();
      next[idx] = row;
      return next;
    });
  }

  /** 打开新增/编辑弹窗。两处都必须清空 apiKeyDraft：它是"本次输入的新 Key"，
   *  残留上一次的值会把旧 Key 提交到新行上。 */
  function openEdit(item: Partial<ChannelKey>) {
    setApiKeyDraft("");
    setEditItem(item);
  }

  function closeEdit() {
    setApiKeyDraft("");
    setEditItem(null);
  }

  async function doImport() {
    if (importing) return;
    setImporting(true);
    try {
      const res = await api.post<ImportResult>("/api/admin/keys/import", {
        text: importText,
      });
      setImportResult(res);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导入失败");
    } finally {
      setImporting(false);
    }
  }

  function closeImport() {
    setImportOpen(false);
    setImportResult(null);
    setImportText("");
  }

  function generateLines() {
    const n = Math.max(1, Math.min(Math.floor(Number(genCount) || 1) || 1, 5000));
    const pad = (i: number) => String(i + 1).padStart(3, "0");
    let lines: string[];
    if (genMode === "public") {
      const key = genKey.trim();
      if (!key) return;
      // 公共 Key 渠道（允许重复 Key）：多个槽位复用同一把 Key
      lines = Array.from({ length: n }, (_, i) => `公共线路 ${pad(i)}---${key}`);
    } else if (genMode === "sk") {
      // sk- 前置：后缀为随机串，每槽位独立 Key
      const prefix = genKey.trim() || "sk-";
      const seen = new Set<string>();
      lines = Array.from({ length: n }, (_, i) => {
        let suffix: string;
        do {
          suffix = Math.random().toString(36).slice(2, 12);
        } while (seen.has(suffix));
        seen.add(suffix);
        return `Key ${pad(i)}---${prefix}${suffix}`;
      });
    } else {
      // 匿名线路：无鉴权渠道的"无需 Key"槽位，Key 留空
      lines = Array.from({ length: n }, (_, i) => `匿名线路 ${pad(i)}---`);
    }
    setImportText(lines.join("\n"));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!editItem) return;
    // 在途防重：双击/连按回车不应重复提交（会创建出重复记录）
    await submit(async () => {
      const body: Record<string, unknown> = { name: editItem.name };
      // 只提交"本次表单里真正输入过"的 Key（见 apiKeyDraft 的注释）。
      // 编辑态不渲染 Key 输入框，draft 恒为空 → 不会碰 Key。
      const typed = apiKeyDraft.trim();
      if (typed) body.api_key = typed;
      if (editItem.rpm_limit != null) body.rpm_limit = editItem.rpm_limit;
      try {
        const row = editItem.id
          ? await api.patch<ChannelKey>(`/api/admin/keys/${editItem.id}`, body)
          : await api.post<ChannelKey>("/api/admin/keys", body);
        setEditItem(null);
        setApiKeyDraft("");
        upsertRow(row);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "保存失败");
      }
    });
  }

  async function toggle(k: ChannelKey) {
    setBusyId(k.id);
    try {
      const row = await api.patch<ChannelKey>(`/api/admin/keys/${k.id}`, {
        enabled: !(k.enabled ?? k.status !== "disabled"),
      });
      upsertRow(row);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(k: ChannelKey) {
    if (!(await confirmDialog({
      title: "删除 Key",
      message: <>确认删除 <b className="text-gray-100">{k.name}</b>？删除后该 Key 立即从调度池移除。</>,
      confirmText: "删除",
      danger: true,
    }))) return;
    try {
      await api.del(`/api/admin/keys/${k.id}`);
      // 204 无响应体，删除结果可本地推导：就地摘除
      setKeys((prev) => prev.filter((x) => x.id !== k.id));
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(k.id);
        return next;
      });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  }

  async function test(k: ChannelKey) {
    setBusyId(k.id);
    try {
      await api.post(`/api/admin/keys/${k.id}/test`, {});
      toast.success(`${k.name} 测试完成`);
      // 测试会改写该 Key 的 status/计数（服务端判定，响应体里没有），
      // 但影响面只有这一行：单行 GET 刷新，代替 128KB 全表重拉
      try {
        const fresh = await api.get<ChannelKey>(`/api/admin/keys/${k.id}`);
        if (fresh && fresh.id) upsertRow(fresh);
      } catch {
        /* 单行刷新失败不影响测试结论本身，等待下一次手动刷新 */
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "测试失败");
    } finally {
      setBusyId(null);
    }
  }

  // 失效 Key（status=invalid，401/403 鉴权失败）数量，驱动"清理失效"按钮
  const invalidCount = keys.filter((k) => k.status === "invalid").length;

  async function cleanupInvalid() {
    if (invalidCount === 0) return;
    if (!(await confirmDialog({
      title: "清理失效 Key",
      message: <>确认删除 <b className="text-gray-100">{invalidCount}</b> 个鉴权失败（401/403）的 Key？此操作不可恢复。</>,
      confirmText: `清理 ${invalidCount} 个`,
      danger: true,
    }))) return;
    setCleaning(true);
    try {
      const res = await api.post<{ deleted?: number }>("/api/admin/keys/cleanup-invalid", {});
      toast.success(`已清理 ${res.deleted ?? 0} 个失效 Key`);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "清理失败");
    } finally {
      setCleaning(false);
    }
  }

  function toggleOne(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // 全选/反选/表头基准一律作用于 **filtered**（当前搜索下真正可见的集合）。
  // 此前按全集 keys 计算：搜出 3 行、点表头全选，实际选中上千条隐藏 Key，
  // 然后一次批量删除就把它们全删了。models 页一直是正确写法。
  function toggleAll() {
    setSelected((prev) =>
      prev.size === filtered.length ? new Set() : new Set(filtered.map((k) => k.id))
    );
  }

  function invertSelection() {
    setSelected((prev) => {
      const next = new Set<number>();
      for (const k of filtered) {
        if (!prev.has(k.id)) next.add(k.id);
      }
      return next;
    });
  }

  async function batch(
    action: "enable" | "disable" | "delete" | "test" | "set_rpm",
    rpm?: number
  ) {
    if (selected.size === 0) return;
    if (action === "delete" && !(await confirmDialog({
      title: "批量删除 Key",
      message: <>确认删除选中的 <b className="text-gray-100">{selected.size}</b> 个 Key？此操作不可恢复。</>,
      confirmText: `删除 ${selected.size} 个`,
      danger: true,
    }))) return;
    setBatchBusy(true);
    try {
      const res = await api.post<{ succeeded?: number; results?: unknown[] }>(
        "/api/admin/keys/batch",
        { ids: [...selected], action, ...(rpm !== undefined ? { rpm } : {}) }
      );
      if (action === "test") toast.success("批量测试完成");
      else if (action === "set_rpm") toast.success(`已将 ${res.succeeded ?? 0} 个 Key 的 RPM 设为 ${rpm}`);
      else if (action === "enable") toast.success(`已启用 ${res.succeeded ?? 0} 个`);
      else if (action === "disable") toast.success(`已禁用 ${res.succeeded ?? 0} 个`);
      else toast.success("批量删除完成");
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "批量操作失败");
    } finally {
      setBatchBusy(false);
    }
  }

  // 批量改 RPM：此前用 window.prompt 收一个数字——原生框与深色主题断裂，
  // 且任何文本都能输进去，校验只能在 JS 里报错。改成带 number 输入框的弹窗。
  const [rpmModal, setRpmModal] = useState(false);
  const [rpmValue, setRpmValue] = useState(40);

  function openRpmModal() {
    if (selected.size === 0) return;
    setRpmValue(40);
    setRpmModal(true);
  }

  async function applyRpmModal() {
    const n = Math.floor(Number(rpmValue));
    if (!Number.isFinite(n) || n < 0) {
      toast.error("RPM 需要是非负整数");
      return;
    }
    setRpmModal(false);
    await batch("set_rpm", n);
  }

  // 行内操作的动作集合用一个**稳定引用**传给 memo 过的行组件。
  // 这些函数每次渲染都会重建（普通函数声明），直接传进 React.memo 的行等于
  // 每渲染一次父组件就让所有行的 props 变化、memo 完全失效。
  // 用 latest-ref 包一层：既保持上面所有逻辑原样不动，又让 rowActions 引用恒定，
  // 且永远调到最新的闭包。
  const fnsRef = useRef({ toggleOne, toggle, test, remove, openEdit });
  fnsRef.current = { toggleOne, toggle, test, remove, openEdit };
  const rowActions = useMemo(
    () => ({
      toggleOne: (id: number) => fnsRef.current.toggleOne(id),
      toggle: (k: ChannelKey) => fnsRef.current.toggle(k),
      test: (k: ChannelKey) => fnsRef.current.test(k),
      remove: (k: ChannelKey) => fnsRef.current.remove(k),
      openEdit: (k: ChannelKey) => fnsRef.current.openEdit(k),
    }),
    [],
  );

  return (
    <div>
      <PageHeader
        title="渠道 Keys"
        subtitle={
          channelName
            ? `管理「${channelName}」渠道的上游 API Key，各渠道独立统计与限流`
            : "管理当前渠道的上游 API Key，各渠道独立统计与限流"
        }
        actions={
          <>
            <Button onClick={() => setImportOpen(true)}>
              <Upload size={14} /> 批量导入
            </Button>
            <Button
              onClick={cleanupInvalid}
              loading={cleaning}
              disabled={invalidCount === 0}
              title={invalidCount === 0 ? "没有失效 Key" : `删除 ${invalidCount} 个鉴权失败的 Key`}
            >
              <Eraser size={14} /> 清理失效{invalidCount > 0 ? ` (${invalidCount})` : ""}
            </Button>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button variant="primary" onClick={() => openEdit({ name: "", rpm_limit: 40 })}>
              <Plus size={14} /> 添加 Key
            </Button>
          </>
        }
      />

      <ErrorBanner message={error} onRetry={load} />

      <div className="relative mb-4 max-w-sm">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
        <Input
          className="pl-9"
          placeholder="搜索名称 / Key…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <BatchBar count={selected.size}>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("enable")}>启用</Button>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("disable")}>禁用</Button>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("test")}>
          <FlaskConical size={13} /> 测试
        </Button>
        <Button size="sm" disabled={batchBusy} onClick={openRpmModal}>
          <Gauge size={13} /> 改 RPM
        </Button>
        <Button size="sm" variant="danger" disabled={batchBusy} onClick={() => batch("delete")}>
          <Trash2 size={13} /> 删除
        </Button>
        <span className="h-4 w-px bg-white/[0.12]" />
        <Button size="sm" disabled={batchBusy} onClick={invertSelection}>反选</Button>
        <Button size="sm" onClick={() => setSelected(new Set())}>取消</Button>
      </BatchBar>

      <DataTable
        loading={loading}
        empty={q.trim() ? "没有匹配的 Key" : "暂无 Key，点击右上角添加或批量导入"}
        head={
          <>
            <Th>
              <Checkbox
                ariaLabel="全选（当前搜索结果）"
                checked={filtered.length > 0 && selected.size === filtered.length}
                indeterminate={selected.size > 0 && selected.size < filtered.length}
                onChange={toggleAll}
              />
            </Th>
            <Th>名称</Th>
            <Th>Key</Th>
            <Th>状态</Th>
            <Th>限制</Th>
            <Th>本分钟请求</Th>
            <Th>成功率</Th>
            <Th>成功 / 失败</Th>
            <Th>最后使用</Th>
            <Th>操作</Th>
          </>
        }
      >
        {visible.map((k) => (
          <KeyRow
            key={k.id}
            k={k}
            selected={selected.has(k.id)}
            busy={busyId === k.id}
            actions={rowActions}
          />
        ))}
      </DataTable>

      {!loading && filtered.length > 0 && (
        <div className="mt-3 flex items-center justify-between text-xs text-faint">
          <span className="tabular-nums">
            已显示 {visible.length} / 共 {filtered.length}
            {q.trim() ? `（全部 ${keys.length}）` : ""}
          </span>
          {visible.length < filtered.length && (
            <Button size="sm" onClick={() => setWindowSize((n) => n + RENDER_WINDOW)}>
              加载更多
            </Button>
          )}
        </div>
      )}

      {/* 批量导入 */}
      <Modal
        open={importOpen}
        wide
        title="批量导入渠道 Key"
        dismissable={!importing}
        onClose={closeImport}
      >
        {importResult ? (
          <div>
            <p className="mb-4 text-[13px] text-gray-300">导入完成</p>
            <div className="mb-4 grid grid-cols-4 gap-3 text-center">
              {(
                [
                  ["成功", importResult.success ?? 0, "text-ok"],
                  ["重复", importResult.duplicate ?? 0, "text-warn"],
                  ["无效", importResult.invalid ?? 0, "text-err"],
                  ["失败", importResult.failed ?? 0, "text-mute"],
                ] as const
              ).map(([label, v, cls]) => (
                <div key={label} className="rounded-lg border border-line bg-white/[0.02] p-3">
                  <div className={`text-xl font-semibold tabular-nums ${cls}`}>{v}</div>
                  <div className="text-xs text-faint">{label}</div>
                </div>
              ))}
            </div>
            <Button variant="primary" onClick={closeImport}>
              完成
            </Button>
          </div>
        ) : (
          <>
            <p className="mb-3 text-xs leading-relaxed text-mute">
              每行一条，支持 <code className="text-gray-300">名称---key</code> 或仅{" "}
              <code className="text-gray-300">key</code>，未命名的按渠道自动命名；
              <code className="text-gray-300">名称---</code> 留空 Key 即匿名线路
            </p>
            <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-white/[0.02] px-3 py-2.5">
              <span className="text-xs text-mute">快速生成：</span>
              <Select
                value={genMode}
                onChange={(e) => setGenMode(e.target.value as "anon" | "public" | "sk")}
                className="w-44"
              >
                <option value="anon">匿名线路（无 Key）</option>
                <option value="public">public（复用同一 Key）</option>
                <option value="sk">sk- 前缀（随机 Key）</option>
              </Select>
              {genMode !== "anon" && (
                <Input
                  value={genKey}
                  onChange={(e) => setGenKey(e.target.value)}
                  placeholder={genMode === "public" ? "公共 Key（如 public）" : "Key 前缀，如 sk-"}
                  className="w-40"
                />
              )}
              <Input
                type="number"
                min={1}
                max={5000}
                value={genCount}
                onChange={(e) => setGenCount(Number(e.target.value))}
                className="w-24"
              />
              <span className="text-xs text-mute">条</span>
              <Button
                size="sm"
                onClick={generateLines}
                disabled={genMode === "public" && !genKey.trim()}
              >
                <Wand2 size={13} /> 生成
              </Button>
            </div>
            <Textarea
              rows={10}
              placeholder={"主账号01---sk-xxxxxxxx\nsk-yyyyyyyy"}
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
            />
            <div className="mt-4 flex justify-end gap-2">
              <Button onClick={closeImport} disabled={importing}>取消</Button>
              <Button variant="primary" onClick={doImport} loading={importing} disabled={!importText.trim()}>
                导入
              </Button>
            </div>
          </>
        )}
      </Modal>

      {/* 批量改 RPM */}
      <Modal
        open={rpmModal}
        title={`设置 ${selected.size} 个 Key 的 RPM`}
        onClose={() => setRpmModal(false)}
      >
        <div className="space-y-3.5">
          <Field label="每分钟请求上限（0 = 不限流）">
            <Input
              type="number"
              min={0}
              value={rpmValue}
              onChange={(e) => setRpmValue(Number(e.target.value))}
            />
          </Field>
          <p className="text-xs text-faint">
            覆盖式写入：选中的 {selected.size} 个 Key 全部改成这个值，包括原本单独设过 RPM 的。
          </p>
          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" onClick={() => setRpmModal(false)}>取消</Button>
            <Button variant="primary" type="button" onClick={applyRpmModal} disabled={batchBusy}>
              应用
            </Button>
          </div>
        </div>
      </Modal>

      {/* 新增/编辑 */}
      <Modal
        open={!!editItem}
        title={editItem?.id ? "编辑 Key" : "添加渠道 Key"}
        dismissable={!saving}
        onClose={closeEdit}
      >
        <form onSubmit={save} className="space-y-3.5">
          <Field label="名称">
            <Input
              value={editItem?.name ?? ""}
              onChange={(e) => setEditItem((p) => ({ ...p, name: e.target.value }))}
              required
            />
          </Field>
          {!editItem?.id && (
            <Field label="API Key">
              <Input
                placeholder="上游 API Key"
                value={apiKeyDraft}
                onChange={(e) => setApiKeyDraft(e.target.value)}
                required
              />
            </Field>
          )}
          <Field label="RPM 限制">
            <Input
              type="number"
              min={0}
              value={editItem?.rpm_limit ?? 40}
              onChange={(e) =>
                setEditItem((p) => ({ ...p, rpm_limit: Number(e.target.value) }))
              }
            />
            <p className="mt-1 text-xs text-faint">0 = 不限流</p>
          </Field>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={closeEdit}>
              取消
            </Button>
            <Button variant="primary" type="submit" loading={saving}>
              保存
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
