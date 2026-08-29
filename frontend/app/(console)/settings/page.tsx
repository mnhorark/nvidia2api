"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, RotateCcw, Save } from "lucide-react";
import { api, RuntimeParam } from "@/lib/api";
import { Button, Card, Field, Input, PageHeader, Select } from "@/components/ui";
import { toast } from "@/components/toaster";

export default function SettingsPage() {
  const [params, setParams] = useState<RuntimeParam[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [channel, setChannel] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api.get<{ channel: string; settings: RuntimeParam[] }>(
        "/api/admin/settings"
      );
      const list = Array.isArray(data?.settings) ? data.settings : [];
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
      const settings: Record<string, string | number> = {};
      for (const p of params) {
        const raw = (draft[p.key] ?? String(p.value)).trim();
        if (p.type === "int" || p.type === "float") {
          const n = Number(raw);
          if (raw === "" || Number.isNaN(n)) {
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
      const list = Array.isArray(updated?.settings) ? updated.settings : [];
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

  return (
    <div>
      <PageHeader
        title="设置"
        subtitle={
          channel
            ? `渠道「${channel}」的运行参数，与其他渠道相互隔离（立即生效，标注需重启的除外）`
            : "系统运行参数（立即生效，标注需重启的除外）"
        }
        actions={
          <>
            <Button onClick={load} loading={loading} aria-label="刷新">
              <RefreshCw size={14} />
            </Button>
            <Button variant="primary" onClick={save} loading={saving} disabled={!params.length}>
              <Save size={14} /> 保存
            </Button>
          </>
        }
      />

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      <Card>
        {params.length === 0 && !loading ? (
          <p className="text-sm text-gray-500">暂无参数</p>
        ) : (
          <div className="divide-y divide-white/5">
            {params.map((p) => (
              <div key={p.key} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3.5 first:pt-0 last:pb-0">
                <div className="w-72 min-w-0 shrink-0">
                  <code className="break-all text-xs text-accent/90">{p.key}</code>
                  {p.overridden && (
                    <span className="ml-1.5 rounded bg-accent/15 px-1 py-px text-[9px] text-accent">
                      已覆盖
                    </span>
                  )}
                  <p className="mt-0.5 text-xs text-gray-500">{p.description}</p>
                </div>
                <div className="flex flex-1 items-center gap-2">
                  {p.type === "bool" ? (
                    <Select
                      value={draft[p.key] ?? ""}
                      onChange={(e) => setDraft((d) => ({ ...d, [p.key]: e.target.value }))}
                      className="w-40"
                    >
                      <option value="True">开启</option>
                      <option value="False">关闭</option>
                    </Select>
                  ) : (
                    <Input
                      type={p.type === "str" ? "text" : "number"}
                      step={p.type === "float" ? "0.1" : "1"}
                      value={draft[p.key] ?? ""}
                      onChange={(e) => setDraft((d) => ({ ...d, [p.key]: e.target.value }))}
                      className={p.type === "str" ? "w-80" : "w-40"}
                    />
                  )}
                  <span className="text-xs text-gray-600">默认 {String(p.default)}</span>
                  <button
                    onClick={() => reset(p.key)}
                    title="恢复默认"
                    aria-label="恢复默认"
                    className="rounded p-1.5 text-gray-600 hover:bg-white/5 hover:text-gray-300"
                  >
                    <RotateCcw size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
