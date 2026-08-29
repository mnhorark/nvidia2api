"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, RotateCcw, Save } from "lucide-react";
import { api, RuntimeParam } from "@/lib/api";
import { Button, Card, Input, PageHeader, Select } from "@/components/ui";
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

  const dirty = params.some((p) => draft[p.key] !== undefined && draft[p.key] !== String(p.value));

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
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button variant="primary" onClick={save} loading={saving} disabled={!params.length}>
              <Save size={14} /> 保存{dirty ? " *" : ""}
            </Button>
          </>
        }
      />

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      <Card className="p-0">
        {params.length === 0 && !loading ? (
          <p className="p-6 text-center text-[13px] text-faint">暂无参数</p>
        ) : (
          <div className="divide-y divide-line">
            {params.map((p) => {
              const modified = draft[p.key] !== undefined && draft[p.key] !== String(p.value);
              return (
                <div key={p.key} className="flex flex-wrap items-center gap-x-6 gap-y-2 bg-transparent px-5 py-4 transition-colors hover:bg-white/[0.015]">
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
        )}
      </Card>
    </div>
  );
}
