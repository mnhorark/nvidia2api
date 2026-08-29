"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, KeyRound } from "lucide-react";
import { NvidiaLogo } from "@/components/ui";
import { api, setToken } from "@/lib/api";
import { Button, Input } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await api.post<{ token: string }>("/api/admin/login", {
        username,
        password,
      });
      setToken(res.token);
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      {/* 顶部氛围光 */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-72"
        style={{
          background:
            "radial-gradient(ellipse 50% 60% at 50% 0%, rgba(118,185,0,0.1), transparent)",
        }}
      />
      <div className="relative w-full max-w-[360px]">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-xl border border-accent/30 bg-accent/10 shadow-[0_0_24px_rgba(118,185,0,0.15)]">
            <NvidiaLogo size={26} />
          </div>
          <h1 className="text-lg font-semibold tracking-wide text-gray-100">NVIDIA2API</h1>
          <p className="mt-1 text-[13px] text-faint">AI API Infrastructure Console</p>
        </div>

        <form
          onSubmit={submit}
          className="rounded-xl border border-line bg-panel-strong p-6 shadow-panel space-y-4"
        >
          <Input
            placeholder="用户名"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
          <Input
            type="password"
            placeholder="密码"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {error && (
            <p role="alert" className="rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-xs text-err">
              {error}
            </p>
          )}
          <Button
            variant="primary"
            className="w-full"
            loading={loading}
            type="submit"
          >
            {loading ? "登录中…" : (
              <>
                登录 <ArrowRight size={14} />
              </>
            )}
          </Button>
          <div className="flex items-center justify-center gap-1.5 pt-1 text-[11px] text-faint">
            <KeyRound size={11} />
            管理员凭据由环境变量 ADMIN_USERNAME / ADMIN_PASSWORD 控制
          </div>
        </form>
      </div>
    </div>
  );
}
