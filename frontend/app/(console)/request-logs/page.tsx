"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw, Search } from "lucide-react";
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
  const [modelInput, setModelInput] = useState(""); // 输入框即时值
  const [model, setModel] = useState(""); // 300ms 防抖后真正参与查询的值
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

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

  // 模型关键字防抖：停止输入 300ms 后才应用到查询条件
  useEffect(() => {
    const t = window.setTimeout(() => setModel(modelInput), 300);
    return () => window.clearTimeout(t);
  }, [modelInput]);

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

      <div className="mb-4 flex flex-wrap items-center gap-2.5 rounded-lg border border-line bg-panel px-3 py-2.5">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
          <Input
            className="border-white/[0.08] bg-white/[0.03] pl-9"
            placeholder="按模型筛选…"
            value={modelInput}
            onChange={(e) => setModelInput(e.target.value)}
          />
        </div>
        <div className="w-40">
          <Select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="border-white/[0.08] bg-white/[0.03]"
          >
            <option value="">全部状态</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
          </Select>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      <DataTable
        loading={loading}
        empty="暂无日志"
        head={
          <>
            <Th className="w-6"></Th>
            <Th>Request ID</Th>
            <Th>时间</Th>
            <Th>模型</Th>
            <Th>总耗时</Th>
            <Th>首字</Th>
            <Th>胜出线路</Th>
            <Th>流式</Th>
            <Th>状态</Th>
            <Th>Tokens</Th>
          </>
        }
      >
        {logs.map((l) => {
          const open = expanded === l.request_id;
          return (
            <Fragment key={l.id ?? l.request_id}>
              <tr
                onClick={() => setExpanded(open ? null : l.request_id)}
                className="cursor-pointer transition-colors hover:bg-white/[0.025]"
              >
                <Td className="w-6 text-faint">
                  {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                </Td>
                <Td className="font-mono text-xs text-mute">{l.request_id}</Td>
                <Td className="text-xs text-faint">{fmtTime(l.created_at)}</Td>
                <Td className="max-w-[220px] truncate font-mono text-xs text-gray-300" title={l.model}>
                  {l.model}
                </Td>
                <Td className="tabular-nums">{fmtLatency(l.duration_ms)}</Td>
                <Td className="tabular-nums">{l.first_token_ms != null ? fmtLatency(l.first_token_ms) : "—"}</Td>
                <Td className="text-xs text-mute">
                  <span className="font-mono">{l.winner_proxy_name || l.winner_key_name ? `${l.winner_proxy_name || "直连"} + ${l.winner_key_name}` : "—"}</span>
                </Td>
                <Td className="text-xs text-faint">{l.is_stream ? "是" : "否"}</Td>
                <Td>
                  <Badge status={l.status} />
                </Td>
                <Td className="text-xs tabular-nums leading-snug">
                  {l.total_tokens != null && l.total_tokens > 0 ? (
                    <>
                      <span className="text-gray-300">{l.prompt_tokens ?? 0}</span>
                      <span className="text-faint">/</span>
                      <span className="text-gray-300">{l.completion_tokens ?? 0}</span>
                      {(l.cached_tokens ?? 0) > 0 && (
                        <div className="text-[10px] text-faint">缓存↓{l.cached_tokens}</div>
                      )}
                    </>
                  ) : (
                    <span className="text-faint">—</span>
                  )}
                </Td>
              </tr>
              {open && (
                <tr className="bg-black/20">
                  <Td colSpan={10} className="!py-4">
                    <div className="space-y-3">
                      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-mute">
                        <span className="font-medium text-gray-300">请求明细</span>
                        <span className="tabular-nums">耗时 {l.duration_ms ?? 0}ms</span>
                        <span className="tabular-nums">首字 {l.first_token_ms ?? "—"}ms</span>
                        <span className="tabular-nums">输入 {l.prompt_tokens ?? 0}</span>
                        <span className="tabular-nums">输出 {l.completion_tokens ?? 0}</span>
                        <span className="tabular-nums">缓存 {l.cached_tokens ?? 0}</span>
                        <span className="tabular-nums">合计 {l.total_tokens ?? 0}</span>
                        {l.proxy_public_ip && <span>代理出口 IP {l.proxy_public_ip}</span>}
                        {l.error_type && <span className="text-err">错误 {l.error_type}</span>}
                      </div>
                      <div className="space-y-1.5 rounded-lg border border-line bg-white/[0.015] p-3">
                        <div className="flex flex-wrap items-center gap-x-2 text-xs text-mute">
                          <span className="font-medium text-gray-400">客户端传入</span>
                          <code className="rounded bg-white/[0.05] px-1.5 py-0.5 font-mono text-[11px] text-info">
                            {Object.keys(l.client_thinking ?? {}).length > 0
                              ? JSON.stringify(l.client_thinking)
                              : "—（未传入思考参数）"}
                          </code>
                        </div>
                        <div className="flex flex-wrap items-center gap-x-2 text-xs text-mute">
                          <span className="font-medium text-gray-400">实际下发上游</span>
                          <code className="rounded bg-white/[0.05] px-1.5 py-0.5 font-mono text-[11px] text-warn">
                            {Object.keys(l.upstream_thinking ?? {}).length > 0
                              ? JSON.stringify(l.upstream_thinking)
                              : "—（未下发思考参数）"}
                          </code>
                        </div>
                      </div>
                      <div className="text-xs font-medium text-mute">线路竞速明细</div>
                      {(l.routes ?? []).length === 0 ? (
                        <p className="text-xs text-faint">该请求未记录线路明细（早期日志）</p>
                      ) : (
                        <div className="space-y-1.5">
                          {l.routes!.map((r, i) => (
                            <div key={i} className="flex items-center gap-2.5 text-xs">
                              <span
                                className={
                                  r.status === "winner"
                                    ? "text-ok"
                                    : r.status === "failed"
                                      ? "text-err"
                                      : "text-faint"
                                }
                              >
                                {r.status === "winner" ? "●" : r.status === "failed" ? "✕" : "○"}
                              </span>
                              <span className="w-28 text-gray-300">
                                {r.kind === "direct" ? "直连" : r.proxy_name}
                              </span>
                              <span className="w-36 font-mono text-mute">{r.key_name}</span>
                              <span className="tabular-nums text-faint">
                                {r.status === "winner"
                                  ? `${r.latency_ms}ms · 胜出`
                                  : r.status === "cancelled"
                                    ? "已取消"
                                    : `${r.error}${r.http_status ? ` (${r.http_status})` : ""}`}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </Td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </DataTable>
    </div>
  );
}
