"use client";

import React from "react";
import { Loader2, X } from "lucide-react";

/* ---------- helpers ---------- */
export function NvidiaLogo({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 15"
      width={size}
      height={(size * 15) / 24}
      className={className}
      aria-label="NVIDIA"
    >
      <path
        fill="#76B900"
        d="M12 0.5C5.9 0.5 1.4 4.3.1 7.5c1.3 3.2 5.8 7 11.9 7s10.6-3.8 11.9-7C22.6 4.3 18.1.5 12 .5zM12 12c-2.6 0-4.6-2-4.6-4.5S9.4 3 12 3s4.6 2 4.6 4.5S14.6 12 12 12z"
      />
      <circle cx="12" cy="7.5" r="2.1" fill="#76B900" />
    </svg>
  );
}

export function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

/* ---------- Card ---------- */
export function Card({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cx(
        "rounded-xl border border-line bg-panel-strong p-5 shadow-panel",
        className
      )}
    >
      {children}
    </div>
  );
}

/* ---------- Button ---------- */
type ButtonVariant = "primary" | "ghost" | "danger" | "outline";

export function Button({
  variant = "ghost",
  size = "md",
  loading,
  className,
  children,
  disabled,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  loading?: boolean;
}) {
  const styles: Record<ButtonVariant, string> = {
    primary:
      "bg-accent text-[#0b0c0e] font-semibold border border-accent hover:bg-[#8fd400] active:translate-y-px",
    ghost:
      "bg-white/[0.04] hover:bg-white/[0.08] text-gray-200 border border-line hover:border-line-strong",
    danger:
      "bg-err/10 hover:bg-err/20 text-err border border-err/25",
    outline:
      "bg-transparent hover:bg-white/[0.05] text-gray-300 border border-line hover:border-line-strong",
  };
  const sizes: Record<string, string> = {
    sm: "h-7 px-2.5 text-xs rounded-md gap-1.5",
    md: "h-8 px-3.5 text-[13px] rounded-lg gap-1.5",
  };
  return (
    <button
      className={cx(
        "inline-flex items-center justify-center whitespace-nowrap font-medium transition-all duration-100",
        "disabled:opacity-40 disabled:cursor-not-allowed disabled:transform-none",
        sizes[size],
        styles[variant],
        className
      )}
      disabled={disabled || loading}
      {...rest}
    >
      {loading && <Loader2 size={size === "sm" ? 12 : 14} className="animate-spin" />}
      {children}
    </button>
  );
}

