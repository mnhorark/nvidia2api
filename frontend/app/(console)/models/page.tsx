"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, Pencil, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { api, asList, Model } from "@/lib/api";
import {
  Badge,
  Button,
  Checkbox,
  DataTable,
  Field,
  fmtTime,
  Input,
  Modal,
  PageHeader,
  Td,
  Th,
  Toggle,
} from "@/components/ui";
import { toast } from "@/components/toaster";

export default function ModelsPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState("");
  const [edit, setEdit] = useState<Partial<Model> | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setModels(asList<Model>(await api.get("/api/admin/models")));
      setSelected(new Set());
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = models.filter(
    (m) =>
      !q ||
      m.model_name.toLowerCase().includes(q.toLowerCase()) ||
      (m.display_name || "").toLowerCase().includes(q.toLowerCase())
  );

  async function sync() {
    setSyncing(true);
    try {
      await api.post("/api/admin/models/sync", {});
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "同步失败");
    } finally {
      setSyncing(false);
    }
  }

  async function setEnabled(m: Model, enabled: boolean) {
    setBusyId(m.id);
    try {
      await api.patch(`/api/admin/models/${m.id}`, { enabled });
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(m: Model) {
    if (!confirm(`确认删除模型 ${m.model_name}？`)) return;
    try {
      await api.del(`/api/admin/models/${m.id}`);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
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
      prev.size === filtered.length ? new Set() : new Set(filtered.map((m) => m.id))
    );
  }

  /** 反选：选中当前未选中的行 */
  function invertSelection() {
    setSelected((prev) => {
      const next = new Set<number>();
      for (const m of filtered) {
        if (!prev.has(m.id)) next.add(m.id);
      }
      return next;
    });
  }

  async function batch(action: "enable" | "disable" | "delete") {
    if (selected.size === 0) return;
    if (action === "delete" && !confirm(`确认删除选中的 ${selected.size} 个模型？`)) return;
    setBatchBusy(true);
    try {
      await api.post("/api/admin/models/batch", { ids: [...selected], action });
      toast.success("批量操作完成");
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "批量操作失败");
    } finally {
      setBatchBusy(false);
    }
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!edit) return;
    try {
      const body = {
        model_name: edit.model_name,
        display_name: edit.display_name ?? "",
        alias: edit.alias ?? "",
        description: edit.description ?? "",
      };
      if (edit.id) await api.patch(`/api/admin/models/${edit.id}`, body);
      else await api.post("/api/admin/models", body);
      setEdit(null);
      load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }

  return (
    <div>
      <PageHeader
        title="模型"
        subtitle="只有启用的模型才会通过 OpenAI API 对外提供"
        actions={
          <>
            <Button onClick={sync} loading={syncing}>
              <Download size={14} /> 同步渠道模型
            </Button>
            <Button variant="primary" onClick={() => setEdit({ model_name: "" })}>
              <Plus size={14} /> 添加模型
            </Button>
            <Button onClick={load} loading={loading} aria-label="刷新">
              <RefreshCw size={14} />
            </Button>
          </>
        }
      />

      <div className="relative mb-4 max-w-sm">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
        <Input
          className="pl-9"
          placeholder="搜索模型名称…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      {selected.size > 0 && (
        <div className="glass animate-rise mb-3 flex items-center gap-3 px-4 py-2.5 text-sm">
          <span className="text-gray-400">
            已选 <b className="text-gray-100">{selected.size}</b> 项
          </span>
          <Button disabled={batchBusy} onClick={() => batch("enable")}>批量启用</Button>
          <Button disabled={batchBusy} onClick={() => batch("disable")}>批量禁用</Button>
          <Button disabled={batchBusy} onClick={() => batch("delete")}>
            <Trash2 size={14} /> 批量删除
          </Button>
          <Button disabled={selected.size === 0} onClick={invertSelection}>反选</Button>
          <Button onClick={() => setSelected(new Set())}>取消选择</Button>
        </div>
      )}

      <DataTable
        loading={loading}
        empty="暂无模型，点击“同步渠道模型”拉取"
        head={
          <>
            <Th>
              <Checkbox
                ariaLabel="全选"
                checked={filtered.length > 0 && selected.size === filtered.length}
                indeterminate={selected.size > 0 && selected.size < filtered.length}
                onChange={toggleAll}
              />
            </Th>
            <Th>模型名称</Th>
            <Th>对外名称</Th>
            <Th>显示名称</Th>
            <Th>来源</Th>
            <Th>状态</Th>
            <Th>启用</Th>
            <Th>更新时间</Th>
            <Th>操作</Th>
          </>
        }
      >
        {filtered.map((m) => (
          <tr key={m.id} className="hover:bg-white/[0.02]">
            <Td>
              <Checkbox
                ariaLabel={`选择 ${m.model_name}`}
                checked={selected.has(m.id)}
                onChange={() => toggleOne(m.id)}
              />
            </Td>
            <Td className="font-mono text-xs text-gray-200">{m.model_name}</Td>
            <Td className="font-mono text-xs text-accent">{m.public_name || m.model_name}</Td>
            <Td className="text-gray-400">{m.display_name || "—"}</Td>
            <Td className="text-gray-400">{m.provider || "—"}</Td>
            <Td>
              <Badge status={m.enabled ? "enabled" : "disabled"} />
            </Td>
            <Td>
              <Toggle
                checked={m.enabled}
                disabled={busyId === m.id}
                onChange={(v) => setEnabled(m, v)}
              />
            </Td>
            <Td className="text-xs text-gray-500">{fmtTime(m.updated_at)}</Td>
            <Td>
              <div className="flex items-center gap-1">
                <button
                  title="编辑"
                  aria-label="编辑"
                  onClick={() => setEdit(m)}
                  className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                >
                  <Pencil size={14} />
                </button>
                <button
                  aria-label="删除"
                  onClick={() => remove(m)}
                  className="rounded p-1.5 text-gray-500 hover:bg-red-500/15 hover:text-red-400"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Modal open={!!edit} title={edit?.id ? "编辑模型" : "添加模型"} onClose={() => setEdit(null)}>
        <form onSubmit={save} className="space-y-3">
          <Field label="模型名称">
            <Input
              placeholder="模型 ID，如 deepseek-ai/deepseek-r1"
              value={edit?.model_name ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, model_name: e.target.value }))}
              required
            />
          </Field>
          <Field label="显示名称">
            <Input
              value={edit?.display_name ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, display_name: e.target.value }))}
            />
          </Field>
          <Field label="对外名称（别名，留空则使用显示名称）">
            <Input
              placeholder="客户端在 /v1 里使用的模型名，如 gpt-4o-mini"
              value={edit?.alias ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, alias: e.target.value }))}
            />
            <p className="mt-1 text-xs text-gray-500">
              /v1/models 返回及 chat 请求时的模型名：别名 &gt; 显示名称 &gt; 原始模型名
            </p>
          </Field>
          <Field label="描述">
            <Input
              value={edit?.description ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, description: e.target.value }))}
            />
          </Field>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setEdit(null)}>
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
