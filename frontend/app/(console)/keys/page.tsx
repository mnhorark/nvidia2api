"use client";

import { useCallback, useEffect, useState } from "react";
import { Ban, Check, FlaskConical, Gauge, Pencil, Plus, RefreshCw, Trash2, Upload, Wand2 } from "lucide-react";
import { api, asList, Channel, ChannelKey } from "@/lib/api";
import {
  Badge,
  BatchBar,
  Button,
  Checkbox,
  DataTable,
  Field,
  fmtTime,
  IconButton,
  Input,
  Modal,
  PageHeader,
  safePct,
  Td,
  Textarea,
  Th,
} from "@/components/ui";
import { toast } from "@/components/toaster";

interface ImportResult {
  success?: number;
  duplicate?: number;
  invalid?: number;
  failed?: number;
  detail?: unknown;
  [k: string]: unknown;
}

export default function ChannelKeysPage() {
  const [keys, setKeys] = useState<ChannelKey[]>([]);
  const [channelName, setChannelName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [genCount, setGenCount] = useState(50);
  const [editItem, setEditItem] = useState<Partial<ChannelKey> | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [k, ch] = await Promise.all([
        api.get("/api/admin/keys"),
        api.get<{ results: Channel[]; current: string }>("/api/admin/channels"),
      ]);
      setKeys(asList<ChannelKey>(k));
      setSelected(new Set());
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

  async function doImport() {
    try {
      const res = await api.post<ImportResult>("/api/admin/keys/import", {
        text: importText,
      });
      setImportResult(res);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导入失败");
    }
  }

  function generateAnonymous() {
    const n = Math.max(1, Math.min(Math.floor(Number(genCount) || 1) || 1, 5000));
    const lines = Array.from(
      { length: n },
      (_, i) => `匿名线路 ${String(i + 1).padStart(3, "0")}---`
    );
    setImportText(lines.join("\n"));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!editItem) return;
    const body: Record<string, unknown> = { name: editItem.name };
    // 留空表示不修改；各渠道 Key 的格式不同，不做前缀校验
    if (editItem.api_key && !editItem.api_key.includes("••") && !editItem.api_key.includes("*")) {
      body.api_key = editItem.api_key;
    }
    if (editItem.rpm_limit) body.rpm_limit = editItem.rpm_limit;
    try {
      if (editItem.id) {
        await api.patch(`/api/admin/keys/${editItem.id}`, body);
      } else {
        await api.post("/api/admin/keys", body);
      }
      setEditItem(null);
      load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }

  async function toggle(k: ChannelKey) {
    setBusyId(k.id);
    try {
      await api.patch(`/api/admin/keys/${k.id}`, {
        enabled: !(k.enabled ?? k.status !== "disabled"),
      });
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(k: ChannelKey) {
    if (!confirm(`确认删除 ${k.name}？`)) return;
    try {
      await api.del(`/api/admin/keys/${k.id}`);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  }

  async function test(k: ChannelKey) {
    setBusyId(k.id);
    try {
      await api.post(`/api/admin/keys/${k.id}/test`, {});
      toast.success(`${k.name} 测试完成`);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "测试失败");
    } finally {
      setBusyId(null);
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

  function toggleAll() {
    setSelected((prev) =>
      prev.size === keys.length ? new Set() : new Set(keys.map((k) => k.id))
    );
  }

  function invertSelection() {
    setSelected((prev) => {
      const next = new Set<number>();
      for (const k of keys) {
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
    if (action === "delete" && !confirm(`确认删除选中的 ${selected.size} 个 Key？`)) return;
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

  function setRpmBatch() {
    if (selected.size === 0) return;
    const raw = window.prompt(
      `设置选中的 ${selected.size} 个 Key 的 RPM 限制（0=不限流）`,
      "40"
    );
    if (raw === null) return;
    const n = Number(raw.trim());
    if (!Number.isFinite(n) || n < 0) {
      toast.error("RPM 需要是非负整数");
      return;
    }
    void batch("set_rpm", Math.floor(n));
  }

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
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button variant="primary" onClick={() => setEditItem({ name: "", rpm_limit: 40 })}>
              <Plus size={14} /> 添加 Key
            </Button>
          </>
        }
      />

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      <BatchBar count={selected.size}>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("enable")}>启用</Button>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("disable")}>禁用</Button>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("test")}>
          <FlaskConical size={13} /> 测试
        </Button>
        <Button size="sm" disabled={batchBusy} onClick={setRpmBatch}>
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
        empty="暂无 Key，点击右上角添加或批量导入"
        head={
          <>
            <Th>
              <Checkbox
                ariaLabel="全选"
                checked={keys.length > 0 && selected.size === keys.length}
                indeterminate={selected.size > 0 && selected.size < keys.length}
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
        {keys.map((k) => {
          const enabled = k.enabled ?? k.status !== "disabled";
          return (
            <tr key={k.id} className="transition-colors hover:bg-white/[0.025]">
              <Td>
                <Checkbox
                  ariaLabel={`选择 ${k.name}`}
                  checked={selected.has(k.id)}
                  onChange={() => toggleOne(k.id)}
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
                    disabled={busyId === k.id}
                    onClick={() => toggle(k)}
                  >
                    {enabled ? <Ban size={14} /> : <Check size={14} />}
                  </IconButton>
                  <IconButton
                    title="测试"
                    aria-label="测试"
                    disabled={busyId === k.id}
                    onClick={() => test(k)}
                  >
                    <FlaskConical size={14} />
                  </IconButton>
                  <IconButton
                    title="编辑"
                    aria-label="编辑"
                    onClick={() => setEditItem(k)}
                  >
                    <Pencil size={14} />
                  </IconButton>
                  <IconButton
                    title="删除"
                    aria-label="删除"
                    danger
                    onClick={() => remove(k)}
                  >
                    <Trash2 size={14} />
                  </IconButton>
                </div>
              </Td>
            </tr>
          );
        })}
      </DataTable>

      {/* 批量导入 */}
      <Modal
        open={importOpen}
        wide
        title="批量导入渠道 Key"
        onClose={() => {
          setImportOpen(false);
          setImportResult(null);
          setImportText("");
        }}
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
            <Button variant="primary" onClick={() => { setImportOpen(false); setImportResult(null); setImportText(""); }}>
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
              <span className="text-xs text-mute">快速生成匿名线路：</span>
              <Input
                type="number"
                min={1}
                max={5000}
                value={genCount}
                onChange={(e) => setGenCount(Number(e.target.value))}
                className="w-24"
              />
              <span className="text-xs text-mute">条</span>
              <Button size="sm" onClick={generateAnonymous}>
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
              <Button onClick={() => setImportOpen(false)}>取消</Button>
              <Button variant="primary" onClick={doImport} disabled={!importText.trim()}>
                导入
              </Button>
            </div>
          </>
        )}
      </Modal>

      {/* 新增/编辑 */}
      <Modal
        open={!!editItem}
        title={editItem?.id ? "编辑 Key" : "添加渠道 Key"}
        onClose={() => setEditItem(null)}
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
                onChange={(e) => setEditItem((p) => ({ ...p, api_key: e.target.value }))}
                required
              />
            </Field>
          )}
          <Field label="RPM 限制">
            <Input
              type="number"
              min={1}
              value={editItem?.rpm_limit ?? 40}
              onChange={(e) =>
                setEditItem((p) => ({ ...p, rpm_limit: Number(e.target.value) }))
              }
            />
          </Field>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setEditItem(null)}>
              取消
            </Button>
            <Button variant="primary" type="submit">
              保存
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
