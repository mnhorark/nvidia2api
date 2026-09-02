"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Brain, ChevronDown, ChevronRight, CornerDownLeft, Loader2, Trash2 } from "lucide-react";
import { AdminChatResponse, API_BASE_URL, api, asList, clearToken, getChannel, getToken, Model } from "@/lib/api";
import { useLocalStorage } from "@/lib/use-local-storage";
import { Button, PageHeader, Select } from "@/components/ui";
import { toast } from "@/components/toaster";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  meta?: AdminChatResponse["meta"];
}

const THINK_RE = /\s*<thinking>([\s\S]*?)<\/thinking>/i;

function splitReasoning(raw: string, explicitReasoning?: string) {
  if (explicitReasoning && explicitReasoning.trim()) {
    // NVIDIA returns reasoning_content separately; content may still hold <think> tags
    const content = raw.replace(THINK_RE, "").trim();
    return { reasoning: explicitReasoning.trim(), content };
  }
  const m = raw.match(THINK_RE);
  if (m) {
    return { reasoning: m[1].trim(), content: raw.replace(THINK_RE, "").trim() };
  }
  return { reasoning: "", content: raw };
}

export default function ChatPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [model, setModel] = useLocalStorage("chatModel", "");
  const [effort, setEffort] = useLocalStorage<"" | "off" | "low" | "medium" | "high" | "max">(
    "chatEffort",
    ""
  );
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  // 在途流式请求控制器：切页 / 清空时取消，避免对已卸载组件 setState
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    api
      .get("/api/admin/models")
      .then((d) => asList<Model>(d))
      .then((list) => {
        const enabled = list.filter((m) => m.enabled);
        setModels(enabled);
        // 优先恢复上次选中的模型；若已不存在（禁用/删除）则退回第一个可用模型
        if (enabled.length) {
          setModel(
            enabled.some((m) => m.model_name === model) ? model : enabled[0].model_name
          );
        }
      })
      .catch((e) => toast.error(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // smooth 滚动在高频 setState 下会排队大量动画，改即时滚动避免卡顿
    bottomRef.current?.scrollIntoView({ behavior: "auto" });
  }, [messages]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;
    if (!model) {
      toast.error("请先在「模型」页面启用至少一个模型");
      return;
    }
    setInput("");
    const history: ChatMessage[] = [...messages, { role: "user", content: text }];
    const assistant: ChatMessage = { role: "assistant", content: "", reasoning: "" };
    setMessages([...history, assistant]);
    setSending(true);

    let content = "";
    let reasoning = "";
    let meta: ChatMessage["meta"];
    let failed = false;
    let paintTimer: number | null = null;
    let paintPending = false;

    const paintNow = () => {
      const { reasoning: r, content: c } = splitReasoning(content, reasoning || undefined);
      setMessages((prev) => {
        const next = prev.slice();
        next[next.length - 1] = { role: "assistant", content: c, reasoning: r, meta };
        return next;
      });
    };

    // 思考模型每秒可吐几十~上百个 SSE chunk。每个 chunk 都 setState + 全文正则
    // + 整树重渲染会把主线程打满（表现为"经常卡死"）。50ms 节流保留流式观感，
    // 错误/收尾仍立即 paintNow。
    const paint = () => {
      paintPending = true;
      if (paintTimer != null) return;
      paintTimer = window.setTimeout(() => {
        paintTimer = null;
        if (!paintPending) return;
        paintPending = false;
        paintNow();
      }, 50);
    };

    const ac = new AbortController();
    abortRef.current = ac;

    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Token ${getToken() ?? ""}`,
          // 与 lib/api 的 request 一致：带上当前渠道作用域
          ...(getChannel() ? { "X-Channel": getChannel() } : {}),
        },
        signal: ac.signal,
        body: JSON.stringify({
          model,
          stream: true,
          messages: history.map((m) => ({ role: m.role, content: m.content })),
          ...(effort ? { reasoning_effort: effort } : {}),
        }),
      });
      if (!res.ok || !res.body) {
        if (res.status === 401 || res.status === 403) {
          clearToken();
          window.location.href = "/login";
          return;
        }
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.error?.message || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      // 从缓冲区切出完整事件；兼容 \n\n 与 \r\n\r\n 两种分隔，并保留残余部分。
      const drain = (flushTail: boolean) => {
        const events: string[] = [];
        for (;;) {
          const rn = buf.indexOf("\r\n\r\n");
          const nn = buf.indexOf("\n\n");
          let cut = -1;
          let width = 2;
          if (rn >= 0 && (nn < 0 || rn < nn)) {
            cut = rn;
            width = 4;
          } else if (nn >= 0) {
            cut = nn;
          }
          if (cut < 0) break;
          events.push(buf.slice(0, cut));
          buf = buf.slice(cut + width);
        }
        if (flushTail && buf.trim()) {
          events.push(buf);
          buf = "";
        }
        return events;
      };

      // 一个 SSE 事件可含多行 `data:`（续行），需拼接后再解析；
      // 同时要跳过 `event:` / `id:` / `:keep-alive` 等非数据行。
      const dataPayload = (evt: string): string | null => {
        const parts: string[] = [];
        let sawData = false;
        for (const rawLine of evt.split(/\r?\n/)) {
          const line = rawLine.trimEnd();
          if (!line.trim() || line.startsWith(":")) continue; // 注释/心跳
          if (!line.startsWith("data:")) continue;           // event:/id: 等
          sawData = true;
          parts.push(line.slice(5).trim());
        }
        if (!sawData) return null;
        return parts.join("\n");
      };

      // 事件处理逻辑只有一份，主循环与尾部冲刷共用，避免两处实现跑偏。
      const handleEvent = (evt: string) => {
        const raw = dataPayload(evt);
        if (raw === null || raw === "[DONE]") return;
        let data: Record<string, unknown>;
        try {
          data = JSON.parse(raw);
        } catch (parseErr) {
          // 静默吞掉解析失败会让"上游返回了内容但界面空白"无法定位
          console.warn("[chat] SSE 事件解析失败:", raw, parseErr);
          return;
        }
        if (data.error) {
          failed = true;
          const e = data.error as { message?: string };
          content = `⚠ ${e.message || "请求失败"}`;
          paintNow();
          toast.error(e.message || "请求失败");
          return;
        }
        if (data.meta) {
          meta = { ...(meta ?? {}), ...(data.meta as ChatMessage["meta"]) };
          paint();
          return;
        }
        if (data.summary) {
          meta = { ...(meta ?? {}), ...(data.summary as ChatMessage["meta"]) };
          paint();
          return;
        }
        const delta = (data.choices as { delta?: Record<string, string> }[])?.[0]?.delta;
        if (delta?.content) content += delta.content;
        if (delta?.reasoning_content) reasoning += delta.reasoning_content;
        if (delta) paint();
      };

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        for (const evt of drain(false)) handleEvent(evt);
      }
      // 上游可能不以空行结尾：流结束后必须冲刷缓冲区里的最后一个事件，
      // 否则最后一段回答会凭空丢失（表现为"回答总是少一截"）。
      for (const evt of drain(true)) handleEvent(evt);
      paintNow();
      if (!content && !reasoning && !failed) {
        setMessages(history);
        toast.error("未收到有效响应");
      }
    } catch (e) {
      // 主动取消（清空 / 切页）：静默返回，不覆盖已生成的会话
      if ((e as Error)?.name === "AbortError") return;
      setMessages(history);
      toast.error(e instanceof Error ? e.message : "请求失败");
    } finally {
      if (paintTimer != null) {
        window.clearTimeout(paintTimer);
        paintTimer = null;
      }
      if (paintPending) paintNow();
      setSending(false);
      if (abortRef.current === ac) abortRef.current = null;
    }
  }, [input, messages, model, effort, sending]);

  return (
    <div className="flex h-[calc(100vh-3rem)] flex-col">
      <PageHeader title="对话" subtitle="通过竞速引擎直接测试已启用的模型" actions={
        <>
          <Select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="w-52"
          >
            <option value="">选择模型…</option>
            {models.map((m) => (
              <option key={m.id} value={m.model_name}>
                {m.public_name || m.model_name}
              </option>
            ))}
          </Select>
          <Select
            value={effort}
            onChange={(e) => setEffort(e.target.value as typeof effort)}
            className="w-32"
            title="思考强度：仅对支持 reasoning 的模型生效"
          >
            <option value="">思考：默认</option>
            <option value="off">思考：关闭</option>
            <option value="low">思考：低</option>
            <option value="medium">思考：中</option>
            <option value="high">思考：高</option>
            <option value="max">思考：最大</option>
          </Select>
          <Button
            onClick={() => {
              abortRef.current?.abort();
              setMessages([]);
            }}
            disabled={!messages.length}
            variant="ghost"
          >
            <Trash2 size={14} /> 清空
          </Button>
        </>
      } />

      <div className="flex-1 overflow-y-auto rounded-xl border border-line bg-panel-strong p-5">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-faint">
            <Bot size={28} strokeWidth={1.5} />
            <p className="text-[13px]">选择模型并开始对话，请求会经过代理竞速引擎转发到当前渠道</p>
          </div>
        ) : (
          <div className="space-y-5">
            {messages.map((m, i) => (
              <div key={i} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
                <div
                  className={
                    m.role === "user"
                      ? "max-w-[75%] rounded-2xl rounded-br-md border border-accent/25 bg-accent/[0.12] px-4 py-2.5 text-[13px] text-gray-100"
                      : "max-w-[80%] rounded-2xl rounded-bl-md border border-line bg-white/[0.03] px-4 py-2.5 text-[13px] text-gray-200"
                  }
                >
                  {m.reasoning && <ReasoningBlock text={m.reasoning} autoOpen={sending} />}
                  {m.content
                    ? <p className="whitespace-pre-wrap leading-relaxed">{m.content}</p>
                    : <p className="text-faint">&nbsp;</p>}
                  {m.meta && <MetaBlock meta={m.meta} />}
                </div>
              </div>
            ))}
            {sending && (
              <div className="flex items-center gap-2 text-[13px] text-faint">
                <Loader2 size={14} className="animate-spin" /> 竞速请求中…
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <div className="mt-3 flex items-end gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          rows={2}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          className="flex-1 resize-none rounded-xl border border-line bg-[#0f1013] px-4 py-3 text-[13px] text-gray-200 outline-none transition-colors placeholder:text-faint focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
        />
        <Button variant="primary" className="h-10" onClick={() => void send()} disabled={!input.trim() || sending}>
          {sending ? <Loader2 size={15} className="animate-spin" /> : <CornerDownLeft size={15} />}
          发送
        </Button>
      </div>
    </div>
  );
}

function MetaBlock({ meta }: { meta: NonNullable<ChatMessage["meta"]> }) {
  return (
    <div className="mt-2.5 rounded-lg border border-line bg-black/20 px-3 py-2 text-[11px] text-faint">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 tabular-nums">
        <span>首字 {Math.round(meta.first_token_ms ?? meta.first_chunk_ms ?? 0)}ms</span>
        <span>总耗时 {Math.round(meta.duration_ms ?? meta.first_chunk_ms ?? 0)}ms</span>
        <span>线路: {meta.route_type === "direct" ? "直连" : meta.proxy_name}</span>
        <span>Key: {meta.key_name}</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 tabular-nums text-faint/90">
        <span>输入 {meta.prompt_tokens ?? meta.usage?.prompt_tokens ?? 0}</span>
        <span>输出 {meta.completion_tokens ?? meta.usage?.completion_tokens ?? 0}</span>
        <span>缓存 {meta.cached_tokens ?? 0}</span>
        <span>合计 {meta.total_tokens ?? meta.usage?.total_tokens ?? 0}</span>
      </div>
      {(meta.routes?.length ?? 0) > 0 && (
        <div className="mt-2 space-y-1 border-t border-line pt-2">
          {(meta.routes ?? []).map((r, i) => (
            <div key={i} className="flex items-center gap-2">
              <span
                className={
                  r.status === "winner"
                    ? "text-ok"
                    : r.status === "failed"
                      ? "text-err"
                      : "text-faint/60"
                }
              >
                {r.status === "winner" ? "●" : r.status === "failed" ? "✕" : "○"}
              </span>
              <span className="text-gray-400">
                {r.kind === "direct" ? "直连" : r.proxy_name} + {r.key_name}
              </span>
              <span className="text-faint tabular-nums">
                {r.status === "winner"
                  ? `${r.latency_ms}ms · 胜出`
                  : r.status === "cancelled"
                    ? "已取消"
                    : `${r.error}${r.http_status ? ` (${r.http_status})` : ""}`}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ReasoningBlock({ text, autoOpen }: { text: string; autoOpen?: boolean }) {
  const [open, setOpen] = useState(autoOpen ?? false);
  return (
    <div className="mb-2 rounded-lg border border-warn/20 bg-warn/[0.05]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-[11px] font-medium text-warn"
      >
        <Brain size={12} />
        思考过程
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
      </button>
      {open && (
        <div className="max-h-72 overflow-y-auto border-t border-warn/10 px-3 py-2 text-xs italic leading-relaxed text-mute">
          <p className="whitespace-pre-wrap">{text}</p>
        </div>
      )}
    </div>
  );
}
