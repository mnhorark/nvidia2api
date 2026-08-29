"use client";

import { useCallback, useEffect, useState } from "react";
import { Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, asList, ProxyGroup } from "@/lib/api";
import {
  Badge,
  Button,
  DataTable,
  Field,
  fmtTime,
  IconButton,
  Input,
  Modal,
  PageHeader,
  Td,
  Th,
} from "@/components/ui";
import { toast } from "@/components/toaster";

export default function ProxyGroupsPage() {
  const [groups, setGroups] = useState<ProxyGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [edit, setEdit] = useState<Partial<ProxyGroup> | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setGroups(asList<ProxyGroup>(await api.get("/api/admin/proxy-groups")));
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!edit) return;
    try {
      const body = {
        name: edit.name,
        description: edit.description ?? "",
        country: edit.country ?? "",
      };
      if (edit.id) await api.patch(`/api/admin/proxy-groups/${edit.id}`, body);
      else await api.post("/api/admin/proxy-groups", body);
      setEdit(null);
      load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }

  async function remove(g: ProxyGroup) {
    if (!confirm(`确认删除分组 ${g.name}？分组内代理将变为未分组。`)) return;
    try {
      await api.del(`/api/admin/proxy-groups/${g.id}`);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  }

  return (
    <div>
      <PageHeader
        title="代理分组"
        subtitle="按地区 / 用途组织代理"
        actions={
          <>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button variant="primary" onClick={() => setEdit({ name: "" })}>
              <Plus size={14} /> 新建分组
            </Button>
          </>
        }
      />

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      <DataTable
        loading={loading}
        empty="暂无分组"
        head={
          <>
            <Th>名称</Th>
            <Th>国家/地区</Th>
            <Th>描述</Th>
            <Th>代理数</Th>
            <Th>状态</Th>
            <Th>更新时间</Th>
            <Th>操作</Th>
          </>
        }
      >
        {groups.map((g) => (
          <tr key={g.id} className="transition-colors hover:bg-white/[0.025]">
            <Td className="font-medium text-gray-200">{g.name}</Td>
            <Td className="text-mute">{g.country || "—"}</Td>
            <Td className="max-w-xs truncate text-faint">{g.description || "—"}</Td>
            <Td className="tabular-nums">{g.proxy_count ?? "—"}</Td>
            <Td>
              <Badge status={g.enabled ? "enabled" : "disabled"} />
            </Td>
            <Td className="text-xs text-faint">{fmtTime(g.updated_at)}</Td>
            <Td>
              <div className="flex items-center gap-0.5">
                <IconButton
                  title="编辑"
                  aria-label="编辑"
                  onClick={() => setEdit(g)}
                >
                  <Pencil size={14} />
                </IconButton>
                <IconButton
                  aria-label="删除"
                  danger
                  onClick={() => remove(g)}
                >
                  <Trash2 size={14} />
                </IconButton>
              </div>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Modal
        open={!!edit}
        title={edit?.id ? "编辑分组" : "新建分组"}
        onClose={() => setEdit(null)}
      >
        <form onSubmit={save} className="space-y-3.5">
          <Field label="名称">
            <Input
              value={edit?.name ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, name: e.target.value }))}
              required
            />
          </Field>
          <Field label="国家/地区">
            <Input
              value={edit?.country ?? ""}
              onChange={(e) => setEdit((p) => ({ ...p, country: e.target.value }))}
            />
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
