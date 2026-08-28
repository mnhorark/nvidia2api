"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { KeyRound } from "lucide-react";
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
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-accent/15 text-accent">
            <NvidiaLogo size={30} />
          </div>
          <h1 className="text-2xl font-semibold tracking-wide text-gray-100">NVIDIA2API</h1>
          <p className="mt-1 text-sm text-gray-500">AI API Infrastructure Console</p>
        </div>

        <form onSubmit={submit} className="glass space-y-4 p-6">
          <Input
            placeholder="用户名"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
          <Input
            type="password"
            placeholder="密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {error && <p className="text-sm text-red-400">{error}</p>}
          <Button
            variant="primary"
            className="w-full justify-center py-2.5"
            loading={loading}
            type="submit"
          >
            <KeyRound size={15} />
            登录
          </Button>
        </form>
      </div>
    </div>
  );
}
