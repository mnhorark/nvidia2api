"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * 根路径 -> /dashboard。
 *
 * 这里不用 `redirect()`：静态导出（output: 'export'）不支持在预渲染阶段调用
 * redirect，否则 `next build` 会直接失败。客户端跳转在 dev 与导出产物下都成立。
 */
export default function Home() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/dashboard");
  }, [router]);

  return (
    <main className="flex min-h-screen items-center justify-center text-sm text-faint">
      正在跳转到控制台…
    </main>
  );
}
