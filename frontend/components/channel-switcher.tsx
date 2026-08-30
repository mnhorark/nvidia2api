"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronsUpDown, Layers, Plus, Settings2 } from "lucide-react";
import { Channel } from "@/lib/api";
import { Button, cx } from "@/components/ui";

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
    <div ref={boxRef} className="relative px-3 pb-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cx(
          "flex w-full items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2 text-left transition-colors",
          open ? "border-accent/50" : "hover:bg-white/[0.06]"
        )}
      >
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent/15">
          <Layers size={13} className="text-accent" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium text-gray-100">
            {active?.name ?? "选择渠道"}
          </span>
          <span className="block truncate text-[10px] text-gray-500">
            {active?.base_url ?? "暂无渠道"}
          </span>
        </span>
        <ChevronsUpDown size={13} className="shrink-0 text-gray-600" />
      </button>

      {open && (
        <div className="absolute left-3 right-3 z-50 mt-1 overflow-hidden rounded-lg border border-white/10 bg-[#14141d] shadow-2xl">
          <div className="max-h-72 overflow-y-auto py-1">
            {channels.length === 0 && (
              <p className="px-3 py-2 text-xs text-gray-600">暂无渠道</p>
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
                  "flex w-full items-center gap-2 px-2.5 py-2 text-left transition-colors",
                  c.slug === current ? "bg-accent/10" : "hover:bg-white/5"
                )}
              >
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-[13px] text-gray-100">{c.name}</span>
                    {c.is_default && (
                      <span className="shrink-0 rounded bg-accent/15 px-1 py-px text-[9px] text-accent">
                        默认
                      </span>
                    )}
                    {!c.enabled && (
                      <span className="shrink-0 rounded bg-white/5 px-1 py-px text-[9px] text-gray-500">
                        停用
                      </span>
                    )}
                  </span>
                  <span className="block truncate text-[10px] text-gray-500">
                    {c.base_url}
                  </span>
                  <span className="mt-0.5 block text-[10px] text-gray-600">
                    Key {c.enabled_key_count}/{c.key_count} · 代理{" "}
                    {c.enabled_proxy_count}/{c.proxy_count} · 模型{" "}
                    {c.enabled_model_count}/{c.model_count}
                  </span>
                </span>
                {c.slug === current && <Check size={13} className="shrink-0 text-accent" />}
              </button>
            ))}
          </div>
          <div className="flex gap-1 border-t border-white/5 p-1">
            <Button
              variant="ghost"
              className="flex-1 justify-center !px-2 !py-1.5 !text-[11px]"
              onClick={() => {
                setOpen(false);
                window.location.href = "/channels";
              }}
            >
              <Settings2 size={12} /> 管理渠道
            </Button>
            <Button
              variant="ghost"
              className="flex-1 justify-center !px-2 !py-1.5 !text-[11px]"
              onClick={() => {
                setOpen(false);
                window.location.href = "/channels?new=1";
              }}
            >
              <Plus size={12} /> 新增
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
