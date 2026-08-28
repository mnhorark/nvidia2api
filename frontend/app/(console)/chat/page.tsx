"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, CornerDownLeft, Loader2, Trash2, Zap } from "lucide-react";
import { AdminChatResponse, api, asList, Model } from "@/lib/api";
import { Button, Card, PageHeader, Select } from "@/components/ui";
import { toast } from "@/components/toaster";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  meta?: AdminChatResponse["meta"];
}

export default function ChatPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [model, setModel] = useState("");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .get("/api/admin/models")
      .then((d) => asList<Model>(d))
      .then((list) => {
        const enabled = list.filter((m) => m.enabled);
        setModels(enabled);
        if (enabled[0]) setModel(enabled[0].model_name);
      })
      .catch((e) => toast.error(e.message));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;
    if (!model) {
      toast.error("请先在“模型”页面启用至少一个模型");
      return;
    }
    setInput("");
    const history: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages(history);
    setSending(true);
    try {
      const res = await api.post<AdminChatResponse>("/api/admin/chat", {
        model,
        messages: history.map((m) => ({ role: m.role, content: m.content })),
      });
      const content =
        res.payload?.choices?.[0]?.message?.content ?? "(无内容)";
      setMessages([...history, { role: "assistant", content, meta: res.meta }]);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "请求失败");
      setMessages(history);
    } finally {
      setSending(false);
    }
  }, [input, messages, model, sending]);

  return (
    <div className="flex h-[calc(100vh-7rem)] flex-col">
      <PageHeader title="对话" subtitle="通过竞速引擎直接测试已启用的模型" actions={
        <>
          <Select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="w-64 !bg-white/[0.03]"
          >
            <option value="">选择模型…</option>
            {models.map((m) => (
              <option key={m.id} value={m.model_name}>
                {m.display_name || m.model_name}
              </option>
            ))}
          </Select>
          <Button
            onClick={() => setMessages([])}
            disabled={!messages.length}
            variant="ghost"
          >
            <Trash2 size={14} /> 清空
          </Button>
        </>
      } />

      <Card className="flex-1 overflow-y-auto p-4">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-gray-600">
            <Bot size={30} />
            <p className="text-sm">选择模型并开始对话。请求会经过代理竞速引擎转发到 NVIDIA。</p>
          </div>
        ) : (
          <div className="space-y-4">
            {messages.map((m, i) => (
              <div key={i} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
                <div
                  className={
                    m.role === "user"
                      ? "max-w-[75%] rounded-2xl rounded-br-md bg-accent/15 px-4 py-2.5 text-sm text-gray-100"
                      : "max-w-[80%] rounded-2xl rounded-bl-md border border-white/10 bg-white/[0.04] px-4 py-2.5 text-sm text-gray-200"
                  }
                >
                  <p className="whitespace-pre-wrap leading-relaxed">{m.content}</p>
                  {m.meta && (
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-white/10 pt-2 text-[11px] text-gray-500">
                      <span>{m.meta.duration_ms}ms</span>
                      <span>线路: {m.meta.route_type === "direct" ? "直连" : m.meta.proxy_name}</span>
                      <span>Key: {m.meta.key_name}</span>
                      <span>
                        tokens: {(m.meta.usage?.prompt_tokens ?? 0)} + {(m.meta.usage?.completion_tokens ?? 0)} = {(m.meta.usage?.total_tokens ?? 0)}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            ))}
            {sending && (
              <div className="flex items-center gap-2 text-sm text-gray-500">
                <Loader2 size={14} className="animate-spin" /> 竞速请求中…
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </Card>

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
          className="flex-1 resize-none rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3 text-sm text-gray-200 outline-none transition-colors placeholder:text-gray-600 focus:border-accent/60"
        />
        <Button variant="primary" onClick={() => void send()} disabled={!input.trim() || sending}>
          {sending ? <Loader2 size={16} className="animate-spin" /> : <CornerDownLeft size={16} />}
          发送
        </Button>
      </div>
    </div>
  );
}
