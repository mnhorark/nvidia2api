"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, Pencil, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { api, asList, Model, ProxyGroup } from "@/lib/api";
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
  Select,
  Td,
  Th,
  Toggle,
} from "@/components/ui";
import { toast } from "@/components/toaster";

export default function ModelsPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [groups, setGroups] = useState<ProxyGroup[]>([]);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState("");
  const [edit, setEdit] = useState<Partial<Model> | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  // 附加别名输入框保存原始字符串（避免受控 split 吃掉用户输入的英文逗号），
  // 保存时才解析成数组
  const [aliasText, setAliasText] = useState("");

  const openEdit = (m?: Partial<Model>) => {
    setEdit(m ?? { model_name: "" });
    setAliasText((m?.aliases ?? []).join(", "));
  };

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [m, g] = await Promise.all([
        api.get("/api/admin/models"),
        api.get("/api/admin/proxy-groups"),
      ]);
      setModels(asList<Model>(m));
      setGroups(asList<ProxyGroup>(g));
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

  async function sync(prune = false) {
    setSyncing(true);
    try {
      const res = await api.post<{ pruned?: number }>("/api/admin/models/sync",
                                                      prune ? { prune: true } : {});
      await load();
      if (prune && (res?.pruned ?? 0) > 0) {
        toast.success(`同步完成，已清理 ${res.pruned} 个失效模型`);
      }
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

  function invertSelection() {
    setSelected((prev) => {
      const next = new Set<number>();
      for (const m of filtered) {
        if (!prev.has(m.id)) next.add(m.id);
      }
      return next;
    });
  }

  // 端点的紧凑标记：完整值放在 title 里悬停查看，避免表格被长 URL 撑宽
  function endpointLabel(ep?: string) {
    if (!ep) return null;
    const s = ep.trim();
    if (s.includes("/responses")) return "responses";
    if (s.includes("/messages")) return "messages";
    if (/^https?:\/\//i.test(s)) return "自定义 URL";
    return s;
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
        aliases: aliasText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        description: edit.description ?? "",
        proxy_group: edit.proxy_group ?? null,
        endpoint: edit.endpoint ?? "",
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
            <Button onClick={() => sync()} loading={syncing}>
              <Download size={14} /> 同步渠道模型
            </Button>
            <Button onClick={() => sync(true)} loading={syncing} title="同步并删除上游已下线的模型">
              <Download size={14} /> 同步并清理
            </Button>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button variant="primary" onClick={() => openEdit()}>
              <Plus size={14} /> 添加模型
            </Button>
          </>
        }
      />

      <div className="relative mb-4 max-w-sm">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
        <Input
          className="pl-9"
          placeholder="搜索模型名称…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      <BatchBar count={selected.size}>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("enable")}>启用</Button>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("disable")}>禁用</Button>
        <Button size="sm" variant="danger" disabled={batchBusy} onClick={() => batch("delete")}>
          <Trash2 size={13} /> 删除
        </Button>
        <span className="h-4 w-px bg-white/[0.12]" />
        <Button size="sm" disabled={batchBusy} onClick={invertSelection}>反选</Button>
        <Button size="sm" onClick={() => setSelected(new Set())}>取消</Button>
      </BatchBar>

      <DataTable
        loading={loading}
        empty="暂无模型，点击「同步渠道模型」拉取"
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
            <Th>模型</Th>
            <Th>代理分组</Th>
            <Th>端点</Th>
            <Th>状态</Th>
            <Th>启用</Th>
            <Th>更新时间</Th>
            <Th>操作</Th>
          </>
        }
      >
        {filtered.map((m) => (
          <tr key={m.id} className="transition-colors hover:bg-white/[0.025]">
            <Td>
              <Checkbox
                ariaLabel={`选择 ${m.model_name}`}
                checked={selected.has(m.id)}
                onChange={() => toggleOne(m.id)}
              />
            </Td>
            <Td className="max-w-[320px]">
              <div
                className="truncate font-mono text-xs text-gray-200"
                title={m.model_name}
              >
                {m.model_name}
              </div>
              {m.public_name && m.public_name !== m.model_name && (
                <div className="truncate text-[10px] text-accent" title={m.public_name}>
                  对外 {m.public_name}
                </div>
              )}
              {(m.aliases ?? []).length > 0 && (
                <div className="mt-0.5 flex flex-wrap gap-1">
                  {m.aliases!.map((a) => (
                    <span
                      key={a}
                      className="rounded border border-line bg-white/[0.03] px-1 py-px text-[9px] text-info"
                    >
                      {a}
                    </span>
                  ))}
                </div>
              )}
              {m.display_name && m.display_name !== m.model_name && (
                <div className="truncate text-[10px] text-mute" title={m.display_name}>
                  {m.display_name}
                </div>
              )}
            </Td>
            <Td>
              {m.proxy_group_name ? (
                <span className="rounded border border-line bg-white/[0.03] px-1.5 py-0.5 text-xs text-info">
                  {m.proxy_group_name}
                </span>
              ) : (
                <span className="text-faint">—</span>
              )}
            </Td>
            <Td>
              {m.endpoint ? (
                <span
                  className="rounded border border-line bg-white/[0.03] px-1.5 py-0.5 text-xs text-accent"
                  title={m.endpoint}
                >
                  {endpointLabel(m.endpoint)}
                </span>
              ) : (
                <span className="text-faint">chat</span>
              )}
            </Td>
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
            <Td className="whitespace-nowrap text-xs text-faint">{fmtTime(m.updated_at)}</Td>
            <Td>
              <div className="flex items-center gap-0.5">
                <IconButton
                  title="编辑"
                  aria-label="编辑"
                  onClick={() => openEdit(m)}
                >
                  <Pencil size={14} />
                </IconButton>
                <IconButton
                  aria-label="删除"
                  danger
                  onClick={() => remove(m)}
                >
                  <Trash2 size={14} />
                </IconButton>
              </div>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Modal open={!!edit} title={edit?.id ? "编辑模型" : "添加模型"} onClose={() => setEdit(null)}>
        <form onSubmit={save} className="space-y-3.5">
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
            <p className="mt-1 text-xs text-faint">
              /v1/models 返回及 chat 请求时的模型名：别名 &gt; 显示名称 &gt; 原始模型名
            </p>
          </Field>
          <Field label="附加对外名（多个别名，英文逗号分隔）">
            <Input
              placeholder="如：gpt-4o-mini, my-llm, chat-assistant"
              value={aliasText}
              onChange={(e) => setAliasText(e.target.value)}
            />
            <p className="mt-1 text-xs text-faint">
              一个模型可暴露多个可调用名字（类似模型映射）：所有对外名都会出现在 /v1/models，
              客户端可用其中任意一个调用，路由到同一个上游模型
            </p>
          </Field>
          <Field label="描述">
            <Input
              value={edit?.description ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, description: e.target.value }))}
            />
          </Field>
          <Field label="独立代理分组（留空则用渠道全部代理）">
            <Select
              value={edit?.proxy_group ?? ""}
              onChange={(e) =>
                setEdit((p) => ({
                  ...p,
                  proxy_group: e.target.value ? Number(e.target.value) : null,
                }))
              }
            >
              <option value="">默认（渠道全部代理）</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                  {g.proxy_count != null ? `（${g.proxy_count}）` : ""}
                </option>
              ))}
            </Select>
            <p className="mt-1 text-xs text-faint">
              设置后，该模型的请求只使用该分组内的代理线路，适合区域受限的模型
            </p>
          </Field>
          <Field label="独立端点（留空则用渠道默认 chat 端点）">
            <Input
              placeholder="/v1/responses 或 https://host/v1/responses"
              value={edit?.endpoint ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, endpoint: e.target.value }))}
            />
            <p className="mt-1 text-xs text-faint">
              部分模型走 Response API（/v1/responses）等非 chat 端点，在这里独立指定；
              支持完整 URL 或相对路径。系统会自动转换请求/响应格式。
            </p>
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
