"use client";

import { useEffect } from "react";
import { RotateCcw, TriangleAlert } from "lucide-react";

/**
 * 控制台路由组的错误边界。
 *
 * 没有它时，任意页面渲染期抛错（例如数据字段为 null 却直接调用
 * `x.toLocaleString()`）都会让整棵 React 树卸载 —— 用户看到的是全白页面，
 * 连侧边栏和报错原因都没有。这里把异常圈在页面内部，保留导航与重试入口。
 */
export default function ConsoleError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // 便于排查：真实堆栈只进控制台，不在 UI 上暴露内部细节
    console.error("[console] render error:", error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="w-full max-w-lg rounded-xl border border-err/25 bg-err/[0.06] p-6">
        <div className="flex items-center gap-2.5">
          <TriangleAlert size={18} className="text-err" />
          <h2 className="text-[15px] font-semibold text-gray-100">页面加载出错</h2>
        </div>
        <p className="mt-2 text-[13px] leading-relaxed text-mute">
          该页面在渲染时发生异常，已被错误边界拦截。你可以重试渲染，
          或从左侧导航切换到其它页面。
        </p>
        <p className="mt-3 rounded-md bg-black/25 px-3 py-2 font-mono text-[11px] break-all text-faint">
          {error.message || "未知错误"}
          {error.digest ? ` (${error.digest})` : ""}
        </p>
        <div className="mt-4 flex gap-2">
          <button
            onClick={reset}
            className="inline-flex items-center gap-1.5 rounded-md bg-white/[0.08] px-3 py-1.5 text-[13px] font-medium text-gray-100 transition-colors hover:bg-white/[0.12]"
          >
            <RotateCcw size={13} /> 重试
          </button>
          <a
            href="/dashboard"
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-[13px] text-mute transition-colors hover:text-gray-200"
          >
            返回仪表盘
          </a>
        </div>
      </div>
    </div>
  );
}
