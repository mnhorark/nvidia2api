"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  Boxes,
  Gauge,
  Globe2,
  KeyRound,
  RefreshCw,
  Timer,
  TrendingUp,
} from "lucide-react";
import { api, DashboardStats } from "@/lib/api";
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
      <div className="mt-2 text-2xl font-semibold text-gray-100">{value}</div>
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

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      setStats(await api.get<DashboardStats>("/api/admin/dashboard"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <div>
      <PageHeader
        title="Dashboard"
        subtitle="平台运行状态总览"
        actions={
          <Button onClick={load} loading={loading}>
            <RefreshCw size={14} /> 刷新
          </Button>
        }
      />

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
        <StatCard
          icon={KeyRound}
          label="NVIDIA Keys"
          value={stats ? `${stats.enabled_keys} / ${stats.nvidia_keys}` : "—"}
          sub="启用 / 总数"
        />
        <StatCard
          icon={Globe2}
          label="Enabled Proxies"
          value={stats ? `${stats.enabled_proxies} / ${stats.max_enabled_proxies ?? stats.max_proxies ?? 0}` : "—"}
          sub={`共 ${stats?.proxies ?? 0} 个代理`}
        />
        <StatCard
          icon={Boxes}
          label="Models"
          value={stats ? `${stats.enabled_models} / ${stats.models}` : "—"}
          sub="启用 / 总数"
        />
        <StatCard
          icon={TrendingUp}
          label="Requests Today"
          value={stats ? stats.requests_today.toLocaleString() : "—"}
        />
        <StatCard
          icon={Activity}
          label="Active Requests"
          value={stats?.active_requests ?? "—"}
          sub="实时并发"
        />
        <StatCard
          icon={Gauge}
          label="Success Rate"
          value={stats ? `${Number(stats.success_rate ?? 0).toFixed(1)}%` : "—"}
        />
        <StatCard
          icon={Timer}
          label="Avg Latency"
          value={stats ? `${Number(stats.avg_latency_s ?? stats.avg_latency ?? 0).toFixed(2)}s` : "—"}
        />
      </div>

      <div className="mt-6 grid gap-4 md:grid-cols-2">
        <StatusBreakdown title="NVIDIA Key 状态" data={stats?.key_status} />
        <StatusBreakdown title="Proxy 状态" data={stats?.proxy_status} />
      </div>
    </div>
  );
}
