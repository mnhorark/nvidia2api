"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Boxes,
  Database,
  Gauge,
  Globe2,
  KeyRound,
  Minus,
  RefreshCw,
  Timer,
  TrendingUp,
  Zap,
} from "lucide-react";
import { api, DashboardStats, UsageResponse } from "@/lib/api";
import { useLocalStorage } from "@/lib/use-local-storage";
import { Button, Card, PageHeader, StatusDot, cx } from "@/components/ui";

/* ==================== KPI 大卡 ==================== */
function KpiCard({
  icon: Icon,
  label,
  en,
  value,
  sub,
  delta,
}: {
  icon: React.ComponentType<{ size?: number | string; className?: string }>;
  label: string;
  en: string;
  value: React.ReactNode;
  sub?: string;
  delta?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-line bg-panel-strong p-5 shadow-panel">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-xs font-medium text-faint">{label}</div>
          <div className="mt-0.5 text-[10px] uppercase tracking-widest text-faint/70">{en}</div>
        </div>
        <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-line bg-white/[0.03]">
          <Icon size={14} className="text-mute" />
        </div>
      </div>
      <div className="mt-4 flex items-baseline gap-2">
        <div className="text-3xl font-semibold tracking-tight text-gray-100 [font-variant-numeric:tabular-nums]">
          {value}
        </div>
        {delta}
      </div>
      {sub && <div className="mt-1.5 text-[11px] text-faint">{sub}</div>}
    </div>
  );
}

function Delta({ cur, prev, invert, suffix = "%" }: { cur: number; prev: number; invert?: boolean; suffix?: string }) {
  if (prev === 0 || prev === undefined) return null;
  const pct = ((cur - prev) / prev) * 100;
  if (Math.abs(pct) < 0.05) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md bg-white/[0.04] px-1.5 py-0.5 text-[11px] text-faint">
        <Minus size={10} /> 0{suffix}
      </span>
    );
  }
  const up = pct > 0;
  const good = invert ? !up : up;
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium tabular-nums",
        good ? "bg-ok/10 text-ok" : "bg-err/10 text-err"
      )}
      title="对比上一周期"
    >
      {up ? <ArrowUpRight size={11} /> : <ArrowDownRight size={11} />}
      {Math.abs(pct).toFixed(1)}{suffix}
    </span>
  );
}

/* ==================== 图表 Tooltip 卡片 ==================== */
function ChartTip({ children }: { children: React.ReactNode }) {
  return (
    <div className="pointer-events-none absolute -top-10 left-1/2 z-10 hidden -translate-x-1/2 whitespace-nowrap rounded-md border border-line-strong bg-[#1c1e22] px-2 py-1 text-[11px] text-gray-300 shadow-pop group-hover:block">
      {children}
    </div>
  );
}

/* ==================== 分区标题 ==================== */
function SectionTitle({ title, desc }: { title: React.ReactNode; desc?: string }) {
  return (
    <div className="mb-3 mt-8 flex items-baseline justify-between">
      <div>
        <div className="text-[13px] font-semibold text-gray-200">{title}</div>
        {desc && <div className="mt-0.5 text-xs text-faint">{desc}</div>}
      </div>
    </div>
  );
}

/* ==================== 资源卡片 ==================== */
function ResourceRow({
  icon: Icon,
  label,
  current,
  total,
  hint,
  warn,
}: {
  icon: React.ComponentType<{ size?: number | string; className?: string }>;
  label: string;
  current: number;
  total: number;
  hint?: string;
  warn?: boolean;
}) {
  const pct = total > 0 ? (current / total) * 100 : 0;
  return (
    <div className={cx(
      "rounded-xl border p-4 transition-colors",
      warn ? "border-warn/30 bg-warn/[0.04]" : "border-line bg-panel-strong"
    )}>
      <div className="flex items-center gap-2.5">
        <div className={cx(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border",
          warn ? "border-warn/25 bg-warn/10 text-warn" : "border-line bg-white/[0.03] text-mute"
        )}>
          <Icon size={14} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-xs font-medium text-mute">{label}</div>
          <div className="mt-0.5 flex items-baseline gap-1">
            <span className={cx("text-xl font-semibold tabular-nums leading-none", warn ? "text-warn" : "text-gray-100")}>
              {current}
            </span>
            <span className="text-xs text-faint">/ {total}</span>
          </div>
        </div>
      </div>
      {/* 进度条 */}
      <div className="mt-3 h-1 overflow-hidden rounded-full bg-white/[0.05]">
        <div
          className={cx("h-full rounded-full transition-all", warn ? "bg-warn" : "bg-accent")}
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>
      {hint && <div className="mt-2 text-[10px] text-faint">{hint}</div>}
    </div>
  );
}