/* ---------- IconButton（表格行内操作） ---------- */
export function IconButton({
  danger,
  active,
  className,
  children,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  danger?: boolean;
  active?: boolean;
}) {
  return (
    <button
      className={cx(
        "flex h-7 w-7 items-center justify-center rounded-md transition-colors",
        "text-faint disabled:opacity-40 disabled:cursor-not-allowed",
        danger
          ? "hover:bg-err/10 hover:text-err"
          : active
            ? "bg-accent/10 text-accent"
            : "hover:bg-white/[0.07] hover:text-gray-200",
        className
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

/* ---------- Badge ---------- */
const badgeTones: Record<string, { dot: string; text: string }> = {
  available: { dot: "bg-ok", text: "text-ok" },
  healthy: { dot: "bg-ok", text: "text-ok" },
  enabled: { dot: "bg-ok", text: "text-ok" },
  success: { dot: "bg-ok", text: "text-ok" },
  rate_limited: { dot: "bg-warn", text: "text-warn" },
  degraded: { dot: "bg-warn", text: "text-warn" },
  error: { dot: "bg-err", text: "text-err" },
  unhealthy: { dot: "bg-err", text: "text-err" },
  failed: { dot: "bg-err", text: "text-err" },
  invalid: { dot: "bg-err", text: "text-err" },
  disabled: { dot: "bg-faint", text: "text-mute" },
  unknown: { dot: "bg-faint", text: "text-mute" },
};

/**
 * 状态 → 中文名的**单一来源**。
 *
 * 此前 dashboard 里另有一份一模一样的 `statusLabels`（两处逐字重复），
 * 后端新增状态时只会改到其中一份，另一份静默漏翻——用户看到裸英文枚举。
 * Badge 与仪表盘的状态分布面板现在共用这一份。
 */
export const STATUS_LABELS: Record<string, string> = {
  available: "正常",
  healthy: "正常",
  enabled: "启用",
  success: "成功",
  pending: "处理中",
  rate_limited: "限流",
  degraded: "降级",
  error: "异常",
  unhealthy: "异常",
  failed: "失败",
  invalid: "无效",
  disabled: "禁用",
  unknown: "未知",
};

/** 未登记的状态原样返回，宁可露出英文枚举也不要把未知含义翻译成错的中文。 */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

const badgeLabels = STATUS_LABELS;

export function StatusDot({ status, className }: { status: string; className?: string }) {
  const tone = badgeTones[status] ?? { dot: "bg-info", text: "text-info" };
  return (
    <span className={cx("relative inline-flex h-2 w-2 shrink-0", className)}>
      {(status === "healthy" || status === "available") && (
        <span className={cx("absolute inline-flex h-full w-full animate-ping rounded-full opacity-40", tone.dot)} />
      )}
      <span className={cx("relative inline-flex h-2 w-2 rounded-full", tone.dot)} />
    </span>
  );
}

export function Badge({ status, label }: { status: string; label?: string }) {
  const tone = badgeTones[status] ?? { dot: "bg-info", text: "text-info" };
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-md border border-white/[0.06] bg-white/[0.03] px-2 py-0.5 text-xs font-medium",
        tone.text
      )}
    >
      <span className={cx("h-1.5 w-1.5 rounded-full", tone.dot)} />
      {label || badgeLabels[status] || status}
    </span>
  );
}

/* ---------- Modal ---------- */
const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),' +
  'input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export function Modal({
  open,
  title,
  onClose,
  children,
  wide,
  dismissable = true,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
  /**
   * 是否允许 Esc / 点遮罩 / 右上角 X 关闭。默认 true。
   *
   * 传 false 的两类场景：
   * 1) 提交在途（`dismissable={!saving}`）——此前 Modal 完全没有这个开关，
   *    任何弹窗都能在请求飞行中被 Esc 关掉，结果落进已卸载的 state；
   * 2) 只展示一次的机密（用户 API Key）——误按 Esc 就等于永久丢失这把 Key。
   */
  dismissable?: boolean;
}) {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const restoreRef = React.useRef<HTMLElement | null>(null);

  React.useEffect(() => {
    if (!open || !dismissable) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, dismissable, onClose]);

  // 打开时把焦点移入面板，关闭时归还给触发元素。
  // 没有这段的话，弹窗打开后焦点仍停在遮罩后面的页面上：Tab 会一项项跳过
  // 用户根本看不见的背景内容，读屏用户完全不知道弹窗出现过。
  React.useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    const first = panelRef.current?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panelRef.current)?.focus();
    return () => restoreRef.current?.focus?.();
  }, [open]);

  if (!open) return null;

  // 焦点陷阱：Tab 到最后一个可聚焦元素后回到第一个（Shift+Tab 反向）。
  const trapTab = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab") return;
    const root = panelRef.current;
    if (!root) return;
    const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === root)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 animate-fade"
      onClick={dismissable ? onClose : undefined}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onKeyDown={trapTab}
        className={cx(
          "w-full max-h-[85vh] overflow-y-auto rounded-xl border border-line-strong bg-[#151619] shadow-pop p-6 animate-modal outline-none",
          wide ? "max-w-2xl" : "max-w-md"
        )}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-center justify-between">
          <h3 className="text-[15px] font-semibold text-gray-100">{title}</h3>
          {dismissable && (
            <button
              onClick={onClose}
              aria-label="关闭"
              className="-m-1.5 rounded-md p-1.5 text-faint transition-colors hover:bg-white/[0.07] hover:text-gray-200"
            >
              <X size={16} />
            </button>
          )}
        </div>
        {children}
      </div>
    </div>
  );
}

