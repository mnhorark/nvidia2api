"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronsUpDown, Layers, Plus, Settings2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { Channel } from "@/lib/api";
import { cx } from "@/components/ui";

/** 侧边栏顶部的渠道切换器：一键切换，layout 会以 key 重挂载页面刷新数据。 */
export function ChannelSwitcher({
  channels,
  current,
  onPick,
}: {
  channels: Channel[];
  current: string;
  onPick: (slug: string) => void;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const active = channels.find((c) => c.slug === current);

  return (
    <div ref={boxRef} className="relative border-b border-line px-3 py-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cx(
          "flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
          open
            ? "border-accent/50 bg-accent/[0.06]"
            : "border-line bg-white/[0.02] hover:border-line-strong hover:bg-white/[0.05]"
        )}
      >
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent/12 border border-accent/20">
          <Layers size={12} className="text-accent" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium text-gray-100">
            {active?.name ?? "选择渠道"}
          </span>
          <span className="block truncate text-[10px] text-faint">
            {active?.base_url ?? "暂无渠道"}
          </span>
        </span>
        <ChevronsUpDown size={13} className="shrink-0 text-faint" />
      </button>

      {open && (
        <div className="absolute left-2 right-2 z-50 mt-1.5 overflow-hidden rounded-lg border border-line-strong bg-[#181a1e] shadow-pop animate-rise">
          <div className="max-h-72 overflow-y-auto p-1">
            {channels.length === 0 && (
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  router.push("/channels");
                }}
                className="block w-full rounded-md px-3 py-2 text-left text-xs text-faint hover:bg-white/[0.05] hover:text-gray-300"
              >
                暂无渠道，点击添加
              </button>
            )}
            {channels.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => {
                  setOpen(false);
                  if (c.slug !== current) onPick(c.slug);
                }}
                className={cx(
                  "flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left transition-colors",
                  c.slug === current ? "bg-accent/[0.08]" : "hover:bg-white/[0.05]"
                )}
              >
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-xs font-medium text-gray-100">{c.name}</span>
                    {c.is_default && (
                      <span className="shrink-0 rounded border border-accent/25 bg-accent/10 px-1 py-px text-[9px] font-medium text-accent">
                        默认
                      </span>
                    )}
                    {!c.enabled && (
                      <span className="shrink-0 rounded border border-line bg-white/[0.03] px-1 py-px text-[9px] text-faint">
                        停用
                      </span>
                    )}
                  </span>
                  <span className="mt-0.5 block text-[10px] text-faint">
                    Key {c.enabled_key_count}/{c.key_count}
                    <span className="mx-1 text-white/15">·</span>
                    代理 {c.enabled_proxy_count}/{c.proxy_count}
                    <span className="mx-1 text-white/15">·</span>
                    模型 {c.enabled_model_count}/{c.model_count}
                  </span>
                </span>
                {c.slug === current && <Check size={13} className="shrink-0 text-accent" />}
              </button>
            ))}
          </div>
          <div className="flex gap-1 border-t border-line p-1.5">
            <button
              type="button"
              className="flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] text-mute transition-colors hover:bg-white/[0.06] hover:text-gray-200"
              onClick={() => {
                setOpen(false);
                router.push("/channels");
              }}
            >
              <Settings2 size={12} /> 管理渠道
            </button>
            <button
              type="button"
              className="flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] text-mute transition-colors hover:bg-white/[0.06] hover:text-gray-200"
              onClick={() => {
                setOpen(false);
                router.push("/channels?new=1");
              }}
            >
              <Plus size={12} /> 新增
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
