export default function NotFound() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#0a0a0f] text-zinc-300">
      <div className="text-center">
        <p className="text-5xl font-semibold text-zinc-600">404</p>
        <p className="mt-2 text-sm text-zinc-500">页面不存在</p>
        <a
          href="/dashboard"
          className="mt-4 inline-block rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm hover:bg-white/10"
        >
          返回控制台
        </a>
      </div>
    </div>
  );
}