/* ==================== Key/Agent 状态面板 ==================== */
function StatusPanel({
  title,
  icon: Icon,
  data,
  meta,
}: {
  title: string;
  icon: React.ComponentType<{ size?: number | string; className?: string }>;
  data?: Record<string, number>;
  meta?: React.ReactNode;
}) {
  const entries = Object.entries(data || {});
  const total = entries.reduce((s, [, n]) => s + n, 0);
  return (
    <Card className="p-0">
      <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <Icon size={15} className="text-mute" />
          <h3 className="text-[13px] font-medium text-gray-200">{title}</h3>
        </div>
        <span className="text-xs tabular-nums text-faint">{total} 个</span>
      </div>
      <div className="px-5 py-4">
        {entries.length === 0 ? (
          <p className="text-xs text-faint">暂无数据</p>
        ) : (
          <div className="grid grid-cols-2 gap-x-6 gap-y-2.5">
            {entries.map(([status, count]) => (
              <div key={status} className="flex items-center gap-2">
                <StatusDot status={status} />
                <span className="flex-1 text-xs text-mute">{statusLabels[status] ?? status}</span>
                <span className="text-xs font-semibold tabular-nums text-gray-200">{count}</span>
              </div>
            ))}
          </div>
        )}
        {meta && <div className="mt-3 border-t border-line pt-2.5 text-[11px] text-faint">{meta}</div>}
      </div>
    </Card>
  );
}

const statusLabels: Record<string, string> = {
  available: "正常",
  healthy: "正常",
  enabled: "启用",
  success: "成功",
  rate_limited: "限流",
  degraded: "降级",
  error: "异常",
  unhealthy: "异常",
  failed: "失败",
  invalid: "无效",
  disabled: "禁用",
  unknown: "未知",
};

