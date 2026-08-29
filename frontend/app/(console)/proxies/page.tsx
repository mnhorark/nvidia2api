"use client";

import { useCallback, useEffect, useState } from "react";
import { Gauge, Globe, Pencil, Plus, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { api, asList, ChannelKey, Proxy, ProxyGroup } from "@/lib/api";
import {
  Badge,
  BatchBar,
  Button,
  Checkbox,
  DataTable,
  Field,
  fmtLatency,
  fmtTime,
  IconButton,
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
  // 节点关键字
  const [kw, setKw] = useState("");
  // 批量分组
  const [groupTarget, setGroupTarget] = useState("");

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

  function invertSelection() {
    setSelected((prev) => {
      const next = new Set<number>();
      for (const p of proxies) {
        if (!prev.has(p.id)) next.add(p.id);
      }
      return next;
    });
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

  async function batchGroup(gid: string) {
    if (!gid || selected.size === 0) return;
    setBatchBusy(true);
    try {
      await api.post("/api/admin/proxies/batch", {
        ids: [...selected],
        action: "group",
        group_id: gid === "__none__" ? null : Number(gid),
      });
      toast.success(gid === "__none__" ? "已取消分组" : "批量分组完成");
      setGroupTarget("");
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "批量分组失败");
    } finally {
      setBatchBusy(false);
    }
  }

  /** 按关键字匹配 + 指定前后区间，一键加入选中 */
  function selectByRange(rangeKey: string) {
    const needle = kw.trim().toLowerCase();
    const matched = proxies.filter(
      (p) =>
        !needle ||
        (p.name || "").toLowerCase().includes(needle) ||
        (p.host || "").toLowerCase().includes(needle)
    );
    let picked: typeof proxies = [];
    switch (rangeKey) {
      case "all":
        picked = matched;
        break;
      case "front50":
        picked = matched.slice(0, 50);
        break;
      case "front50_100":
        picked = matched.slice(50, 100);
        break;
      case "front100_150":
        picked = matched.slice(100, 150);
        break;
      case "back50":
        picked = matched.slice(-50);
        break;
      case "back50_100":
        picked = matched.slice(-100, -50);
        break;
      case "back100_150":
        picked = matched.slice(-150, -100);
        break;
      default:
        picked = matched;
    }
    setSelected((prev) => {
      const next = new Set(prev);
      for (const p of picked) next.add(p.id);
      return next;
    });
  }

  const RANGE_CHIPS: [string, string][] = [
    ["all", "全部"],
    ["front50", "前1-50"],
    ["front50_100", "前50-100"],
    ["front100_150", "前100-150"],
    ["back50", "后1-50"],
    ["back50_100", "后50-100"],
    ["back100_150", "后100-150"],
  ];

  const kwMatched = kw.trim()
    ? proxies.filter(
        (p) =>
          (p.name || "").toLowerCase().includes(kw.trim().toLowerCase()) ||
          (p.host || "").toLowerCase().includes(kw.trim().toLowerCase())
      ).length
    : proxies.length;

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

  const atCap = enabledCount >= maxProxies && maxProxies > 0;

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
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button
              variant="primary"
              onClick={() => setEditItem({ protocol: "socks5", port: 0 })}
            >
              <Plus size={14} /> 添加代理
            </Button>
          </>
        }
      />

      {/* 线路状态条 */}
      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-lg border border-line bg-panel-strong px-4 py-3">
          <div className="text-[11px] text-faint">渠道 Key</div>
          <div className="mt-0.5 text-lg font-semibold tabular-nums text-gray-100">{keyCount}</div>
        </div>
        <div className={`rounded-lg border px-4 py-3 ${atCap ? "border-warn/30 bg-warn/[0.05]" : "border-line bg-panel-strong"}`}>
          <div className="text-[11px] text-faint">启用代理</div>
          <div className="mt-0.5 text-lg font-semibold tabular-nums text-gray-100">
            <span className={atCap ? "text-warn" : "text-accent"}>{enabledCount}</span>
            <span className="text-sm font-normal text-faint"> / {maxProxies}</span>
          </div>
          {atCap && <div className="text-[10px] text-warn">已达启用上限</div>}
        </div>
        <div className="rounded-lg border border-line bg-panel-strong px-4 py-3">
          <div className="text-[11px] text-faint">直连线路</div>
          <div className="mt-0.5 text-lg font-semibold tabular-nums text-gray-100">1</div>
        </div>
        <div className="rounded-lg border border-line bg-panel-strong px-4 py-3">
          <div className="text-[11px] text-faint">当前总线路</div>
          <div className="mt-0.5 text-lg font-semibold tabular-nums text-gray-100">
            {Math.min(enabledCount, maxProxies) + 1}
          </div>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      {/* 快速选择 / 筛选 */}
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-line bg-panel px-3 py-2.5">
        <div className="relative">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <Input
            className="h-7 w-40 border-white/[0.08] bg-white/[0.03] pl-8 text-xs"
            placeholder="节点关键字…"
            value={kw}
            onChange={(e) => setKw(e.target.value)}
          />
        </div>
        <div className="flex items-center gap-0.5 rounded-md bg-white/[0.04] p-0.5">
          {RANGE_CHIPS.map(([key, label]) => (
            <button
              key={key}
              disabled={batchBusy}
              onClick={() => selectByRange(key)}
              className="rounded px-2 py-1 text-[11px] text-mute transition-colors hover:bg-white/[0.08] hover:text-gray-200 disabled:opacity-50"
            >
              {label}
            </button>
          ))}
        </div>
        <span className="text-[11px] tabular-nums text-faint">
          匹配 {kwMatched} · 已选 {selected.size}
        </span>
      </div>

      <BatchBar count={selected.size}>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("enable")}>启用</Button>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("disable")}>禁用</Button>
        <Button size="sm" disabled={batchBusy} onClick={() => batch("test")}>
          <Gauge size={13} /> 测速
        </Button>
        <Button size="sm" variant="danger" disabled={batchBusy} onClick={() => batch("delete")}>
          <Trash2 size={13} /> 删除
        </Button>
        <div className="flex items-center gap-1.5">
          <Select
            className="h-7 w-32 text-xs"
            value={groupTarget}
            onChange={(e) => setGroupTarget(e.target.value)}
          >
            <option value="">批量分组…</option>
            {groups.map((g) => (
              <option key={g.id} value={g.id}>{g.name}</option>
            ))}
            <option value="__none__">取消分组</option>
          </Select>
          <Button size="sm" disabled={batchBusy || !groupTarget} onClick={() => batchGroup(groupTarget)}>
            应用
          </Button>
        </div>
        <span className="h-4 w-px bg-white/[0.12]" />
        <Button size="sm" disabled={batchBusy} onClick={invertSelection}>反选</Button>
        <Button size="sm" onClick={() => setSelected(new Set())}>取消</Button>
      </BatchBar>

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
          <tr key={p.id} className="transition-colors hover:bg-white/[0.025]">
            <Td>
              <Checkbox
                ariaLabel={`选择 ${p.name}`}
                checked={selected.has(p.id)}
                onChange={() => toggleOne(p.id)}
              />
            </Td>
            <Td className="font-medium text-gray-200">{p.name}</Td>
            <Td>
              <code className="rounded border border-line bg-white/[0.03] px-1.5 py-0.5 text-[10px] font-medium uppercase text-info">
                {p.protocol}
              </code>
            </Td>
            <Td className="font-mono text-xs text-mute">
              {p.host}:{p.port}
            </Td>
            <Td className="text-mute">{p.group_name || "—"}</Td>
            <Td className="font-mono text-xs text-mute">{p.public_ip || "—"}</Td>
            <Td className="text-mute">{p.country || "—"}</Td>
            <Td
              className="tabular-nums"
              title={p.latency != null ? `${p.latency} ms` : undefined}
            >
              <span
                className={
                  p.latency == null ? "text-faint"
                    : p.latency < 300 ? "text-ok"
                    : p.latency < 1000 ? "text-gray-200"
                    : "text-warn"
                }
              >
                {fmtLatency(p.latency)}
              </span>
            </Td>
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
            <Td className="text-xs text-faint">{fmtTime(p.last_check_time)}</Td>
            <Td>
              <div className="flex items-center gap-0.5">
                <IconButton
                  title="测速"
                  aria-label="测速"
                  disabled={busyId === p.id}
                  onClick={() => testOne(p)}
                >
                  <Gauge size={14} />
                </IconButton>
                <IconButton
                  title="获取 IP"
                  aria-label="获取 IP"
                  disabled={busyId === p.id}
                  onClick={() => fetchIp(p)}
                >
                  <Globe size={14} />
                </IconButton>
                <IconButton
                  title="编辑"
                  aria-label="编辑"
                  onClick={() => setEditItem(p)}
                >
                  <Pencil size={14} />
                </IconButton>
                <IconButton
                  title="删除"
                  aria-label="删除"
                  danger
                  onClick={() => remove(p)}
                >
                  <Trash2 size={14} />
                </IconButton>
              </div>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Modal open={importOpen} wide title="批量导入代理" onClose={() => setImportOpen(false)}>
        <p className="mb-3 text-xs leading-relaxed text-mute">
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
        <form onSubmit={save} className="space-y-3.5">
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
