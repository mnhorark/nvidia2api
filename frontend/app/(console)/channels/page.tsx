"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  Check,
  Download,
  FlaskConical,
  Layers,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  Star,
  Trash2,
} from "lucide-react";
import { Channel, api, asList, setChannel } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Field,
  fmtTime,
  Input,
  Modal,
  PageHeader,
  Select,
  Td,
  Textarea,
  Th,
  Toggle,
} from "@/components/ui";
import { toast } from "@/components/toaster";

const PRESETS = [
  {
    name: "OpenCode Zen",
    slug: "zen",
    base_url: "https://opencode.ai/zen/v1/chat/completions",
  },
  {
    name: "Kilo Gateway",
    slug: "kilo",
    base_url: "https://api.kilo.ai/api/gateway/chat/completions",
  },
  {
    name: "LLM7",
    slug: "llm7",
    base_url: "https://api.llm7.io/v1/chat/completions",
  },
];

const EMPTY: Partial<Channel> = {
  name: "",
  slug: "",
  base_url: "",
  chat_path: "/chat/completions",
  models_path: "/models",
  key_prefix: "",
  auth_scheme: "bearer",
  default_rpm: 40,
  enabled: true,
  is_default: false,
  notes: "",
};

export default function ChannelsPage() {
  // useSearchParams 在静态预渲染时需要 Suspense 边界
  return (
    <Suspense fallback={null}>
      <ChannelsInner />
    </Suspense>
  );
}

