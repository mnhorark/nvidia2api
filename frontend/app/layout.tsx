import type { Metadata, Viewport } from "next";
import { Toaster } from "@/components/toaster";
import { ConfirmHost } from "@/components/ui";
import "./globals.css";

export const metadata: Metadata = {
  title: "NVIDIA2API · AI API Infrastructure",
  description: "NVIDIA AI API aggregation, proxy acceleration and OpenAI-compatible gateway.",
};

// 深色控制台要配深色浏览器外壳：没有 themeColor 时移动端地址栏是白的，
// 接在 #0c0d0f 的页面外面非常跳。viewport 不导出的话 Next 16 会告警。
export const viewport: Viewport = {
  themeColor: "#0c0d0f",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        {children}
        <Toaster />
        <ConfirmHost />
      </body>
    </html>
  );
}
