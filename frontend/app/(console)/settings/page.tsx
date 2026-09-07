"use client";

import { useCallback, useEffect, useState } from "react";
import { Eraser, RefreshCw, RotateCcw, Save } from "lucide-react";
import { api, RuntimeParam } from "@/lib/api";
import { Button, Card, ErrorBanner, Input, PageHeader, Select, confirmDialog } from "@/components/ui";
import { toast } from "@/components/toaster";

// 隐藏不需要在控制台调整的参数；后端仍可通过 API / env 修改，
// 功能不受影响（如 max_request_bytes 这类体积极限，默认即可）。
// load() 和 save() 的响应回填都必须过滤——PATCH 返回的是后端全量参数。
const HIDDEN_PARAMS = new Set(["max_request_bytes"]);

function filterVisible(list: RuntimeParam[] | undefined | null): RuntimeParam[] {
  return (Array.isArray(list) ? list : []).filter((p) => !HIDDEN_PARAMS.has(p.key));
}

export default function SettingsPage() {
  const [params, setParams] = useState<RuntimeParam[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [channel, setChannel] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api.get<{ channel: string; settings: RuntimeParam[] }>(
        "/api/admin/settings"
      );
      const list = filterVisible(data?.settings);
      setParams(list);
      setChannel(data?.channel ?? "");
      setDraft(Object.fromEntries(list.map((p) => [p.key, String(p.value)])));
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    setSaving(true);
    try {
      const settings: Record<string, string | number | null> = {};
      for (const p of params) {
        const cur = String(p.value);
        const raw = (draft[p.key] ?? cur).trim();
        // 未修改且从未覆盖过：不提交。避免把"当前默认值"固化成覆盖行，
        // 导致日后调整代码默认值时旧值继续压着（如流式判死参数曾多次改名演进）。
        if (raw === cur && !p.overridden) continue;
        // 改回默认值（或数字类型留空）→ 提交 null，由后端清除覆盖、回落后端默认
        if (raw === String(p.default)) {
          settings[p.key] = null;
          continue;
        }
        if (p.type === "int" || p.type === "float") {
          if (raw === "") {
            settings[p.key] = null;
            continue;
          }
          const n = Number(raw);
          if (Number.isNaN(n)) {
            toast.error(`参数 ${p.key} 需要合法的数字`);
            setSaving(false);
            return;
          }
          settings[p.key] = n;
        } else {
          settings[p.key] = raw;
        }
      }
      const updated = await api.patch<{ channel: string; settings: RuntimeParam[] }>(
        "/api/admin/settings", { settings });
      const list = filterVisible(updated?.settings);
      setParams(list);
      setDraft(Object.fromEntries(list.map((p) => [p.key, String(p.value)])));
      toast.success("设置已保存");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  function reset(key: string) {
    const p = params.find((x) => x.key === key);
    if (p) setDraft((d) => ({ ...d, [key]: String(p.default) }));
  }

  async function cleanLogs() {
    const retention = params.find((p) => p.key === "log_retention_days");
    const days = Number(draft["log_retention_days"] ?? retention?.value ?? 30);
    if (!(days > 0)) {
      toast.info("保留天数为 0 表示永不清理，请先设置一个大于 0 的天数");
      return;
    }
    if (!(await confirmDialog({
      title: "清理请求日志",
      message: (
        <>确认删除当前渠道 <b className="text-gray-100">早于 {days} 天</b> 的请求日志？
          <span className="text-err">该操作不可恢复</span>，历史用量统计会随之减少。</>
      ),
      confirmText: `清理 ${days} 天前`,
      danger: true,
      requireText: String(days),
    }))) return;
    setCleaning(true);
    try {
      const res = await api.post<{ deleted: number; retention_days: number }>(
        "/api/admin/logs/clean", { days }
      );
      if (res.retention_days <= 0) {
        toast.info("保留天数设置为 0，未清理任何日志");
      } else {
        toast.success(`已清理 ${res.deleted} 条日志`);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "清理失败");
    } finally {
      setCleaning(false);
    }
  }

  const dirty = params.some((p) => draft[p.key] !== undefined && draft[p.key] !== String(p.value));

  // 分区展示顺序与标题（与后端 sysconfig 的分组字段对应）
  const GROUPS: { id: string; label: string }[] = [
    { id: "request", label: "并发与请求" },
    { id: "timeout", label: "超时控制" },
    { id: "stream", label: "流式传输" },
    { id: "health", label: "健康检查与冷却" },
    { id: "thinking", label: "思考参数" },
    { id: "logs", label: "日志" },
  ];
  const grouped = GROUPS.map((g) => ({
    ...g,
    items: params.filter((p) => p.group === g.id),
  })).filter((g) => g.items.length > 0);

  return (
    <div>
      <PageHeader
        title="设置"
        subtitle={
          channel
            ? `渠道「${channel}」的运行参数，与其他渠道相互隔离（立即生效）`
            : "系统运行参数（立即生效）"
        }
        actions={
          <>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button onClick={cleanLogs} loading={cleaning}>
              <Eraser size={14} /> 清理日志
            </Button>
            <Button variant="primary" onClick={save} loading={saving} disabled={!params.length}>
              <Save size={14} /> 保存{dirty ? " *" : ""}
            </Button>
          </>
        }
      />

      <ErrorBanner message={error} onRetry={load} />

      <Card className="p-0">
        {params.length === 0 && !loading ? (
          <p className="p-6 text-center text-[13px] text-faint">暂无参数</p>
        ) : (
          <div className="divide-y divide-line">
            {grouped.map((g) => (
              <section key={g.id}>
                <div className="bg-white/[0.02] px-5 py-3">
                  <h3 className="text-[12px] font-semibold uppercase tracking-wider text-faint">
                    {g.label}
                  </h3>
                </div>
                <div className="divide-y divide-line/60">
                  {g.items.map((p) => {
                    const modified =
                      draft[p.key] !== undefined && draft[p.key] !== String(p.value);
                    return (
                      <div
                        key={p.key}
                        className="flex flex-wrap items-center gap-x-6 gap-y-2 bg-transparent px-5 py-4 transition-colors hover:bg-white/[0.015]"
                      >
                        <div className="w-72 min-w-0 shrink-0">
                          <div className="flex items-center gap-1.5">
                            <code className="break-all text-xs text-accent">{p.key}</code>
                            {p.overridden && (
                              <span className="rounded border border-accent/25 bg-accent/10 px-1 py-px text-[9px] font-medium text-accent">
                                已覆盖
                              </span>
                            )}
                            {modified && !p.overridden && (
                              <span className="rounded border border-warn/25 bg-warn/10 px-1 py-px text-[9px] font-medium text-warn">
                                未保存
                              </span>
                            )}
                          </div>
                          <p className="mt-0.5 text-xs leading-relaxed text-faint">{p.description}</p>
                        </div>
                        <div className="flex flex-1 items-center gap-2.5">
                          {p.type === "bool" ? (
                            <Select
                              value={draft[p.key] ?? ""}
                              onChange={(e) =>
                                setDraft((d) => ({ ...d, [p.key]: e.target.value }))
                              }
                              className="w-40"
                            >
                              <option value="true">开启</option>
                              <option value="false">关闭</option>
                            </Select>
                          ) : (
                            <Input
                              type={p.type === "str" ? "text" : "number"}
                              step={p.type === "float" ? "0.1" : "1"}
                              value={draft[p.key] ?? ""}
                              onChange={(e) =>
                                setDraft((d) => ({ ...d, [p.key]: e.target.value }))
                              }
                              className={p.type === "str" ? "w-80" : "w-40"}
                            />
                          )}
                          <span className="text-xs text-faint">默认 {String(p.default)}</span>
                          <button
                            onClick={() => reset(p.key)}
                            title="恢复默认"
                            aria-label="恢复默认"
                            className="rounded-md p-1.5 text-faint transition-colors hover:bg-white/[0.07] hover:text-gray-300"
                          >
                            <RotateCcw size={13} />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