function fmtNum(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/* ==================== 分布条形 ==================== */
function ShareBar({ value, max, color }: { value: number; max: number; color: string }) {
  return (
    <div className="h-1 flex-1 overflow-hidden rounded-full bg-white/[0.05]">
      <div
        className={cx("h-full rounded-full transition-all", color)}
        style={{ width: `${max > 0 ? (value / max) * 100 : 0}%` }}
      />
    </div>
  );
}

/* ==================== 主页面 ==================== */
export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [days, setDays] = useLocalStorage("dashboardDays", 1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(d = days) {
    setLoading(true);
    setError("");
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone ?? "";
      const [s, u] = await Promise.all([
        api.get<DashboardStats>("/api/admin/dashboard"),
        api.get<UsageResponse>(
          `/api/admin/dashboard/usage?days=${d}&tz=${encodeURIComponent(tz)}`
        ),
      ]);
      setStats(s);
      setUsage(u);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 轻量轮询：每 10s 刷新运行指标（实时并发等），不重拉用量图
  useEffect(() => {
    const timer = window.setInterval(async () => {
      try {
        const s = await api.get<DashboardStats>("/api/admin/dashboard");
        setStats(s);
      } catch {
        /* 静默，等待下一次轮询 */
      }
    }, 10_000);
    return () => window.clearInterval(timer);
  }, []);

  function changeDays(d: number) {
    setDays(d);
    load(d);
  }

  const maxProxies = Math.max(
    stats?.max_enabled_proxies ?? (stats?.nvidia_keys ?? 0) - 1,
    0
  );
  const keyTotal = stats?.nvidia_keys ?? 0;
  const proxyTotal = stats?.proxies ?? 0;
  const modelTotal = stats?.models ?? 0;

  return (
    <div>
      <PageHeader
        title="仪表盘"
        subtitle={stats?.channel_name ? `当前渠道：${stats.channel_name}` : "平台运行状态总览"}
        actions={
          <Button onClick={() => load()} loading={loading}>
            <RefreshCw size={14} /> 刷新
          </Button>
        }
      />

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      {/* ── KPI 卡片区 ── */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KpiCard
          icon={TrendingUp}
          label="今日请求"
          en="Requests today"
          value={stats ? stats.requests_today.toLocaleString() : "—"}
          delta={usage?.prev_totals && usage?.totals
            ? <Delta cur={usage.totals.requests} prev={usage.prev_totals.requests} />
            : undefined}
          sub="当前渠道过去 24 小时"
        />
        <KpiCard
          icon={Gauge}
          label="成功率"
          en="Success rate"
          value={stats ? `${Number(stats.success_rate ?? 0).toFixed(1)}%` : "—"}
          delta={usage?.prev_totals && usage?.totals
            ? <Delta cur={usage.totals.success_rate} prev={usage.prev_totals.success_rate} />
            : undefined}
          sub="今日成功/总请求"
        />
        <KpiCard
          icon={Timer}
          label="平均延迟"
          en="Avg latency"
          value={stats ? `${Number(stats.avg_latency_s ?? stats.avg_latency ?? 0).toFixed(2)}s` : "—"}
          sub="端到端响应时间"
        />
        <KpiCard
          icon={Zap}
          label="实时并发"
          en="Active now"
          value={stats?.active_requests ?? 0}
          sub="正在处理的请求数"
        />
      </div>

      {/* ── Token 用量图 ── */}
      <SectionTitle title="用量统计" desc="全渠道 Token 与请求量趋势" />
      <TokenUsageSection usage={usage} days={days} onChangeDays={changeDays} />

      {/* ── 渠道资源 ── */}
      <SectionTitle title="渠道资源" desc={`${stats?.channel_name ?? "当前渠道"} · Keys / 代理 / 模型`} />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <ResourceRow
          icon={KeyRound}
          label="渠道 Keys"
          current={stats?.enabled_keys ?? 0}
          total={keyTotal}
          hint={`已启用 ${stats?.enabled_keys ?? 0}，共 ${keyTotal} 个`}
        />
        <ResourceRow
          icon={Globe2}
          label="代理池"
          current={stats?.enabled_proxies ?? 0}
          total={maxProxies}
          warn={(stats?.enabled_proxies ?? 0) >= maxProxies && maxProxies > 0}
          hint={`共 ${proxyTotal} 个代理，启用上限 ${maxProxies}（可用 Key 数 - 1）`}
        />
        <ResourceRow
          icon={Boxes}
          label="模型"
          current={stats?.enabled_models ?? 0}
          total={modelTotal}
          hint={`已启用 ${stats?.enabled_models ?? 0}，共 ${modelTotal} 个`}
        />
      </div>

      {/* ── 状态分布 ── */}
      <SectionTitle title="健康状态" desc="Keys 与代理池运行情况" />
      <div className="grid gap-3 md:grid-cols-2">
        <StatusPanel
          title="Key 状态分布"
          icon={KeyRound}
          data={stats?.key_status}
          meta={<>共 {stats?.nvidia_keys ?? 0} 个 Key，参与竞速调度</>}
        />
        <StatusPanel
          title="代理状态分布"
          icon={Activity}
          data={stats?.proxy_status}
          meta={<>共 {stats?.proxies ?? 0} 个代理，含直连线路</>}
        />
      </div>
    </div>
  );
}

/* ==================== Token 用量大卡 ==================== */
function TokenUsageSection({
  usage,
  days,
  onChangeDays,
}: {
  usage: UsageResponse | null;
  days: number;
  onChangeDays: (d: number) => void;
}) {
  const list = usage?.days ?? [];
  const totals = usage?.totals;
  const prev = usage?.prev_totals;
  const models = usage?.models ?? [];
  const channels = usage?.channels ?? [];
  const apiKeys = usage?.keys ?? [];
  const max = Math.max(1, ...list.map((d) => d.total_tokens || 0));
  const maxReq = Math.max(1, ...list.map((d) => d.requests || 0));

  const metricRow: [string, React.ReactNode][] = [
    ["请求", totals?.requests.toLocaleString() ?? "—"],
    ["成功率", totals ? `${totals.success_rate.toFixed(1)}%` : "—"],
    ["总 Tokens", totals ? fmtNum(totals.total_tokens) : "—"],
    ["输入/输出", totals ? `${fmtNum(totals.prompt_tokens)} / ${fmtNum(totals.completion_tokens)}` : "—"],
    ["缓存命中率", totals ? `${totals.cache_hit_rate.toFixed(1)}%` : "—"],
    ["平均延迟", totals?.avg_latency_s != null ? `${totals.avg_latency_s.toFixed(2)}s` : "—"],
    ["平均 TTFT", totals?.avg_ttft_ms != null ? `${Math.round(totals.avg_ttft_ms)}ms` : "—"],
  ];

  return (
    <Card className="overflow-hidden">
      {/* 头部：标题 + 时间筛选 */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-4">
        <div className="flex items-baseline gap-2">
          <Database size={15} className="translate-y-[-2px] text-mute" />
          <h3 className="text-[13px] font-medium text-gray-200">Token 用量</h3>
          <span className="text-[11px] text-faint">全渠道汇总</span>
        </div>
        <div className="flex gap-0.5 rounded-md border border-line bg-white/[0.02] p-0.5">
          {[1, 7, 14, 30].map((d) => (
            <button
              key={d}
              onClick={() => onChangeDays(d)}
              className={cx(
                "h-6 rounded px-2.5 text-xs font-medium transition-colors",
                days === d ? "bg-white/[0.09] text-gray-100" : "text-faint hover:text-gray-300"
              )}
            >
              {d === 1 ? "今日" : `${d} 天`}
            </button>
          ))}
        </div>
      </div>

      {/* 汇总指标条 */}
      {totals && (
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 border-b border-line px-5 py-4 sm:grid-cols-4 xl:grid-cols-7">
          {metricRow.map(([label, value], i) => {
            const showDelta =
              totals && prev && i < 3;
            return (
              <div key={label}>
                <div className="text-[10px] uppercase tracking-wide text-faint">{label}</div>
                <div className="mt-0.5 flex items-baseline gap-1.5 text-[13px] font-medium tabular-nums text-gray-100">
                  {value}
                  {showDelta && (
                    <>
                      {i === 0 && <Delta cur={totals.requests} prev={prev.requests} />}
                      {i === 1 && <Delta cur={totals.success_rate} prev={prev.success_rate} />}
                      {i === 2 && <Delta cur={totals.total_tokens} prev={prev.total_tokens} />}
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 图表区 */}
      {list.length === 0 ? (
        <div className="py-10 text-center text-[13px] text-faint">暂无数据</div>
      ) : (
        <div className="grid gap-0 lg:grid-cols-2">
          <ChartPanel
            title="Token 用量"
            unit="峰值"
            unitValue={`${fmtNum(max)}`}
            side="left"
            legend={[
              { label: "输入", color: "bg-accent" },
              { label: "输出", color: "bg-info" },
            ]}
          >
            <div className="flex items-end gap-[3px]" style={{ height: 140 }}>
              {list.map((d) => {
                const p = Math.min(100, ((d.prompt_tokens || 0) / max) * 100);
                const c = Math.min(100, ((d.completion_tokens || 0) / max) * 100);
                return (
                  <div key={d.date} className="group relative h-full flex-1">
                    <ChartTip>
                      {d.date} · 总计 <b className="text-gray-100">{d.total_tokens.toLocaleString()}</b>
                      {(d.cached_tokens ?? 0) > 0 && <> · 缓存 {d.cached_tokens.toLocaleString()}</>}
                      {d.prompt_tokens > 0 && <> · 输入 {d.prompt_tokens.toLocaleString()}</>}
                      {d.completion_tokens > 0 && <> · 输出 {d.completion_tokens.toLocaleString()}</>}
                    </ChartTip>
                    <div className="flex h-full flex-col justify-end overflow-hidden rounded-t-[3px]">
                      <div style={{ height: `${c}%`, minHeight: c > 0 ? 3 : 0 }}
                        className="w-full bg-info/70 transition-colors group-hover:bg-info" />
                      <div style={{ height: `${p}%` }}
                        className="w-full bg-accent/70 transition-colors group-hover:bg-accent/90" />
                    </div>
                  </div>
                );
              })}
            </div>
          </ChartPanel>

          <ChartPanel
            title="请求量"
            unit="峰值"
            unitValue={`${maxReq} 次`}
            legend={[
              { label: "成功", color: "bg-ok" },
              { label: "失败", color: "bg-err" },
            ]}
          >
            <div className="flex items-end gap-[3px]" style={{ height: 140 }}>
              {list.map((d) => {
                const ok = Math.min(100, ((d.success || 0) / maxReq) * 100);
                const fail = Math.min(100, ((d.requests - d.success) / maxReq) * 100);
                return (
                  <div key={d.date} className="group relative h-full flex-1">
                    <ChartTip>
                      {d.date} · <b className="text-gray-100">{d.requests}</b> 次 · 失败 {d.requests - d.success}
                    </ChartTip>
                    <div className="flex h-full flex-col justify-end overflow-hidden rounded-t-[3px]">
                      <div style={{ height: `${fail}%` }} className="w-full bg-err/70" />
                      <div style={{ height: `${ok}%` }} className="w-full bg-ok/70" />
                    </div>
                  </div>
                );
              })}
            </div>
          </ChartPanel>

          {/* X 轴标签 */}
          <div className="col-span-full flex justify-between border-t border-line px-5 py-2 text-[10px] tabular-nums text-faint">
            {list.map((d, i) => (
              <span key={d.date} className="flex-1 text-center">
                {days === 1
                  ? parseInt(d.date, 10) % 3 === 0 ? d.date : ""
                  : list.length > 14
                    ? i % 5 === 0 ? d.date.slice(5) : ""
                    : d.date.slice(5)}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 分布明细 */}
      {(models.length > 0 || channels.length > 0 || apiKeys.length > 0) && (
        <div className="grid border-t border-line lg:grid-cols-3">
          {models.length > 0 && (
            <DistributionSection title="按模型" rows={models.slice(0, 6).map((m) => ({ key: m.model, tokens: m.total_tokens, mono: true }))} max={Math.max(...models.map((m) => m.total_tokens), 1)} total={totals?.total_tokens ?? 1} color="bg-accent" mono />
          )}
          {channels.length > 0 && (
            <DistributionSection title="按渠道" rows={channels.slice(0, 6).map((c) => ({ key: c.name, tokens: c.total_tokens }))} max={Math.max(...channels.map((c) => c.total_tokens), 1)} total={totals?.total_tokens ?? 1} color="bg-info" />
          )}
          {apiKeys.length > 0 && (
            <DistributionSection title="按用户 Key" rows={apiKeys.slice(0, 6).map((k) => ({ key: k.name, tokens: k.total_tokens }))} max={Math.max(...apiKeys.map((k) => k.total_tokens), 1)} total={totals?.total_tokens ?? 1} color="bg-ok" />
          )}
        </div>
      )}
    </Card>
  );
}

function ChartPanel({
  title,
  unit,
  unitValue,
  legend,
  side,
  children,
}: {
  title: string;
  unit: string;
  unitValue: string;
  legend: { label: string; color: string }[];
  side?: "left" | "right";
  children: React.ReactNode;
}) {
  return (
    <div className={cx(
      "px-5 pb-4 pt-4",
      side === "left" ? "border-b border-line lg:border-b-0 lg:border-r" : ""
    )}>
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <span className="text-xs font-medium text-mute">{title}</span>
          <div className="flex items-center gap-2.5">
            {legend.map((l) => (
              <span key={l.label} className="flex items-center gap-1 text-[11px] text-faint">
                <span className={cx("h-1.5 w-1.5 rounded-full", l.color)} /> {l.label}
              </span>
            ))}
          </div>
        </div>
        <span className="text-[10px] tabular-nums text-faint">{unit} {unitValue}</span>
      </div>
      {children}
    </div>
  );
}

function DistributionSection({
  title,
  rows,
  max,
  total,
  color,
  mono,
}: {
  title: string;
  rows: { key: string; tokens: number; mono?: boolean }[];
  max: number;
  total: number;
  color: string;
  mono?: boolean;
}) {
  return (
    <div className="px-5 py-4">
      <h4 className="mb-3 text-xs font-medium text-mute">{title}</h4>
      <div className="space-y-2">
        {rows.map((r) => (
          <div key={r.key} className="flex items-center gap-3 text-[11px]">
            <span className={cx("w-36 truncate", mono && "font-mono text-[10px]")} title={r.key}>
              {r.key}
            </span>
            <ShareBar value={r.tokens} max={max} color={color} />
            <span className="w-12 text-right tabular-nums text-mute">{fmtNum(r.tokens)}</span>
            <span className="w-9 text-right tabular-nums text-faint">
              {((r.tokens / total) * 100).toFixed(0)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
