"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Activity,
  Boxes,
  FileClock,
  Globe2,
  KeyRound,
  Layers,
  LayoutDashboard,
  LogOut,
  MessageSquareText,
  Network,
  Settings,
} from "lucide-react";
import { Channel, api, asList, clearToken, getChannel, getToken, setChannel } from "@/lib/api";
import { ChannelSwitcher } from "@/components/channel-switcher";
import { cx, NvidiaLogo } from "@/components/ui";

const NAV = [
  { href: "/dashboard", label: "仪表盘", icon: LayoutDashboard },
  { href: "/channels", label: "渠道", icon: Layers },
  { href: "/chat", label: "对话", icon: MessageSquareText },
  { href: "/keys", label: "渠道 Keys", icon: KeyRound },
  { href: "/proxies", label: "代理池", icon: Globe2 },
  { href: "/proxy-groups", label: "代理分组", icon: Network },
  { href: "/models", label: "模型", icon: Boxes },
  { href: "/api-keys", label: "API Keys", icon: Activity },
  { href: "/request-logs", label: "请求日志", icon: FileClock },
  { href: "/settings", label: "设置", icon: Settings },
];

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [channel, setChannelSlug] = useState("");

  const loadChannels = useCallback(async () => {
    try {
      const data = await api.get<{ results: Channel[]; current: string }>(
        "/api/admin/channels"
      );
      const list = asList<Channel>(data.results);
      setChannels(list);
      const saved = getChannel();
      const known = list.some((c) => c.slug === saved);
      const slug = known ? saved : data.current || list[0]?.slug || "";
      if (slug) setChannel(slug);
      setChannelSlug(slug);
    } catch {
      /* 忽略：登录失效时 request() 会跳转 */
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
    } else {
      setReady(true);
      void loadChannels();
    }
  }, [router, loadChannels]);

  // 监听全局渠道变更事件（渠道页 switchTo / lib setChannel 派发）：
  // 更新本地 state 使 <main key={channel}> 重挂载刷新页面，并重拉渠道列表保持侧边栏名称同步。
  useEffect(() => {
    const onChannelChange = (e: Event) => {
      const slug = (e as CustomEvent<string | undefined>).detail;
      if (typeof slug === "string" && slug) {
        if (slug !== getChannel()) setChannel(slug);
        setChannelSlug(slug);
      }
      void loadChannels();
    };
    window.addEventListener("nvidia2api:channel-change", onChannelChange);
    return () => window.removeEventListener("nvidia2api:channel-change", onChannelChange);
  }, [loadChannels]);

  if (!ready) return null;

  function pick(slug: string) {
    setChannel(slug);
    setChannelSlug(slug);
  }

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-40 flex w-56 flex-col bg-bg-soft border-r border-line">
        {/* 品牌 */}
        <div className="flex h-14 items-center gap-2.5 border-b border-line px-4">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-accent/15">
            <NvidiaLogo size={17} />
          </div>
          <div className="leading-tight">
            <div className="text-[13px] font-semibold tracking-wide text-gray-100">
              NVIDIA2API
            </div>
            <div className="text-[10px] text-faint">AI API Infrastructure</div>
          </div>
        </div>

        <ChannelSwitcher
          channels={channels}
          current={channel}
          onPick={pick}
        />

        {/* 导航 */}
        <nav className="flex-1 overflow-y-auto px-2 py-2 space-y-px">
          {NAV.map((item) => {
            const active = pathname.startsWith(item.href);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cx(
                  "flex items-center gap-2.5 rounded-md px-2.5 py-[7px] text-[13px] transition-colors",
                  active
                    ? "bg-white/[0.07] font-medium text-gray-100"
                    : "text-mute hover:bg-white/[0.04] hover:text-gray-200"
                )}
              >
                <Icon size={15} className={active ? "text-accent" : undefined} />
                {item.label}
              </Link>
            );
          })}
        </nav>

        {/* 底部退出 */}
        <div className="border-t border-line p-2">
          <button
            onClick={() => {
              clearToken();
              router.replace("/login");
            }}
            className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-[7px] text-[13px] text-mute transition-colors hover:bg-err/10 hover:text-err"
          >
            <LogOut size={15} />
            退出登录
          </button>
        </div>
      </aside>

      {/* key 随渠道变化：切换渠道时重挂载页面，各页数据自动按新渠道重新加载 */}
      <main key={channel} className="ml-56 flex-1">
        <div className="mx-auto max-w-6xl px-6 py-6 lg:px-10">
          {children}
        </div>
      </main>
    </div>
  );
}
