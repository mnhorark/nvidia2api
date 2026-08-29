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

const badgeLabels: Record<string, string> = {
  available: "正常",
  healthy: "正常",
  enabled: "启用",
  success: "成功",
  rate_limited: "限流",
  degraded: "降级",
  error: "异常",
  unhealthy: "异常",
  failed: "失败",
  invalid: "无效",
  disabled: "禁用",
  unknown: "未知",
};

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
export function Modal({
  open,
  title,
  onClose,
  children,
  wide,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 animate-fade"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cx(
          "w-full max-h-[85vh] overflow-y-auto rounded-xl border border-line-strong bg-[#151619] shadow-pop p-6 animate-modal",
          wide ? "max-w-2xl" : "max-w-md"
        )}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-center justify-between">
          <h3 className="text-[15px] font-semibold text-gray-100">{title}</h3>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="-m-1.5 rounded-md p-1.5 text-faint transition-colors hover:bg-white/[0.07] hover:text-gray-200"
          >
            <X size={16} />
          </button>
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
}: {
  head: React.ReactNode;
  children: React.ReactNode;
  empty?: string;
  loading?: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-line bg-panel">
      <table className="w-full min-w-max border-collapse">
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
