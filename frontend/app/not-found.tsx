import Link from "next/link";

/**
 * 404 页。此前用的是 `bg-[#0a0a0f]` + `text-zinc-*` 一套硬编码色，
 * 是全仓库唯一游离在设计令牌之外的文件——改主题时它会单独漏掉，
 * 且 #0a0a0f 与全站 #0c0d0f 的底色肉眼可见地不一致。
 */
export default function NotFound() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-bg text-gray-300">
      <div className="text-center">
        <p className="text-5xl font-semibold text-faint">404</p>
        <p className="mt-2 text-sm text-mute">页面不存在</p>
        <Link
          href="/dashboard"
          className="mt-4 inline-block rounded-lg border border-line bg-white/[0.04] px-4 py-2 text-sm text-gray-200 transition-colors hover:border-line-strong hover:bg-white/[0.08]"
        >
          返回控制台
        </Link>
      </div>
    </div>
  );
}
