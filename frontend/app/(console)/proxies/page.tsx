"use client";

import { useCallback, useEffect, useState } from "react";
import { Gauge, Globe, Pencil, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { api, asList, ChannelKey, Proxy, ProxyGroup } from "@/lib/api";
import {
  Badge,
  Button,
  Checkbox,
  DataTable,
  Field,
  fmtLatency,
  fmtTime,
  Input,
  Modal,
  PageHeader,
  Select,
  Td,
  Textarea,
  Th,
  Toggle,
} from "@/components/ui";
import { toast } from "@/components/toaster";

export default function ProxiesPage() {
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [groups, setGroups] = useState<ProxyGroup[]>([]);
  const [keyCount, setKeyCount] = useState(0);
  const [enabledCount, setEnabledCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [editItem, setEditItem] = useState<Partial<Proxy> | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [testingAll, setTestingAll] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);

  const maxProxies = Math.max(keyCount - 1, 0);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [p, g, k] = await Promise.all([
        api.get("/api/admin/proxies"),
        api.get("/api/admin/proxy-groups"),
        api.get("/api/admin/keys"),
      ]);
      const proxyList = asList<Proxy>(p);
      setProxies(proxyList);
      setSelected(new Set());
      setEnabledCount(proxyList.filter((x) => x.enabled).length);
      setGroups(asList<ProxyGroup>(g));
      setKeyCount(asList<ChannelKey>(k).length);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function setEnabled(p: Proxy, enabled: boolean) {
    setBusyId(p.id);
    try {
      await api.patch(`/api/admin/proxies/${p.id}`, { enabled });
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  }

  async function testOne(p: Proxy) {
    setBusyId(p.id);
    try {
      await api.post(`/api/admin/proxies/${p.id}/test`, {});
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "测速失败");
    } finally {
      setBusyId(null);
    }
  }

  async function fetchIp(p: Proxy) {
    setBusyId(p.id);
    try {
      await api.post(`/api/admin/proxies/${p.id}/fetch-ip`, {});
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "获取 IP 失败");
    } finally {
      setBusyId(null);
    }
  }

  async function testAll() {
    setTestingAll(true);
    try {
      await api.post("/api/admin/proxies/test-all", {});
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "测速失败");
    } finally {
      setTestingAll(false);
    }
  }

  async function remove(p: Proxy) {
    if (!confirm(`确认删除代理 ${p.name}？`)) return;
    try {
      await api.del(`/api/admin/proxies/${p.id}`);
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
      prev.size === proxies.length ? new Set() : new Set(proxies.map((p) => p.id))
    );
  }

  async function batch(action: "enable" | "disable" | "delete" | "test") {
    if (selected.size === 0) return;
    if (action === "delete" && !confirm(`确认删除选中的 ${selected.size} 个代理？`)) return;
    setBatchBusy(true);
    try {
      const res = await api.post<{
        succeeded?: number;
        skipped?: { id: number; name: string; reason: string }[];
        ok?: number;
        failed?: number;
      }>("/api/admin/proxies/batch", { ids: [...selected], action });
      if (action === "enable" && res.skipped && res.skipped.length > 0) {
        toast.error(`已启用 ${res.succeeded ?? 0} 个，${res.skipped.length} 个超出上限被跳过`);
      } else if (action === "test") {
        toast.success(`测速完成：成功 ${res.ok ?? 0}，失败 ${res.failed ?? 0}`);
      } else {
        toast.success("批量操作完成");
      }
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "批量操作失败");
    } finally {
      setBatchBusy(false);
    }
  }

  async function doImport() {
    try {
      const res = await api.post<Record<string, number>>("/api/admin/proxies/import", {
        text: importText,
      });
      toast.success(`导入完成：成功 ${res.success ?? 0}，重复 ${res.duplicate ?? 0}，无效 ${res.invalid ?? 0}`);
      setImportOpen(false);
      setImportText("");
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导入失败");
    }
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!editItem) return;
    try {
      const body: Record<string, unknown> = {
        name: editItem.name,
        protocol: editItem.protocol,
        host: editItem.host,
        port: editItem.port,
        group: editItem.group ?? null,
      };
      if (editItem.id) await api.patch(`/api/admin/proxies/${editItem.id}`, body);
      else await api.post("/api/admin/proxies", body);
      setEditItem(null);
      load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }

  return (
    <div>
      <PageHeader
        title="代理池"
        subtitle="当前渠道的 SOCKS5 / HTTP / HTTPS 代理线路；启用上限 = 该渠道 Key 数 - 1"
        actions={
          <>
            <Button onClick={() => setImportOpen(true)}>
              <Upload size={14} /> 批量导入
            </Button>
            <Button onClick={testAll} loading={testingAll}>
              <Gauge size={14} /> 全部测速
            </Button>
            <Button
              variant="primary"
              onClick={() => setEditItem({ protocol: "socks5", port: 0 })}
            >
              <Plus size={14} /> 添加代理
            </Button>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} />
            </Button>
          </>
        }
      />

      <div className="glass mb-5 flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-sm [font-variant-numeric:tabular-nums]">
        <span className="text-gray-400">
          渠道 Key：<b className="text-gray-100">{keyCount}</b>
        </span>
        <span className="text-gray-400">
          启用代理：
          <b className="text-accent">{enabledCount}</b>
          <span className="text-gray-600"> / {maxProxies}（最多）</span>
        </span>
        <span className="text-gray-400">直连线路：1</span>
        <span className="text-gray-400">
          当前总线路：<b className="text-gray-100">{Math.min(enabledCount, maxProxies) + 1}</b>
        </span>
        {enabledCount >= maxProxies && maxProxies > 0 && (
          <span className="text-xs text-amber-400">已达启用上限</span>
        )}
      </div>

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      {selected.size > 0 && (
        <div className="glass animate-rise mb-3 flex items-center gap-3 px-4 py-2.5 text-sm">
          <span className="text-gray-400">
            已选 <b className="text-gray-100">{selected.size}</b> 项
          </span>
          <Button disabled={batchBusy} onClick={() => batch("enable")}>批量启用</Button>
          <Button disabled={batchBusy} onClick={() => batch("disable")}>批量禁用</Button>
          <Button disabled={batchBusy} onClick={() => batch("test")}>
            <Gauge size={14} /> 批量测速
          </Button>
          <Button disabled={batchBusy} onClick={() => batch("delete")}>
            <Trash2 size={14} /> 批量删除
          </Button>
          <Button onClick={() => setSelected(new Set())}>取消选择</Button>
        </div>
      )}

      <DataTable
        loading={loading}
        empty="暂无代理"
        head={
          <>
            <Th>
              <Checkbox
                ariaLabel="全选"
                checked={proxies.length > 0 && selected.size === proxies.length}
                indeterminate={selected.size > 0 && selected.size < proxies.length}
                onChange={toggleAll}
              />
            </Th>
            <Th>名称</Th>
            <Th>协议</Th>
            <Th>地址</Th>
            <Th>分组</Th>
            <Th>公网 IP</Th>
            <Th>国家</Th>
            <Th>延迟</Th>
            <Th>状态</Th>
            <Th>启用</Th>
            <Th>最后测速</Th>
            <Th>操作</Th>
          </>
        }
      >
        {proxies.map((p) => (
          <tr key={p.id} className="hover:bg-white/[0.02]">
            <Td>
              <Checkbox
                ariaLabel={`选择 ${p.name}`}
                checked={selected.has(p.id)}
                onChange={() => toggleOne(p.id)}
              />
            </Td>
            <Td className="font-medium text-gray-200">{p.name}</Td>
            <Td>
              <code className="rounded bg-white/5 px-1.5 py-0.5 text-xs uppercase text-blue-300">
                {p.protocol}
              </code>
            </Td>
            <Td className="font-mono text-xs text-gray-400">
              {p.host}:{p.port}
            </Td>
            <Td className="text-gray-400">{p.group_name || "—"}</Td>
            <Td className="font-mono text-xs text-gray-400">{p.public_ip || "—"}</Td>
            <Td className="text-gray-400">{p.country || "—"}</Td>
            <Td>{fmtLatency(p.latency)}</Td>
            <Td>
              <Badge status={p.status} />
            </Td>
            <Td>
              <Toggle
                checked={p.enabled}
                disabled={busyId === p.id}
                onChange={(v) => setEnabled(p, v)}
              />
            </Td>
            <Td className="text-xs text-gray-500">{fmtTime(p.last_check_time)}</Td>
            <Td>
              <div className="flex items-center gap-1">
                <button
                  title="测速"
                  disabled={busyId === p.id}
                  onClick={() => testOne(p)}
                  className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                >
                  <Gauge size={14} />
                </button>
                <button
                  title="获取 IP"
                  disabled={busyId === p.id}
                  onClick={() => fetchIp(p)}
                  className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                >
                  <Globe size={14} />
                </button>
                <button
                  title="编辑"
                  onClick={() => setEditItem(p)}
                  className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                >
                  <Pencil size={14} />
                </button>
                <button
                  title="删除"
                  onClick={() => remove(p)}
                  className="rounded p-1.5 text-gray-500 hover:bg-red-500/15 hover:text-red-400"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Modal open={importOpen} wide title="批量导入代理" onClose={() => setImportOpen(false)}>
        <p className="mb-3 text-xs leading-relaxed text-gray-500">
          每行一条：名称---协议://[user:pass@]host:port，或直接写代理地址
        </p>
        <Textarea
          rows={10}
          placeholder={"美国01---socks5://127.0.0.1:10001\nhttp://127.0.0.1:10003"}
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
        />
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={() => setImportOpen(false)}>取消</Button>
          <Button variant="primary" onClick={doImport} disabled={!importText.trim()}>
            导入
          </Button>
        </div>
      </Modal>

      <Modal
        open={!!editItem}
        title={editItem?.id ? "编辑代理" : "添加代理"}
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
          <div className="grid grid-cols-3 gap-3">
            <Field label="协议">
              <Select
                value={editItem?.protocol ?? "socks5"}
                onChange={(e) => setEditItem((p) => ({ ...p, protocol: e.target.value }))}
              >
                <option value="socks5">socks5</option>
                <option value="socks5h">socks5h</option>
                <option value="http">http</option>
                <option value="https">https</option>
              </Select>
            </Field>
            <Field label="Host">
              <Input
                value={editItem?.host ?? ""}
                onChange={(e) => setEditItem((p) => ({ ...p, host: e.target.value }))}
                required
              />
            </Field>
            <Field label="端口">
              <Input
                type="number"
                min={1}
                max={65535}
                value={editItem?.port ?? ""}
                onChange={(e) => setEditItem((p) => ({ ...p, port: Number(e.target.value) }))}
                required
              />
            </Field>
          </div>
          <Field label="分组">
            <Select
              value={editItem?.group ?? ""}
              onChange={(e) =>
                setEditItem((p) => ({ ...p, group: e.target.value ? Number(e.target.value) : null }))
              }
            >
              <option value="">未分组</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </Select>
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
