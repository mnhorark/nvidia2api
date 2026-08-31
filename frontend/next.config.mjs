/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // 允许通过 127.0.0.1 访问 dev 资源，否则 Next.js 16 会拦截
  // /_next/static/chunks 等跨域请求，导致页面 JS 不加载、白屏。
  allowedDevOrigins: ["127.0.0.1", "localhost"],

  // 静态导出：产出纯静态 out/ 目录，由 Django 统一托管（all-in-one 镜像）。
  // 之前缺这一项，`next build` 永不产出 out/，Dockerfile 的
  // `COPY --from=fe /fe/out` 必然失败，docker compose build 直接断链。
  output: "export",
  // 导出模式下 Next 的图片优化服务不存在，必须关闭优化。
  images: { unoptimized: true },
  // 导出为 <route>/index.html，便于后端 catch-all 按目录回退查找。
  trailingSlash: true,
};

export default nextConfig;
