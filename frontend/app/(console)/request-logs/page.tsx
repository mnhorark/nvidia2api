"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Search } from "lucide-react";
import { api, asList, RequestLog } from "@/lib/api";
import {
  Badge,
  Button,
  DataTable,
  fmtLatency,
  fmtTime,
  Input,
  PageHeader,
  Select,
  Td,
  Th,
} from "@/components/ui";

export default function RequestLogsPage() {
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [model, setModel] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (model) params.set("model", model);
      if (status) params.set("status", status);
      const qs = params.toString();
      setLogs(asList<RequestLog>(await api.get(`/api/admin/logs${qs ? `?${qs}` : ""}`)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [model, status]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <PageHeader
        title="请求日志"
        subtitle="用户请求与线路竞速记录"
        actions={
          <Button onClick={load} loading={loading}>
            <RefreshCw size={14} /> 刷新
          </Button>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative max-w-xs flex-1">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
          <Input
            className="pl-9"
            placeholder="按模型筛选…"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
        </div>
        <div className="w-40">
          <Select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">全部状态</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
          </Select>
        </div>
      </div>

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      <DataTable
        loading={loading}
        empty="暂无日志"
        head={
          <>
            <Th>Request ID</Th>
            <Th>时间</Th>
            <Th>模型</Th>
            <Th>耗时</Th>
            <Th>NVIDIA Key</Th>
            <Th>代理</Th>
            <Th>Stream</Th>
            <Th>状态</Th>
            <Th>Tokens</Th>
          </>
        }
      >
        {logs.map((l) => (
          <tr key={l.id ?? l.request_id} className="hover:bg-white/[0.02]">
            <Td className="font-mono text-xs text-gray-400">{l.request_id}</Td>
            <Td className="text-xs text-gray-500">{fmtTime(l.created_at)}</Td>
            <Td className="max-w-[220px] truncate font-mono text-xs text-gray-300" title={l.model}>
              {l.model}
            </Td>
            <Td>{fmtLatency(l.duration_ms)}</Td>
            <Td className="font-mono text-xs text-gray-500">{l.nvidia_key || "—"}</Td>
            <Td className="text-xs text-gray-500">{l.proxy || "直连"}</Td>
            <Td className="text-xs text-gray-500">{l.is_stream ? "是" : "否"}</Td>
            <Td>
              <Badge status={l.status} />
            </Td>
            <Td className="text-xs text-gray-500">
              {l.total_tokens ?? "—"}
            </Td>
          </tr>
        ))}
      </DataTable>
    </div>
  );
}
