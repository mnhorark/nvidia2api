"use client";

import { useCallback, useEffect, useState } from "react";
import { Ban, Check, FlaskConical, Pencil, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { api, asList, Channel, ChannelKey } from "@/lib/api";
import {
  Badge,
  Button,
  DataTable,
  Field,
  fmtTime,
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
  const [editItem, setEditItem] = useState<Partial<ChannelKey> | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [k, ch] = await Promise.all([
        api.get("/api/admin/keys"),
        api.get<{ results: Channel[]; current: string }>("/api/admin/channels"),
      ]);
      setKeys(asList<ChannelKey>(k));
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
            <Button variant="primary" onClick={() => setEditItem({ name: "", rpm_limit: 40 })}>
              <Plus size={14} /> 添加 Key
            </Button>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新状态
            </Button>
          </>
        }
      />

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      <DataTable
        loading={loading}
        empty="暂无 Key，点击右上角添加或批量导入"
        head={
          <>
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
            <tr key={k.id} className="hover:bg-white/[0.02]">
              <Td className="font-medium text-gray-200">{k.name}</Td>
              <Td>
                <code className="font-mono text-xs text-gray-500">{k.api_key}</code>
              </Td>
              <Td>
                <Badge status={k.status} />
              </Td>
              <Td className="text-gray-400">{k.rpm_limit ?? 40}/分钟</Td>
              <Td>{k.minute_request_count ?? 0}</Td>
              <Td>{safePct(k.success_count, k.success_count + k.failure_count)}</Td>
              <Td className="text-gray-500">
                <span className="text-accent">{k.success_count}</span>
                {" / "}
                <span className="text-red-400/80">{k.failure_count}</span>
              </Td>
              <Td className="text-xs text-gray-500">{fmtTime(k.last_used_at)}</Td>
              <Td>
                <div className="flex items-center gap-1">
                  <button
                    title={enabled ? "禁用" : "启用"}
                    disabled={busyId === k.id}
                    onClick={() => toggle(k)}
                    className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                  >
                    {enabled ? <Ban size={14} /> : <Check size={14} />}
                  </button>
                  <button
                    title="测试"
                    disabled={busyId === k.id}
                    onClick={() => test(k)}
                    className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                  >
                    <FlaskConical size={14} />
                  </button>
                  <button
                    title="编辑"
                    onClick={() => setEditItem(k)}
                    className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                  >
                    <Pencil size={14} />
                  </button>
                  <button
                    title="删除"
                    onClick={() => remove(k)}
                    className="rounded p-1.5 text-gray-500 hover:bg-red-500/15 hover:text-red-400"
                  >
                    <Trash2 size={14} />
                  </button>
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
            <p className="mb-4 text-sm text-gray-300">导入完成</p>
            <div className="mb-4 grid grid-cols-4 gap-3 text-center">
              {(
                [
                  ["成功", importResult.success ?? 0, "text-accent"],
                  ["重复", importResult.duplicate ?? 0, "text-amber-400"],
                  ["无效", importResult.invalid ?? 0, "text-red-400"],
                  ["失败", importResult.failed ?? 0, "text-gray-400"],
                ] as const
              ).map(([label, v, cls]) => (
                <div key={label} className="glass p-3">
                  <div className={`text-xl font-semibold ${cls}`}>{v}</div>
                  <div className="text-xs text-gray-500">{label}</div>
                </div>
              ))}
            </div>
            <Button variant="primary" onClick={() => { setImportOpen(false); setImportResult(null); setImportText(""); }}>
              完成
            </Button>
          </div>
        ) : (
          <>
            <p className="mb-3 text-xs leading-relaxed text-gray-500">
              每行一条，支持 <code className="text-gray-300">名称---key</code> 或仅 <code className="text-gray-300">key</code>，未命名的按渠道自动命名
            </p>
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
        <form onSubmit={save} className="space-y-3">
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
