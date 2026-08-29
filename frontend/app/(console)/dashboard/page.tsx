"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  Boxes,
  Gauge,
  Globe2,
  KeyRound,
  Minus,
  RefreshCw,
  Timer,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { api, DashboardStats, UsageResponse } from "@/lib/api";
import { Badge, Button, Card, PageHeader } from "@/components/ui";

function StatCard({
  icon: Icon,
  label,
  value,
  sub,
}: {
  icon: React.ComponentType<{ size?: number | string; className?: string }>;
  label: string;
  value: React.ReactNode;
  sub?: string;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-500">{label}</span>
        <Icon size={16} className="text-gray-600" />
      </div>
      <div className="mt-2 text-2xl font-semibold text-gray-100 [font-variant-numeric:tabular-nums]">{value}</div>
      {sub && <div className="mt-1 text-xs text-gray-500">{sub}</div>}
    </Card>
  );
}

function StatusBreakdown({
  title,
  data,
}: {
  title: string;
  data?: Record<string, number>;
}) {
  const entries = Object.entries(data || {});
  return (
    <Card>
      <h3 className="mb-3 text-sm font-medium text-gray-300">{title}</h3>
      {entries.length === 0 ? (
        <p className="text-sm text-gray-600">暂无数据</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {entries.map(([status, count]) => (
            <span key={status} className="flex items-center gap-1.5">
              <Badge status={status} />
              <span className="text-sm text-gray-400">{count}</span>
            </span>
          ))}
        </div>
      )}
    </Card>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-3 mt-8 flex items-center gap-3 text-xs font-medium uppercase tracking-wider text-gray-500">
      <span>{children}</span>
      <span className="h-px flex-1 bg-white/5" />
    </h2>
  );
}

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(d = days) {
    setLoading(true);
    setError("");
    try {
      const [s, u] = await Promise.all([
        api.get<DashboardStats>("/api/admin/dashboard"),
        api.get<UsageResponse>(`/api/admin/dashboard/usage?days=${d}`),
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

  function changeDays(d: number) {
    setDays(d);
    load(d);
  }

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

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      {/* 今日运行状态（当前渠道） */}
      <SectionTitle>今日运行 · {stats?.channel_name ?? "当前渠道"}</SectionTitle>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          icon={TrendingUp}
          label="今日请求"
          value={stats ? stats.requests_today.toLocaleString() : "—"}
        />
        <StatCard
          icon={Gauge}
          label="成功率"
          value={stats ? `${Number(stats.success_rate ?? 0).toFixed(1)}%` : "—"}
        />
        <StatCard
          icon={Timer}
          label="平均延迟"
          value={stats ? `${Number(stats.avg_latency_s ?? stats.avg_latency ?? 0).toFixed(2)}s` : "—"}
        />
        <StatCard
          icon={Activity}
          label="实时并发"
          value={stats?.active_requests ?? "—"}
        />
      </div>

      {/* 全渠道 token 汇总 */}
      <SectionTitle>用量统计</SectionTitle>
      <TokenUsageSection usage={usage} days={days} onChangeDays={changeDays} flush />

      {/* 渠道资源与状态 */}
      <SectionTitle>渠道资源 · {stats?.channel_name ?? "当前渠道"}</SectionTitle>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <StatCard
          icon={KeyRound}
          label="渠道 Keys"
          value={stats ? `${stats.enabled_keys} / ${stats.nvidia_keys}` : "—"}
          sub="启用 / 总数"
        />
        <StatCard
          icon={Globe2}
          label="代理"
          value={stats ? `${stats.enabled_proxies} / ${stats.max_enabled_proxies ?? stats.max_proxies ?? 0}` : "—"}
          sub={`启用 / 上限（共 ${stats?.proxies ?? 0}）`}
        />
        <StatCard
          icon={Boxes}
          label="模型"
          value={stats ? `${stats.enabled_models} / ${stats.models}` : "—"}
          sub="启用 / 总数"
        />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <StatusBreakdown title="Key 状态" data={stats?.key_status} />
        <StatusBreakdown title="代理状态" data={stats?.proxy_status} />
      </div>
    </div>
  );
}

function fmtNum(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function Delta({ cur, prev, invert }: { cur: number; prev: number; invert?: boolean }) {
  if (!prev) return null;
  const pct = ((cur - prev) / prev) * 100;
  if (Math.abs(pct) < 0.05) {
    return (
      <span className="ml-1.5 inline-flex items-center gap-0.5 text-[11px] text-gray-600">
        <Minus size={11} /> 0%
      </span>
    );
  }
  const up = pct > 0;
  const good = invert ? !up : up;
  return (
    <span
      className={`ml-1.5 inline-flex items-center gap-0.5 text-[11px] ${
        good ? "text-emerald-400" : "text-red-400"
      }`}
      title="较上一周期"
    >
      {up ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
      {Math.abs(pct).toFixed(1)}%
    </span>
  );
}

function ShareBar({ value, max, color }: { value: number; max: number; color: string }) {
  return (
    <div className="h-2 flex-1 overflow-hidden rounded-full bg-white/5">
      <div
        className={`h-full rounded-full ${color}`}
        style={{ width: `${max > 0 ? (value / max) * 100 : 0}%` }}
      />
    </div>
  );
}

function TokenUsageSection({
  usage,
  days,
  onChangeDays,
  flush,
}: {
  usage: UsageResponse | null;
  days: number;
  onChangeDays: (d: number) => void;
  flush?: boolean;
}) {
  const list = usage?.days ?? [];
  const totals = usage?.totals;
  const prev = usage?.prev_totals;
  const models = usage?.models ?? [];
  const channels = usage?.channels ?? [];
  const apiKeys = usage?.keys ?? [];
  const max = Math.max(1, ...list.map((d) => d.total_tokens || 0));
  const maxReq = Math.max(1, ...list.map((d) => d.requests || 0));
  const maxModelTokens = Math.max(1, ...models.map((m) => m.total_tokens || 0));
  const maxChannelTokens = Math.max(1, ...channels.map((c) => c.total_tokens || 0));
  const maxKeyTokens = Math.max(1, ...apiKeys.map((k) => k.total_tokens || 0));
  const totalTokens = totals?.total_tokens || 1;

  const metric = (label: string, value: React.ReactNode, delta?: React.ReactNode) => (
    <div>
      <div className="text-xs text-gray-500">{label}</div>
      <div className="mt-0.5 text-gray-100">
        {value}
        {delta}
      </div>
    </div>
  );

  return (
    <Card className="mt-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium text-gray-300">Token 使用（全渠道汇总）</h3>
        <div className="flex gap-1 rounded-lg border border-white/10 p-0.5 text-xs">
          {[1, 7, 14, 30].map((d) => (
            <button
              key={d}
              onClick={() => onChangeDays(d)}
              className={`rounded-md px-2.5 py-1 transition-colors ${
                days === d ? "bg-accent text-black" : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {d === 1 ? "今日" : `${d} 天`}
            </button>
          ))}
        </div>
      </div>

      {totals && (
        <div className="mb-5 grid grid-cols-2 gap-3 rounded-lg border border-white/5 bg-white/[0.02] px-4 py-3 text-sm sm:grid-cols-4 xl:grid-cols-7 [font-variant-numeric:tabular-nums]">
          {metric("请求", totals.requests.toLocaleString(),
            prev ? <Delta cur={totals.requests} prev={prev.requests} /> : undefined)}
          {metric("成功率", `${totals.success_rate.toFixed(1)}%`,
            prev ? <Delta cur={totals.success_rate} prev={prev.success_rate} /> : undefined)}
          {metric("总 Tokens", fmtNum(totals.total_tokens),
            prev ? <Delta cur={totals.total_tokens} prev={prev.total_tokens} /> : undefined)}
          {metric("输入 / 输出", `${fmtNum(totals.prompt_tokens)} / ${fmtNum(totals.completion_tokens)}`)}
          {metric("缓存命中率", `${totals.cache_hit_rate.toFixed(1)}%`)}
          {metric("平均延迟",
            totals.avg_latency_s != null ? `${totals.avg_latency_s.toFixed(2)}s` : "—")}
          {metric("平均 TTFT",
            totals.avg_ttft_ms != null ? `${Math.round(totals.avg_ttft_ms)}ms` : "—")}
        </div>
      )}

      {list.length === 0 ? (
        <p className="text-sm text-gray-600">暂无数据</p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          {/* Token 堆叠图 */}
          <div>
            <div className="mb-2 flex items-center gap-4 text-[11px] text-gray-500">
              <span className="font-medium text-gray-400">Token 用量</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-sm bg-accent/80" /> 输入</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-sm bg-sky-400/80" /> 输出</span>
              <span className="ml-auto">峰值 {fmtNum(max)}</span>
            </div>
            <div className="flex items-end gap-1" style={{ height: 132 }}>
              {list.map((d) => {
                const p = Math.min(100, ((d.prompt_tokens || 0) / max) * 100);
                const c = Math.min(100, ((d.completion_tokens || 0) / max) * 100);
                return (
                  <div key={d.date} className="group relative flex-1">
                    <div className="pointer-events-none absolute -top-9 left-1/2 z-10 hidden -translate-x-1/2 whitespace-nowrap rounded-md border border-white/10 bg-[#14141d] px-2 py-1 text-[11px] text-gray-300 shadow-xl group-hover:block">
                      {d.date} · 总计 {d.total_tokens.toLocaleString()}
                      {(d.cached_tokens ?? 0) > 0 && ` · 缓存 ${d.cached_tokens.toLocaleString()}`}
                    </div>
                    <div className="flex h-full flex-col justify-end overflow-hidden rounded-md">
                      <div style={{ height: `${c}%` }} className="w-full bg-sky-400/80" />
                      <div style={{ height: `${p}%` }} className="w-full bg-accent/80" />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* 请求成功/失败图 */}
          <div>
            <div className="mb-2 flex items-center gap-4 text-[11px] text-gray-500">
              <span className="font-medium text-gray-400">请求量</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-sm bg-emerald-400/80" /> 成功</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-sm bg-red-400/80" /> 失败</span>
              <span className="ml-auto">峰值 {maxReq} 次</span>
            </div>
            <div className="flex items-end gap-1" style={{ height: 132 }}>
              {list.map((d) => {
                const ok = Math.min(100, ((d.success || 0) / maxReq) * 100);
                const fail = Math.min(100, ((d.requests - d.success) / maxReq) * 100);
                return (
                  <div key={d.date} className="group relative flex-1">
                    <div className="pointer-events-none absolute -top-9 left-1/2 z-10 hidden -translate-x-1/2 whitespace-nowrap rounded-md border border-white/10 bg-[#14141d] px-2 py-1 text-[11px] text-gray-300 shadow-xl group-hover:block">
                      {d.date} · {d.requests} 次 · 失败 {d.requests - d.success}
                    </div>
                    <div className="flex h-full flex-col justify-end overflow-hidden rounded-md">
                      <div style={{ height: `${fail}%` }} className="w-full bg-red-400/80" />
                      <div style={{ height: `${ok}%` }} className="w-full bg-emerald-400/80" />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="col-span-full flex justify-between text-[11px] text-gray-600">
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

      {(models.length > 0 || channels.length > 0) && (
        <div className="mt-6 grid gap-6 border-t border-white/5 pt-4 lg:grid-cols-2">
          {models.length > 0 && (
            <div>
              <h4 className="mb-3 text-xs font-medium text-gray-400">按模型分布（tokens）</h4>
              <div className="space-y-2">
                {models.slice(0, 8).map((m) => (
                  <div key={m.model} className="flex items-center gap-3 text-xs">
                    <span className="w-48 truncate font-mono text-gray-300" title={m.model}>
                      {m.model}
                    </span>
                    <ShareBar value={m.total_tokens} max={maxModelTokens} color="bg-accent/70" />
                    <span className="w-16 text-right text-gray-400">{fmtNum(m.total_tokens)}</span>
                    <span className="w-12 text-right text-gray-600">
                      {((m.total_tokens / totalTokens) * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {channels.length > 0 && (
            <div>
              <h4 className="mb-3 text-xs font-medium text-gray-400">按渠道分布（tokens）</h4>
              <div className="space-y-2">
                {channels.slice(0, 8).map((c) => (
                  <div key={c.name} className="flex items-center gap-3 text-xs">
                    <span className="w-48 truncate text-gray-300" title={c.name}>
                      {c.name}
                    </span>
                    <ShareBar value={c.total_tokens} max={maxChannelTokens} color="bg-sky-400/70" />
                    <span className="w-16 text-right text-gray-400">{fmtNum(c.total_tokens)}</span>
                    <span className="w-12 text-right text-gray-600">
                      {((c.total_tokens / totalTokens) * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {apiKeys.length > 0 && (
            <div>
              <h4 className="mb-3 text-xs font-medium text-gray-400">按用户 Key 分布（tokens）</h4>
              <div className="space-y-2">
                {apiKeys.slice(0, 8).map((k) => (
                  <div key={k.name} className="flex items-center gap-3 text-xs">
                    <span className="w-48 truncate text-gray-300" title={k.name}>
                      {k.name}
                    </span>
                    <ShareBar value={k.total_tokens} max={maxKeyTokens} color="bg-emerald-400/70" />
                    <span className="w-16 text-right text-gray-400">{fmtNum(k.total_tokens)}</span>
                    <span className="w-12 text-right text-gray-600">
                      {((k.total_tokens / totalTokens) * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
