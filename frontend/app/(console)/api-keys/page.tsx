"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Copy, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, asList, UserApiKey } from "@/lib/api";
import {
  Badge,
  Button,
  DataTable,
  Field,
  fmtTime,
  IconButton,
  Input,
  Modal,
  PageHeader,
  Td,
  Th,
  Toggle,
} from "@/components/ui";
import { toast } from "@/components/toaster";

function fmtQuota(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${n}`;
}

export default function ApiKeysPage() {
  const [keys, setKeys] = useState<UserApiKey[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [create, setCreate] = useState<{ name: string; rate_limit: number; quota: number } | null>(null);
  const [quotaEdit, setQuotaEdit] = useState<UserApiKey | null>(null);
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setKeys(asList<UserApiKey>(await api.get("/api/admin/api-keys")));
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function doCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!create) return;
    try {
      const res = await api.post<{ key?: string }>("/api/admin/api-keys", create);
      setCreate(null);
      if (res.key) {
        setCreatedKey(res.key);
        setCopied(false);
      }
      load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "创建失败");
    }
  }

  async function setEnabled(k: UserApiKey, enabled: boolean) {
    setBusyId(k.id);
    try {
      await api.patch(`/api/admin/api-keys/${k.id}`, { enabled });
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(k: UserApiKey) {
    if (!confirm(`确认删除 API Key「${k.name}」？该 Key 将立即失效。`)) return;
    try {
      await api.del(`/api/admin/api-keys/${k.id}`);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  }

  async function saveQuota(k: UserApiKey) {
    if (!quotaEdit) return;
    try {
      await api.patch(`/api/admin/api-keys/${k.id}`, { quota: quotaEdit.quota });
      setQuotaEdit(null);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "修改额度失败");
    }
  }

  function copyKey() {
    if (!createdKey) return;
    const done = () => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(createdKey).then(done).catch(() => {
        fallbackCopy(createdKey);
        done();
      });
    } else {
      fallbackCopy(createdKey);
      done();
    }
  }

  /** 非安全上下文（http://局域网 IP 等）剪贴板 API 不可用时降级为 execCommand */
  function fallbackCopy(text: string): boolean {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }

  return (
    <div>
      <PageHeader
        title="用户 API Keys"
        subtitle="通过 Bearer Token 访问 OpenAI 兼容接口"
        actions={
          <>
            <Button onClick={load} loading={loading}>
              <RefreshCw size={14} /> 刷新
            </Button>
            <Button variant="primary" onClick={() => setCreate({ name: "", rate_limit: 0, quota: 0 })}>
              <Plus size={14} /> 创建 API Key
            </Button>
          </>
        }
      />

      {error && (
        <div className="mb-4 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err">
          {error}
        </div>
      )}

      <DataTable
        loading={loading}
        empty="暂无 API Key"
        head={
          <>
            <Th>名称</Th>
            <Th>Key</Th>
            <Th>状态</Th>
            <Th>限流(次/分)</Th>
            <Th>Token 额度</Th>
            <Th>总请求</Th>
            <Th>成功 / 失败</Th>
            <Th>最后使用</Th>
            <Th>启用</Th>
            <Th>操作</Th>
          </>
        }
      >
        {keys.map((k) => (
          <tr key={k.id} className="transition-colors hover:bg-white/[0.025]">
            <Td className="font-medium text-gray-200">{k.name}</Td>
            <Td>
              <code className="font-mono text-xs text-faint">{k.key_prefix}…</code>
            </Td>
            <Td>
              <Badge status={k.enabled ? "enabled" : "disabled"} />
            </Td>
            <Td className="tabular-nums text-mute">
              {k.rate_limit > 0 ? `${k.rate_limit}/分钟` : "不限"}
            </Td>
            <Td className="tabular-nums">
              {k.quota > 0 ? (
                <span
                  className={`cursor-pointer font-mono text-xs ${
                    k.used_quota >= k.quota ? "text-err" : "text-mute"
                  }`}
                  title="点击调整 Token 额度"
                  onClick={() => setQuotaEdit(k)}
                >
                  {fmtQuota(k.used_quota)} / {fmtQuota(k.quota)}
                </span>
              ) : (
                <span className="text-faint">不限</span>
              )}
            </Td>
            <Td className="tabular-nums">{k.total_requests}</Td>
            <Td className="tabular-nums">
              <span className="text-ok">{k.success_requests}</span>
              <span className="text-faint"> / </span>
              <span className="text-err/80">{k.failed_requests}</span>
            </Td>
            <Td className="text-xs text-faint">{fmtTime(k.last_used_at)}</Td>
            <Td>
              <Toggle
                checked={k.enabled}
                disabled={busyId === k.id}
                onChange={(v) => setEnabled(k, v)}
              />
            </Td>
            <Td>
              <IconButton
                aria-label="删除"
                danger
                onClick={() => remove(k)}
              >
                <Trash2 size={14} />
              </IconButton>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Modal open={!!create} title="创建 API Key" onClose={() => setCreate(null)}>
        <form onSubmit={doCreate} className="space-y-3.5">
          <Field label="名称">
            <Input
              value={create?.name ?? ""}
              onChange={(e) => setCreate((p) => p && { ...p, name: e.target.value })}
              required
            />
          </Field>
          <Field label="每分钟限流（0 表示不限）">
            <Input
              type="number"
              min={0}
              value={create?.rate_limit ?? 0}
              onChange={(e) =>
                setCreate((p) => p && { ...p, rate_limit: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Token 额度（0 表示不限）">
            <Input
              type="number"
              min={0}
              value={create?.quota ?? 0}
              onChange={(e) =>
                setCreate((p) => p && { ...p, quota: Number(e.target.value) })
              }
            />
          </Field>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setCreate(null)}>
              取消
            </Button>
            <Button variant="primary" type="submit">
              创建
            </Button>
          </div>
        </form>
      </Modal>

      <Modal open={!!createdKey} title="API Key 创建成功" onClose={() => setCreatedKey(null)}>
        <p className="mb-3 flex items-start gap-2 rounded-lg border border-warn/25 bg-warn/[0.08] px-3 py-2.5 text-xs text-warn">
          完整 Key 只会显示这一次，请立即复制保存。
        </p>
        <div className="flex items-center gap-2 rounded-lg border border-line bg-[#0f1013] p-3">
          <code className="flex-1 break-all font-mono text-[13px] text-accent">{createdKey}</code>
          <IconButton
            title="复制"
            aria-label="复制"
            onClick={copyKey}
          >
            {copied ? <Check size={15} className="text-ok" /> : <Copy size={15} />}
          </IconButton>
        </div>
        <div className="mt-4 flex justify-end">
          <Button variant="primary" onClick={() => setCreatedKey(null)}>
            我已保存
          </Button>
        </div>
      </Modal>

      <Modal
        open={!!quotaEdit}
        title={`调整 Token 额度 · ${quotaEdit?.name ?? ""}`}
        onClose={() => setQuotaEdit(null)}
      >
        {quotaEdit && (
          <div className="space-y-3.5">
            <p className="text-xs text-mute">
              当前已用 {fmtQuota(quotaEdit.used_quota)} / {fmtQuota(quotaEdit.quota)} tokens。
              设置新额度后，已用量会保留；0 表示不限额度。
            </p>
            <Field label="总 Token 额度">
              <Input
                type="number"
                min={0}
                value={quotaEdit.quota}
                onChange={(e) =>
                  setQuotaEdit({ ...quotaEdit, quota: Number(e.target.value) })
                }
              />
            </Field>
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" onClick={() => setQuotaEdit(null)}>
                取消
              </Button>
              <Button variant="primary" onClick={() => saveQuota(quotaEdit)}>
                保存
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
