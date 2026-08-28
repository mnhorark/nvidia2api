import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "nvidia2api · AI API Infrastructure",
  description: "NVIDIA AI API aggregation, proxy acceleration and OpenAI-compatible gateway.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
