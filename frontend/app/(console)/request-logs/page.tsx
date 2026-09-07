"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw, Search } from "lucide-react";
import { api, RequestLog } from "@/lib/api";
import { readStoredValue, useLocalStorage } from "@/lib/use-local-storage";
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
  Toggle,
} from "@/components/ui";
import { toast } from "@/components/toaster";

export default function RequestLogsPage() {
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [modelInput, setModelInput] = useLocalStorage("requestLogsModelFilter", ""); // 输入框即时值
  // 300ms 防抖后真正参与查询的值。
  // 必须用 readStoredValue 解析：直接 getItem 会拿到带引号的 JSON 串，首查带脏值。
  const [model, setModel] = useState(() =>
    readStoredValue<string>("requestLogsModelFilter", "")
  );
  const [status, setStatus] = useLocalStorage("requestLogsStatusFilter", "");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  // 展开行的懒加载明细缓存：列表接口已剔除 routes/thinking 大字段，展开时按 id 取全文
  const [details, setDetails] = useState<Record<number, RequestLog>>({});
  const [detailLoading, setDetailLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useLocalStorage("requestLogsAutoRefresh", false);
  const [refreshSec, setRefreshSec] = useLocalStorage("requestLogsRefreshSec", 10);
  const safeRefreshSec = [5, 10, 30, 60].includes(Number(refreshSec))
    ? Number(refreshSec)
    : 10;

  const PAGE_SIZE = 100;
  // 请求序号：筛选/刷新变更时递增，过期的分页响应直接丢弃，避免跨筛选追加错乱
  const seqRef = useRef(0);
  // 在途保护：慢后端/慢网络时 setInterval 会堆积 multiple 在途 load，
  // 上一轮没结束就跳过本轮（不叠加请求风暴）
  const inflightRef = useRef(false);
  // 已展开窗口大小（加载更多后会 > PAGE_SIZE），供自动刷新保持窗口不塌回首屏
  const logsLenRef = useRef(PAGE_SIZE);

  const load = useCallback(async () => {
    if (inflightRef.current) return;
    inflightRef.current = true;
    setLoading(true);
    setLoadingMore(false);
    setError("");
    const seq = ++seqRef.current;
    try {
      // 自动刷新时保留用户已展开的窗口：若已加载更多（logs.length > PAGE_SIZE），
      // 用同等大小的窗口重拉，否则轮询会把列表拍回首屏、滚动位置丢失。
      const windowSize = Math.max(PAGE_SIZE, logsLenRef.current);
      const params = new URLSearchParams({ limit: String(windowSize) });
      if (model) params.set("model", model);
      if (status) params.set("status", status);
      const qs = params.toString();
      const data = await api.get<{
        results: RequestLog[];
        total: number;
        has_more: boolean;
      }>(`/api/admin/logs${qs ? `?${qs}` : ""}`);
      if (seq !== seqRef.current) return; // 筛选已变，丢弃过期结果
      setLogs(data.results ?? []);
      logsLenRef.current = (data.results ?? []).length;
      setTotal(data.total ?? null);
      setHasMore(Boolean(data.has_more));
    } catch (e) {
      if (seq !== seqRef.current) return;
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      inflightRef.current = false;
      if (seq === seqRef.current) setLoading(false);
    }
  }, [model, status]);

  async function loadMore() {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    const seq = ++seqRef.current;
    // 快照当前筛选与 offset，防止在途请求期间筛选被修改导致错位追加
    const offset = logs.length;
    const m = model;
    const s = status;
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (m) params.set("model", m);
      if (s) params.set("status", s);
      const data = await api.get<{
        results: RequestLog[];
        total: number;
        has_more: boolean;
      }>(`/api/admin/logs?${params.toString()}`);
      if (seq !== seqRef.current) return; // 期间筛选/刷新已重置列表，丢弃过期分页
      setLogs((prev) => {
        const next = [...prev, ...(data.results ?? [])];
        logsLenRef.current = next.length;
        return next;
      });
      setTotal(data.total ?? null);
      setHasMore(Boolean(data.has_more));
    } catch (e) {
      if (seq !== seqRef.current) return;
      toast.error(e instanceof Error ? e.message : "加载更多失败");
    } finally {
      if (seq === seqRef.current) setLoadingMore(false);
    }
  }

  // 模型关键字防抖：停止输入 300ms 后才应用到查询条件
  useEffect(() => {
    const t = window.setTimeout(() => setModel(modelInput), 300);
    return () => window.clearTimeout(t);
  }, [modelInput]);

  // 自动刷新：开启后按间隔重新加载（加载中状态复用，不打断展开明细）。
  // 标签页不可见时暂停轮询：多标签页 / 切后台时不再空耗后端（日志页轮询风暴来源之一）。
  useEffect(() => {
    if (!autoRefresh) return;
    const t = window.setInterval(() => {
      if (!document.hidden) load();
    }, safeRefreshSec * 1000);
    return () => window.clearInterval(t);
  }, [autoRefresh, safeRefreshSec, load]);

  // 展开一行：列表已不含 routes/thinking 明细，展开时按 id 懒加载全文并缓存。
  async function toggleExpand(l: RequestLog) {
    if (expanded === l.request_id) {
      setExpanded(null);
      return;
    }
    setExpanded(l.request_id);
    if (!details[l.id]) {
      setDetailLoading(true);
      try {
        const d = await api.get<RequestLog>(`/api/admin/logs/${l.id}`);
        setDetails((prev) => ({ ...prev, [l.id]: d }));
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "加载明细失败");
      } finally {
        setDetailLoading(false);
      }
    }
  }

  // token 生成速度（tokens/s），口径对齐 new-api / one-api：输出 tokens / 总耗时。
  // 按量级自适应精度，慢模型（<0.1 tok/s）也保留可读精度，不落回 "—"。
  function fmtSpeed(s?: number | null) {
    if (s == null || !isFinite(s) || s <= 0) return "—";
    if (s < 0.1) return `${s.toFixed(3)} tok/s`;
    if (s < 100) return `${s.toFixed(1)} tok/s`;
    if (s < 1000) return `${s.toFixed(0)} tok/s`;
    return `${(s / 1000).toFixed(2)}k tok/s`;
  }

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <PageHeader
        title="请求日志"
        subtitle="用户请求与线路竞速记录"
        actions={
          <>
            <div className="flex items-center gap-2">
              <span className="text-xs text-mute">自动刷新</span>
              <Toggle checked={autoRefresh} onChange={setAutoRefresh} />
              <Select
                value={String(safeRefreshSec)}
                onChange={(e) => setRefreshSec(Number(e.target.value))}
                className="w-[74px]"
                disabled={!autoRefresh}
                aria-label="刷新间隔"
              >
                <option value="5">5s</option>
                <option value="10">10s</option>
                <option value="30">30s</option>
                <option value="60">60s</option>
              </Select>
            </div>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
          </>
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
        fill
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
          // 明细优先用懒加载的全文（含 routes/thinking）；未展开或未加载时回落列表轻量字段
          const detail = details[l.id] ?? l;
          return (
            <Fragment key={l.id ?? l.request_id}>
              <tr
                onClick={() => toggleExpand(l)}
                className="cursor-pointer transition-colors hover:bg-white/[0.025]"
              >
                <Td className="w-6 text-faint">
                  {/* 展开控件必须是真 button：此前整行只有 onClick，
                      没有 role / tabIndex / onKeyDown，键盘用户完全无法查看
                      任何一条日志明细——而这是这一页的主功能。
                      行的 onClick 保留给鼠标用户，这里阻止冒泡避免触发两次。 */}
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleExpand(l);
                    }}
                    aria-expanded={open}
                    aria-label={open ? "收起请求明细" : "展开请求明细"}
                    className="rounded p-0.5 transition-colors hover:bg-white/[0.08] hover:text-gray-200"
                  >
                    {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  </button>
                </Td>
                <Td className="font-mono text-xs text-mute">{l.request_id}</Td>
                <Td className="text-xs text-faint">{fmtTime(l.created_at)}</Td>
                <Td className="max-w-[180px]">
                  <div className="truncate font-mono text-xs text-gray-300" title={l.model}>
                    {l.model}
                  </div>
                </Td>
                <Td className="tabular-nums">{fmtLatency(l.duration_ms)}</Td>
                <Td className="tabular-nums">{l.first_token_ms != null ? fmtLatency(l.first_token_ms) : "—"}</Td>
                <Td className="max-w-[240px] text-xs text-mute">
                  <div
                    className="truncate"
                    title={l.winner_proxy_name || l.winner_key_name ? `${l.winner_proxy_name || "直连"} + ${l.winner_key_name}` : undefined}
                  >
                    <span className="font-mono">{l.winner_proxy_name || l.winner_key_name ? `${l.winner_proxy_name || "直连"} + ${l.winner_key_name}` : "—"}</span>
                  </div>
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
                      {l.status === "success" && (
                        <div
                          className="text-[10px] text-info"
                          title={l.generation_speed != null ? "输出 tokens / 总耗时" : undefined}
                        >
                          {fmtSpeed(l.generation_speed)}
                        </div>
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
                        <span className="tabular-nums text-info">速度 {l.status === "success" ? fmtSpeed(l.generation_speed) : "—"}</span>
                        <span className="tabular-nums">输入 {l.prompt_tokens ?? 0}</span>
                        <span className="tabular-nums">输出 {l.completion_tokens ?? 0}</span>
                        <span className="tabular-nums">缓存 {l.cached_tokens ?? 0}</span>
                        <span className="tabular-nums">合计 {l.total_tokens ?? 0}</span>
                        {l.proxy_public_ip && <span>代理出口 IP {l.proxy_public_ip}</span>}
                        {l.error_type && <span className="text-err">错误 {l.error_type}</span>}
                      </div>
                      {/* 交付量：截断类错误的归因关键——completion_tokens 为 0 时，
                          靠这三个数区分"上游静默被掐"（该换线）与"思考流了很久被掐"
                          （绝不能换线，重跑会把已交付的思考再发一遍）。 */}
                      {detail.stream_chunks != null && (
                        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-mute">
                          <span className="font-medium text-gray-300">已交付</span>
                          <span className="tabular-nums">SSE 帧 {detail.stream_chunks ?? 0}</span>
                          <span className="tabular-nums">正文 {detail.content_chars ?? 0} 字</span>
                          <span className="tabular-nums">思考 {detail.reasoning_chars ?? 0} 字</span>
                        </div>
                      )}
                      <div className="space-y-1.5 rounded-lg border border-line bg-white/[0.015] p-3">
                        <div className="flex flex-wrap items-center gap-x-2 text-xs text-mute">
                          <span className="font-medium text-gray-400">客户端传入</span>
                          <code className="rounded bg-white/[0.05] px-1.5 py-0.5 font-mono text-[11px] text-info">
                            {Object.keys(detail.client_thinking ?? {}).length > 0
                              ? JSON.stringify(detail.client_thinking)
                              : "—（未传入思考参数）"}
                          </code>
                        </div>
                        <div className="flex flex-wrap items-center gap-x-2 text-xs text-mute">
                          <span className="font-medium text-gray-400">实际下发上游</span>
                          <code className="rounded bg-white/[0.05] px-1.5 py-0.5 font-mono text-[11px] text-warn">
                            {Object.keys(detail.upstream_thinking ?? {}).length > 0
                              ? JSON.stringify(detail.upstream_thinking)
                              : "—（未下发思考参数）"}
                          </code>
                        </div>
                      </div>
                      <div className="text-xs font-medium text-mute">线路竞速明细</div>
                      {detailLoading && !detail.routes ? (
                        <p className="text-xs text-faint">加载中…</p>
                      ) : (detail.routes ?? []).length === 0 ? (
                        <p className="text-xs text-faint">该请求未记录线路明细（早期日志）</p>
                      ) : (
                        <div className="space-y-1.5">
                          {detail.routes!.map((r, i) => (
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

      {(hasMore || total != null) && (
        <div className="mt-3 flex items-center justify-center gap-3 text-xs text-faint">
          <span className="tabular-nums">共 {total ?? "—"} 条，已加载 {logs.length}</span>
          {hasMore && (
            <Button onClick={loadMore} loading={loadingMore} size="sm" variant="ghost">
              {loadingMore ? "加载中…" : "加载更多"}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
