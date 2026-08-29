"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";

export type ToastType = "success" | "error" | "info";

interface ToastItem {
  id: number;
  type: ToastType;
  message: string;
}

let idSeq = 0;

function emit(type: ToastType, message: string) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("nvidia2api:toast", { detail: { type, message } })
    );
  }
}

export const toast = {
  success: (m: string) => emit("success", m),
  error: (m: string) => emit("error", m),
  info: (m: string) => emit("info", m),
};

const CONFIG: Record<ToastType, { icon: typeof Info; stripe: string }> = {
  success: { icon: CheckCircle2, stripe: "bg-ok" },
  error: { icon: XCircle, stripe: "bg-err" },
  info: { icon: Info, stripe: "bg-info" },
};

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    function onToast(e: Event) {
      const detail = (e as CustomEvent<{ type: ToastType; message: string }>).detail;
      const id = ++idSeq;
      setItems((prev) => [...prev.slice(-4), { id, ...detail }]);
      window.setTimeout(() => {
        setItems((prev) => prev.filter((t) => t.id !== id));
      }, 4000);
    }
    window.addEventListener("nvidia2api:toast", onToast);
    return () => window.removeEventListener("nvidia2api:toast", onToast);
  }, []);

  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed bottom-5 right-5 z-[100] flex w-80 flex-col gap-2"
    >
      {items.map((t) => {
        const cfg = CONFIG[t.type];
        const Icon = cfg.icon;
        return (
          <div
            key={t.id}
            className="animate-slide-up pointer-events-auto relative flex items-start gap-2.5 overflow-hidden rounded-lg border border-line-strong bg-[#181a1e] py-2.5 pl-4 pr-2 text-[13px] shadow-pop"
          >
            <span className={`absolute inset-y-0 left-0 w-0.5 ${cfg.stripe}`} />
            <Icon
              size={15}
              className={`mt-0.5 shrink-0 ${
                t.type === "success" ? "text-ok" : t.type === "error" ? "text-err" : "text-info"
              }`}
            />
            <span className="flex-1 leading-snug text-gray-200">{t.message}</span>
            <button
              type="button"
              aria-label="关闭"
              onClick={() => setItems((prev) => prev.filter((x) => x.id !== t.id))}
              className="mt-0.5 shrink-0 rounded p-1 text-faint transition-colors hover:bg-white/[0.07] hover:text-gray-200"
            >
              <X size={12} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
