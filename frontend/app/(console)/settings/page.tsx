"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Save } from "lucide-react";
import { api, SystemSetting } from "@/lib/api";
import { Button, Card, Input, PageHeader } from "@/components/ui";

export default function SettingsPage() {
  const [settings, setSettings] = useState<SystemSetting | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api.get<SystemSetting>("/api/admin/settings");
      setSettings(data && typeof data === "object" ? data : {});
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function save() {
    if (!settings) return;
    try {
      await api.patch("/api/admin/settings", settings);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      alert(e instanceof Error ? e.message : "保存失败");
    }
  }

  const entries = Object.entries(settings || {});

  return (
    <div>
      <PageHeader
        title="设置"
        subtitle="系统运行参数"
        actions={
          <>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} />
            </Button>
            <Button variant="primary" onClick={save} disabled={!settings || loading}>
              <Save size={14} /> {saved ? "已保存" : "保存"}
            </Button>
          </>
        }
      />

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      {!settings && !loading && !error && (
        <Card>
          <p className="text-sm text-gray-500">暂无设置项</p>
        </Card>
      )}

      {entries.length > 0 && (
        <Card>
          <div className="grid gap-4 md:grid-cols-2">
            {entries.map(([key, value]) => (
              <label key={key} className="block">
                <span className="mb-1 block font-mono text-xs text-gray-500">{key}</span>
                {typeof value === "boolean" ? (
                  <input
                    type="checkbox"
                    checked={value}
                    onChange={(e) =>
                      setSettings((s) => ({ ...s, [key]: e.target.checked }))
                    }
                    className="h-4 w-4 accent-[#76b900]"
                  />
                ) : (
                  <Input
                    value={String(value)}
                    onChange={(e) => {
                      const v =
                        typeof value === "number"
                          ? Number(e.target.value) || 0
                          : e.target.value;
                      setSettings((s) => ({ ...s, [key]: v }));
                    }}
                  />
                )}
              </label>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
