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
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // 键盘高亮项索引（方向键移动、Enter 选中）
  const [active, setActive] = useState(0);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  // 打开时把高亮项定位到当前渠道，并聚焦对应选项
  useEffect(() => {
    if (!open) return;
    const idx = Math.max(0, channels.findIndex((c) => c.slug === current));
    setActive(idx);
    optionRefs.current[idx]?.focus();
  }, [open, channels, current]);

  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  const pickAt = (i: number) => {
    const c = channels[i];
    if (!c) return;
    setOpen(false);
    triggerRef.current?.focus();
    if (c.slug !== current) onPick(c.slug);
  };

  // 列表内键盘导航：此前只有 outside-mousedown 能关，既没有 Esc，
  // 也没有方向键 —— 纯键盘用户打不开也选不了渠道。
  const onListKeyDown = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case "Escape":
        e.preventDefault();
        close();
        break;
      case "ArrowDown":
        e.preventDefault();
        setActive((i) => {
          const n = Math.min(i + 1, channels.length - 1);
          optionRefs.current[n]?.focus();
          return n;
        });
        break;
      case "ArrowUp":
        e.preventDefault();
        setActive((i) => {
          const n = Math.max(i - 1, 0);
          optionRefs.current[n]?.focus();
          return n;
        });
        break;
      case "Home":
        e.preventDefault();
        setActive(0);
        optionRefs.current[0]?.focus();
        break;
      case "End":
        e.preventDefault();
        setActive(Math.max(0, channels.length - 1));
        optionRefs.current[Math.max(0, channels.length - 1)]?.focus();
        break;
      default:
        break;
    }
  };

  const activeChannel = channels.find((c) => c.slug === current);

  return (
    <div ref={boxRef} className="relative border-b border-line px-3 py-3">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
            // Enter/Space 由 click 处理；这里只负责用方向键直接展开
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setOpen(true);
            }
          }
        }}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="切换渠道"
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
            {activeChannel?.name ?? "选择渠道"}
          </span>
          <span className="block truncate text-[10px] text-faint">
            {activeChannel?.base_url ?? "暂无渠道"}
          </span>
        </span>
        <ChevronsUpDown size={13} className="shrink-0 text-faint" />
      </button>

      {open && (
        <div
          onKeyDown={onListKeyDown}
          className="absolute left-2 right-2 z-50 mt-1.5 overflow-hidden rounded-lg border border-line-strong bg-[#181a1e] shadow-pop animate-rise"
        >
          {/* role=listbox 只包选项：底部"管理渠道/新增"是动作按钮，
              放进 listbox 里会让读屏把它们当成第 N+1 个渠道选项。 */}
          <div role="listbox" aria-label="渠道列表" className="max-h-72 overflow-y-auto p-1">
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
            {channels.map((c, i) => (
              <button
                key={c.id}
                ref={(el) => { optionRefs.current[i] = el; }}
                type="button"
                role="option"
                aria-selected={c.slug === current}
                tabIndex={i === active ? 0 : -1}
                onFocus={() => setActive(i)}
                onClick={() => pickAt(i)}
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