/* ---------- Field / Input ---------- */
export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium text-mute">{label}</span>
      {children}
    </label>
  );
}

const inputCls =
  "w-full rounded-lg border border-line bg-[#0f1013] px-3 py-2 text-[13px] text-gray-100 outline-none transition-colors placeholder:text-faint focus:border-accent/60 focus:ring-2 focus:ring-accent/20 hover:border-line-strong";

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx(inputCls, props.className)} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cx(inputCls, "font-mono leading-relaxed", props.className)} />;
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={cx(
        inputCls,
        "cursor-pointer appearance-none pr-8 bg-no-repeat [&>option]:bg-[#151619] [&>option]:text-gray-200",
        props.className
      )}
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b9099' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
        backgroundPosition: "right 0.65rem center",
        ...props.style,
      }}
    />
  );
}

/* ---------- Table ---------- */
export function Th({ children, className }: { children?: React.ReactNode; className?: string }) {
  return (
    <th
      scope="col"
      className={cx(
        "px-3 py-2.5 text-left text-[11px] font-medium uppercase tracking-wider text-faint",
        className
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  className,
  title,
  colSpan,
}: {
  children?: React.ReactNode;
  className?: string;
  title?: string;
  colSpan?: number;
}) {
  return (
    <td
      colSpan={colSpan}
      title={title}
      className={cx(
        "px-3 py-2.5 text-[13px] text-gray-300 [font-variant-numeric:tabular-nums]",
        className
      )}
    >
      {children}
    </td>
  );
}

export function DataTable({
  head,
  children,
  empty,
  loading,
  fill,
}: {
  head: React.ReactNode;
  children: React.ReactNode;
  empty?: string;
  loading?: boolean;
  /** 填满容器宽度：去掉 min-w-max，让长内容单元格（模型名等）能按 max-w 收缩省略 */
  fill?: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-line bg-panel">
      <table className={cx("w-full border-collapse", !fill && "min-w-max")}>
        <thead>
          <tr className="border-b border-line bg-white/[0.015]">{head}</tr>
        </thead>
        <tbody className="divide-y divide-line/60">
          {children}
          {!loading && React.Children.count(children) === 0 && (
            <tr>
              <td colSpan={50} className="px-3 py-14 text-center">
                <p className="text-[13px] text-faint">{empty || "暂无数据"}</p>
              </td>
            </tr>
          )}
          {loading && (
            <tr>
              <td colSpan={100} className="px-3 py-14 text-center">
                <Loader2 className="mx-auto animate-spin text-faint" size={20} />
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- Checkbox ---------- */
export function Checkbox({
  checked,
  onChange,
  indeterminate,
  disabled,
  ariaLabel,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  indeterminate?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  const ref = React.useRef<HTMLInputElement>(null);
  React.useEffect(() => {
    if (ref.current) ref.current.indeterminate = !!indeterminate && !checked;
  }, [indeterminate, checked]);
  return (
    <input
      ref={ref}
      type="checkbox"
      role="checkbox"
      aria-label={ariaLabel}
      checked={checked}
      disabled={disabled}
      onChange={(e) => onChange(e.target.checked)}
      className={cx(
        "h-3.5 w-3.5 shrink-0 rounded accent-[#76b900] cursor-pointer align-middle",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
        "disabled:cursor-not-allowed disabled:opacity-40"
      )}
    />
  );
}

/* ---------- Toggle ---------- */
export function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx(
        "relative h-[18px] w-8 rounded-full transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
        "disabled:opacity-40 disabled:cursor-not-allowed",
        checked ? "bg-accent" : "bg-white/[0.12]"
      )}
    >
      <span
        className={cx(
          "absolute top-[2px] h-[14px] w-[14px] rounded-full transition-all duration-150",
          checked ? "left-[16px] bg-[#0b0c0e]" : "left-[2px] bg-gray-400"
        )}
      />
    </button>
  );
}

/* ---------- Page header ---------- */
export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-[17px] font-semibold tracking-tight text-gray-100">{title}</h1>
        {subtitle && <p className="mt-0.5 text-[13px] text-faint">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/* ---------- 批量操作条 ---------- */
export function BatchBar({ count, children }: { count: number; children: React.ReactNode }) {
  if (count === 0) return null;
  return (
    <div className="animate-rise mb-3 flex flex-wrap items-center gap-2 rounded-xl border border-accent/25 bg-accent/[0.06] px-4 py-2.5">
      <span className="text-[13px] text-mute">
        已选 <b className="font-semibold text-gray-100">{count}</b> 项
      </span>
      <span className="h-4 w-px bg-white/[0.12]" />
      {children}
    </div>
  );
}

/* ---------- misc ---------- */
export function fmtTime(v?: string | null) {
  if (!v) return "—";
  const d = new Date(v);
  if (isNaN(d.getTime())) return v;
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function fmtLatency(ms?: number | null) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function safePct(ok: number, total: number) {
  if (!total) return "—";
  return `${((ok / total) * 100).toFixed(1)}%`;
}

/* ---------- 错误条 ---------- */
/**
 * 页内错误提示。此前 8 个页面各自逐字抄同一串 className，
 * 只有 dashboard 那份带重试按钮 —— 其余页面出错后只能整页刷新重来。
 * 统一到这里，重试变成可选参数。
 */
export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-err/25 bg-err/10 px-3 py-2 text-[13px] text-err"
    >
      <span className="min-w-0 break-words">{message}</span>
      {onRetry && (
        <button
          onClick={onRetry}
          className="shrink-0 rounded-md border border-err/25 bg-err/10 px-2.5 py-1 text-xs font-medium text-err transition-colors hover:bg-err/20"
        >
          重试
        </button>
      )}
    </div>
  );
}

/* ---------- 应用内确认框 ---------- */
export interface ConfirmOptions {
  title: string;
  message: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  /** 需要逐字输入这段文本才能点确认。用于级联删除这类不可恢复操作。 */
  requireText?: string;
}

type ConfirmRequest = {
  opts: ConfirmOptions;
  resolve: (v: boolean) => void;
};

let confirmRequest: ((opts: ConfirmOptions) => Promise<boolean>) | null = null;

/**
 * 替代原生 `confirm()`。原生框是系统浅色样式，贴在深色控制台里视觉断裂，
 * 阻塞主线程，而且没法要求"输入名称确认"这种强确认——
 * 删除渠道会连带删掉其下所有 Key / 代理 / 模型 / 日志，用的却是一个 OK/Cancel。
 *
 * 宿主未挂载时降级回原生 confirm：宁可样式丑，也绝不能静默返回 false
 * 让按钮看起来点了没反应。
 */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  if (confirmRequest) return confirmRequest(opts);
  const text = typeof opts.message === "string" ? opts.message : opts.title;
  return Promise.resolve(window.confirm(`${opts.title}\n\n${text}`));
}

/** 挂在根布局里，全应用共用一个确认框宿主。 */
export function ConfirmHost() {
  const [req, setReq] = React.useState<ConfirmRequest | null>(null);
  const [typed, setTyped] = React.useState("");

  React.useEffect(() => {
    confirmRequest = (opts) =>
      new Promise<boolean>((resolve) => {
        setTyped("");
        setReq({ opts, resolve });
      });
    return () => {
      confirmRequest = null;
    };
  }, []);

  if (!req) return null;
  const { opts, resolve } = req;
  const blocked = !!opts.requireText && typed.trim() !== opts.requireText;
  const done = (v: boolean) => {
    setReq(null);
    resolve(v);
  };

  return (
    <Modal open title={opts.title} dismissable={false} onClose={() => done(false)}>
      <div className="space-y-4">
        <div className="text-[13px] leading-relaxed text-gray-300">{opts.message}</div>
        {opts.requireText && (
          <Field label={`输入「${opts.requireText}」以确认`}>
            <Input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={opts.requireText}
            />
          </Field>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={() => done(false)}>
            {opts.cancelText ?? "取消"}
          </Button>
          <Button
            type="button"
            variant={opts.danger ? "danger" : "primary"}
            disabled={blocked}
            onClick={() => done(true)}
          >
            {opts.confirmText ?? "确认"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
