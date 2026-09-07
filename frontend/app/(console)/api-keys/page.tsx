"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Copy, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, asList, UserApiKey } from "@/lib/api";
import { useSubmitGuard } from "@/lib/use-submit-guard";
import {
  Badge,
  Button,
  DataTable,
  ErrorBanner,
  Field,
  fmtTime,
  IconButton,
  Input,
  Modal,
  PageHeader,
  Td,
  Th,
  Toggle,
  cx,
  confirmDialog,
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
  // 事后编辑（名称 / 限流 / 额度）。此前只有 quotaEdit，且入口挂在
  // `k.quota > 0` 分支里 —— 结果是"额度设了就再也改不回不限、
  // 创建时留 0 就再也加不上额度"，rate_limit 更是只有创建时能设。
  // 后端 PATCH 早就支持 name/rate_limit/quota/enabled 并返回整行序列化，
  // 缺的只是前端入口。
  const [editKey, setEditKey] = useState<UserApiKey | null>(null);
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [saving, submit] = useSubmitGuard();

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

  /** PATCH 的响应就是权威整行，单行改动不必重拉全表。 */
  function upsertRow(row: UserApiKey) {
    setKeys((prev) => {
      const idx = prev.findIndex((x) => x.id === row.id);
      if (idx < 0) return [...prev, row];
      const next = prev.slice();
      next[idx] = row;
      return next;
    });
  }

  async function doCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!create) return;
    await submit(async () => {
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
    });
  }

  async function setEnabled(k: UserApiKey, enabled: boolean) {
    setBusyId(k.id);
    try {
      const row = await api.patch<UserApiKey>(`/api/admin/api-keys/${k.id}`, { enabled });
      upsertRow(row);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(k: UserApiKey) {
    if (!(await confirmDialog({
      title: "删除 API Key",
      message: (
        <>确认删除 <b className="text-gray-100">{k.name}</b>（{k.key_prefix}…）？
          <span className="text-err">该 Key 将立即失效</span>，用它调用的客户端会全部收到 401。</>
      ),
      confirmText: "删除",
      danger: true,
    }))) return;
    try {
      await api.del(`/api/admin/api-keys/${k.id}`);
      // 204 无响应体，删除结果可本地推导
      setKeys((prev) => prev.filter((x) => x.id !== k.id));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  }

  async function saveEdit() {
    if (!editKey) return;
    await submit(async () => {
      try {
        const row = await api.patch<UserApiKey>(`/api/admin/api-keys/${editKey.id}`, {
          name: editKey.name,
          rate_limit: editKey.rate_limit,
          quota: editKey.quota,
        });
        setEditKey(null);
        upsertRow(row);
        toast.success("已更新");
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "保存失败");
      }
    });
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

      <ErrorBanner message={error} onRetry={load} />

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
              <span
                className="cursor-pointer underline decoration-line decoration-dotted underline-offset-2 hover:text-gray-200"
                title="点击修改名称 / 限流 / 额度"
                onClick={() => setEditKey(k)}
              >
                {k.rate_limit > 0 ? `${k.rate_limit}/分钟` : "不限"}
              </span>
            </Td>
            <Td className="tabular-nums">
              {/* 整格可点，不再挂在 quota>0 分支上：否则"已设额度改回不限"
                  和"创建时留 0 后来想加额度"两条路都不存在。 */}
              <span
                className={cx(
                  "cursor-pointer underline decoration-line decoration-dotted underline-offset-2 hover:text-gray-200",
                  k.quota > 0 && k.used_quota >= k.quota ? "text-err" : "text-mute"
                )}
                title="点击修改名称 / 限流 / 额度"
                onClick={() => setEditKey(k)}
              >
                {k.quota > 0
                  ? `${fmtQuota(k.used_quota)} / ${fmtQuota(k.quota)}`
                  : "不限"}
              </span>
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
              <div className="flex items-center gap-0.5">
                <IconButton
                  title="编辑"
                  aria-label="编辑"
                  onClick={() => setEditKey(k)}
                >
                  <Pencil size={14} />
                </IconButton>
                <IconButton
                  title="删除"
                  aria-label="删除"
                  danger
                  onClick={() => remove(k)}
                >
                  <Trash2 size={14} />
                </IconButton>
              </div>
            </Td>
          </tr>
        ))}
      </DataTable>

      <Modal open={!!create} title="创建 API Key" dismissable={!saving} onClose={() => setCreate(null)}>
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
            <Button variant="primary" type="submit" loading={saving}>
              创建
            </Button>
          </div>
        </form>
      </Modal>

      {/* 一次性密钥：禁止 Esc / 点遮罩 / X 关闭。误按一下就永久丢失这把 Key，
          只能删掉重建；唯一出口是下面的"我已保存"。 */}
      <Modal
        open={!!createdKey}
        title="API Key 创建成功"
        dismissable={false}
        onClose={() => setCreatedKey(null)}
      >
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
        open={!!editKey}
        title={`编辑 API Key · ${editKey?.name ?? ""}`}
        dismissable={!saving}
        onClose={() => setEditKey(null)}
      >
        {editKey && (
          <div className="space-y-3.5">
            <Field label="名称">
              <Input
                value={editKey.name}
                onChange={(e) => setEditKey({ ...editKey, name: e.target.value })}
              />
            </Field>
            <Field label="每分钟限流（0 表示不限）">
              <Input
                type="number"
                min={0}
                value={editKey.rate_limit}
                onChange={(e) =>
                  setEditKey({ ...editKey, rate_limit: Number(e.target.value) })
                }
              />
            </Field>
            <Field label="总 Token 额度（0 表示不限）">
              <Input
                type="number"
                min={0}
                value={editKey.quota}
                onChange={(e) =>
                  setEditKey({ ...editKey, quota: Number(e.target.value) })
                }
              />
            </Field>
            <p className="text-xs text-faint">
              当前已用 {fmtQuota(editKey.used_quota)} tokens；改额度只动上限，已用量保留。
            </p>
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" onClick={() => setEditKey(null)}>
                取消
              </Button>
              <Button variant="primary" onClick={saveEdit} loading={saving}>
                保存
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
