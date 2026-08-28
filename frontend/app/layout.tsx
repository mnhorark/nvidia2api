import type { Metadata } from "next";
import { Toaster } from "@/components/toaster";
import "./globals.css";

export const metadata: Metadata = {
  title: "NVIDIA2API · AI API Infrastructure",
  description: "NVIDIA AI API aggregation, proxy acceleration and OpenAI-compatible gateway.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        {children}
        <Toaster />
      </body>
    </html>
  );
}