function ChannelsInner() {
  const params = useSearchParams();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [current, setCurrent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [edit, setEdit] = useState<Partial<Channel> | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api.get<{ results: Channel[]; current: string }>(
        "/api/admin/channels"
      );
      setChannels(asList<Channel>(data.results));
      setCurrent(data.current ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (params.get("new") === "1") setEdit({ ...EMPTY });
  }, [params]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!edit) return;
    const body = {
      name: edit.name,
      slug: edit.slug,
      base_url: edit.base_url,
      chat_path: edit.chat_path,
      models_path: edit.models_path,
      key_prefix: edit.key_prefix,
      auth_scheme: edit.auth_scheme,
      default_rpm: Number(edit.default_rpm ?? 40),
      enabled: edit.enabled ?? true,
      is_default: edit.is_default ?? false,
      notes: edit.notes ?? "",
    };
    try {
      if (edit.id) await api.patch(`/api/admin/channels/${edit.id}`, body);
      else await api.post("/api/admin/channels", body);
      setEdit(null);
      await load();
      toast.success("渠道已保存");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    }
  }

  async function switchTo(c: Channel) {
    if (c.slug === current) return;
    setChannel(c.slug);
    setCurrent(c.slug);
    toast.success(`已切换到渠道「${c.name}」`);
    // 以 key 重挂载，各页面按新渠道重新加载
    window.dispatchEvent(
      new CustomEvent("nvidia2api:channel-change", { detail: c.slug })
    );
  }

  async function makeDefault(c: Channel) {
    try {
      await api.patch(`/api/admin/channels/${c.id}`, { is_default: true });
      await load();
      toast.success(`「${c.name}」已设为默认渠道`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    }
  }

  async function toggleEnabled(c: Channel, enabled: boolean) {
    setBusyId(c.id);
    try {
      await api.patch(`/api/admin/channels/${c.id}`, { enabled });
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  }

  async function test(c: Channel) {
    setBusyId(c.id);
    try {
      const res = await api.post<{ ok: boolean; model_count?: number; error?: string }>(
        `/api/admin/channels/${c.id}/test`, {}
      );
      if (res.ok) toast.success(`连通正常，可拉取 ${res.model_count ?? 0} 个模型`);
      else toast.error(res.error ?? "连通失败");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "测试失败");
    } finally {
      setBusyId(null);
    }
  }

  async function syncModels(c: Channel) {
    setBusyId(c.id);
    try {
      setChannel(c.slug);
      const res = await api.post<{ created?: number; total?: number }>(
        "/api/admin/models/sync", {}
      );
      toast.success(`同步完成：新增 ${res.created ?? 0} 个，共 ${res.total ?? 0} 个`);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "同步失败");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(c: Channel) {
    if (!confirm(`确认删除渠道「${c.name}」？其下的 Key、代理、模型与日志会一并删除。`)) return;
    try {
      await api.del(`/api/admin/channels/${c.id}`);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  }

  return (
    <div>
      <PageHeader
        title="渠道"
        subtitle="每个渠道是一套独立的 OpenAI 兼容上游：Keys、代理、模型、日志、设置互不干扰"
        actions={
          <>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button variant="primary" onClick={() => setEdit({ ...EMPTY })}>
              <Plus size={14} /> 新增渠道
            </Button>
          </>
        }
      />

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      <DataTable
        loading={loading}
        empty="暂无渠道，点击右上角新增"
        head={
          <>
            <Th>渠道</Th>
            <Th>Chat 端点</Th>
            <Th>鉴权</Th>
            <Th>Key</Th>
            <Th>代理</Th>
            <Th>模型</Th>
            <Th>状态</Th>
            <Th>默认</Th>
            <Th>更新时间</Th>
            <Th>操作</Th>
          </>
        }
      >
        {channels.map((c) => (
          <tr key={c.id} className={c.slug === current ? "bg-accent/[0.04]" : "hover:bg-white/[0.02]"}>
            <Td>
              <div className="flex items-center gap-2">
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent/15">
                  <Layers size={12} className="text-accent" />
                </span>
                <div>
                  <div className="font-medium text-gray-200">{c.name}</div>
                  <code className="text-[10px] text-gray-500">{c.slug}</code>
                </div>
              </div>
            </Td>
            <Td>
              <code className="font-mono text-[11px] text-gray-400">{c.chat_url}</code>
            </Td>
            <Td className="text-xs text-gray-400">
              {c.auth_scheme === "bearer"
                ? "Bearer"
                : c.auth_scheme === "x_api_key"
                  ? "X-API-Key"
                  : "无"}
            </Td>
            <Td className="text-xs text-gray-400">
              {c.enabled_key_count}/{c.key_count}
            </Td>
            <Td className="text-xs text-gray-400">
              {c.enabled_proxy_count}/{c.proxy_count}
            </Td>
            <Td className="text-xs text-gray-400">
              {c.enabled_model_count}/{c.model_count}
            </Td>
            <Td>
              <Toggle
                checked={c.enabled}
                disabled={busyId === c.id}
                onChange={(v) => toggleEnabled(c, v)}
              />
            </Td>
            <Td>
              {c.is_default ? (
                <span className="text-accent">★ 默认</span>
              ) : (
                <button
                  title="设为默认"
                  onClick={() => makeDefault(c)}
                  className="rounded p-1 text-gray-600 hover:bg-white/10 hover:text-gray-200"
                >
                  <Star size={13} />
                </button>
              )}
            </Td>
            <Td className="text-xs text-gray-500">{fmtTime(c.updated_at)}</Td>
            <Td>
              <div className="flex items-center gap-1">
                <button
                  title={c.slug === current ? "当前渠道" : "切换到此渠道"}
                  disabled={c.slug === current}
                  onClick={() => switchTo(c)}
                  className={c.slug === current
                    ? "rounded p-1.5 text-accent"
                    : "rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"}
                >
                  {c.slug === current ? <Check size={14} /> : <Plug size={14} />}
                </button>
                <button
                  title="测试连通性"
                  disabled={busyId === c.id}
                  onClick={() => test(c)}
                  className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                >
                  <FlaskConical size={14} />
                </button>
                <button
                  title="同步该渠道模型"
                  disabled={busyId === c.id}
                  onClick={() => syncModels(c)}
                  className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                >
                  <Download size={14} />
                </button>
                <button
                  title="编辑"
                  onClick={() => setEdit(c)}
                  className="rounded p-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
                >
                  <Pencil size={14} />
                </button>
                <button
                  title="删除"
                  onClick={() => remove(c)}
                  className="rounded p-1.5 text-gray-500 hover:bg-red-500/15 hover:text-red-400"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Card className="mt-6">
        <h3 className="mb-3 text-sm font-medium text-gray-300">对外调用地址</h3>
        <p className="mb-3 text-xs leading-relaxed text-gray-500">
          平台对外仍是一套 OpenAI 兼容接口。默认渠道直连 <code>/v1/*</code>；
          指定渠道加 <code>/c/&lt;slug&gt;</code> 前缀，例如：
        </p>
        <div className="space-y-1.5">
          {channels.map((c) => (
            <div key={c.id} className="flex items-center gap-2 text-xs">
              <Badge status={c.slug === current ? "healthy" : "disabled"} />
              <code className="font-mono text-gray-400">
                /c/{c.slug}/v1/chat/completions
              </code>
              <span className="text-gray-600">→ {c.chat_url}</span>
            </div>
          ))}
        </div>
      </Card>

      <Modal
        open={!!edit}
        wide
        title={edit?.id ? `编辑渠道 · ${edit.name}` : "新增渠道"}
        onClose={() => setEdit(null)}
      >
        <form onSubmit={save} className="space-y-3">
          {!edit?.id && (
            <div>
              <p className="mb-2 text-xs text-gray-500">快速填充：</p>
              <div className="flex flex-wrap gap-2">
                {PRESETS.map((p) => (
                  <Button
                    key={p.slug}
                    type="button"
                    onClick={() => setEdit((v) => ({ ...v, ...p }))}
                  >
                    {p.name}
                  </Button>
                ))}
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <Field label="渠道名称">
              <Input
                value={edit?.name ?? ""}
                onChange={(e) => setEdit((v) => ({ ...v, name: e.target.value }))}
                placeholder="OpenCode Zen"
                required
              />
            </Field>
            <Field label="渠道标识（slug）">
              <Input
                value={edit?.slug ?? ""}
                onChange={(e) => setEdit((v) => ({ ...v, slug: e.target.value }))}
                placeholder="留空自动按名称生成"
                disabled={!!edit?.id}
              />
            </Field>
          </div>

          <Field label="Chat 端点地址">
            <Input
              value={edit?.base_url ?? ""}
              onChange={(e) => setEdit((v) => ({ ...v, base_url: e.target.value }))}
              placeholder="https://opencode.ai/zen/v1/chat/completions"
              required
            />
            <p className="mt-1 text-[11px] text-gray-600">
              可直接粘贴完整 chat 地址，系统会自动拆出 base 与 path
            </p>
          </Field>

          <div className="grid grid-cols-3 gap-3">
            <Field label="Chat 路径">
              <Input
                value={edit?.chat_path ?? ""}
                onChange={(e) => setEdit((v) => ({ ...v, chat_path: e.target.value }))}
              />
            </Field>
            <Field label="模型列表路径">
              <Input
                value={edit?.models_path ?? ""}
                onChange={(e) => setEdit((v) => ({ ...v, models_path: e.target.value }))}
              />
            </Field>
            <Field label="鉴权方式">
              <Select
                value={edit?.auth_scheme ?? "bearer"}
                onChange={(e) => setEdit((v) => ({ ...v, auth_scheme: e.target.value }))}
              >
                <option value="bearer">Bearer Token</option>
                <option value="x_api_key">X-API-Key Header</option>
                <option value="none">无鉴权</option>
              </Select>
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="默认 RPM">
              <Input
                type="number"
                min={1}
                value={edit?.default_rpm ?? 40}
                onChange={(e) =>
                  setEdit((v) => ({ ...v, default_rpm: Number(e.target.value) }))
                }
              />
            </Field>
            <Field label="Key 前缀提示">
              <Input
                value={edit?.key_prefix ?? ""}
                onChange={(e) => setEdit((v) => ({ ...v, key_prefix: e.target.value }))}
                placeholder="可选，如 nvapi"
              />
            </Field>
          </div>

          <Field label="备注">
            <Textarea
              rows={2}
              value={edit?.notes ?? ""}
              onChange={(e) => setEdit((v) => ({ ...v, notes: e.target.value }))}
            />
          </Field>

          <div className="flex items-center gap-5 pt-1">
            <label className="flex items-center gap-2 text-sm text-gray-300">
              <input
                type="checkbox"
                checked={edit?.enabled ?? true}
                onChange={(e) => setEdit((v) => ({ ...v, enabled: e.target.checked }))}
              />
              启用
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-300">
              <input
                type="checkbox"
                checked={edit?.is_default ?? false}
                onChange={(e) => setEdit((v) => ({ ...v, is_default: e.target.checked }))}
              />
              设为默认渠道（/v1/* 走它）
            </label>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setEdit(null)}>
              取消
            </Button>
            <Button variant="primary" type="submit">
              保存
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
