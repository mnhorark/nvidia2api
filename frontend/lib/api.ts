export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";

export const TOKEN_KEY = "nvidia2api_admin_token";
export const CHANNEL_KEY = "nvidia2api_channel";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

/** 当前选中的渠道 slug；切换渠道后所有管理接口自动带上 X-Channel */
export function getChannel(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem(CHANNEL_KEY) ?? "";
}

export function setChannel(slug: string) {
  localStorage.setItem(CHANNEL_KEY, slug);
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("nvidia2api:channel-change", { detail: slug }));
  }
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function extractErrorMessage(data: unknown, status: number): string {
  if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    if (typeof obj.detail === "string") return obj.detail;
    if (typeof obj.message === "string") return obj.message;
    if (obj.error && typeof obj.error === "object") {
      const err = obj.error as Record<string, unknown>;
      if (typeof err.message === "string") return err.message;
    }
    if (typeof obj.error === "string") return obj.error;
  }
  return `请求失败 (HTTP ${status})`;
}

export async function request<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Token ${token}`;
  // 渠道作用域：所有管理接口按当前渠道过滤，页面无需各自传参
  const channel = getChannel();
  if (channel) headers["X-Channel"] = channel;

  const res = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });

  if (res.status === 401 || res.status === 403) {
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      clearToken();
      window.location.href = "/login";
    }
  }

  let data: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    throw new ApiError(extractErrorMessage(data, res.status), res.status);
  }
  return data as T;
}

/** 兼容返回 [..] 或 {results:[..]} 或 {data:[..]} */
export function asList<T = Record<string, unknown>>(data: unknown): T[] {
  if (Array.isArray(data)) return data as T[];
  if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    if (Array.isArray(obj.results)) return obj.results as T[];
    if (Array.isArray(obj.data)) return obj.data as T[];
  }
  return [];
}

export const api = {
  get: <T = unknown>(path: string) => request<T>(path, { method: "GET" }),
  post: <T = unknown>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T = unknown>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  del: <T = unknown>(path: string) => request<T>(path, { method: "DELETE" }),
};

// ---------- Types ----------
export interface Channel {
  id: number;
  name: string;
  slug: string;
  base_url: string;
  chat_path: string;
  models_path: string;
  chat_url: string;
  models_url: string;
  key_prefix: string;
  auth_scheme: string;
  default_rpm: number;
  allow_duplicate_keys: boolean;
  enabled: boolean;
  is_default: boolean;
  notes: string;
  key_count: number;
  enabled_key_count: number;
  proxy_count: number;
  enabled_proxy_count: number;
  model_count: number;
  enabled_model_count: number;
  created_at: string;
  updated_at: string;
}

export interface ChannelKey {
  id: number;
  channel: number | null;
  name: string;
  api_key: string; // 已脱敏
  status: string;
  enabled?: boolean;
  rpm_limit: number;
  minute_request_count: number;
  remaining_rpm?: number;
  success_count: number;
  failure_count: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProxyGroup {
  id: number;
  name: string;
  description?: string;
  country?: string;
  enabled: boolean;
  proxy_count?: number;
  created_at: string;
  updated_at: string;
}

export interface Proxy {
  id: number;
  name: string;
  protocol: string;
  host: string;
  port: number;
  group: number | null;
  group_name?: string;
  country?: string;
  region?: string;
  city?: string;
  enabled: boolean;
  status: string;
  latency: number | null;
  last_check_time: string | null;
  public_ip?: string;
  success_count: number;
  failure_count: number;
  created_at: string;
  updated_at: string;
}

export interface Model {
  id: number;
  model_name: string;
  display_name?: string;
  alias?: string;
  route_priority?: number;
  public_name?: string;
  description?: string;
  proxy_group?: number | null;
  proxy_group_name?: string;
  provider?: string;
  status?: string;
  enabled: boolean;
  endpoint?: string;
  created_at: string;
  updated_at: string;
}

export interface UserApiKey {
  id: number;
  name: string;
  key_prefix: string;
  enabled: boolean;
  rate_limit: number;
  total_requests: number;
  success_requests: number;
  failed_requests: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface LogRoute {
  name: string;
  kind: "direct" | "proxy";
  key_name: string;
  proxy_name: string;
  status: string;
  latency_ms: number;
  error: string;
  http_status: number;
}

export interface RequestLog {
  id: number;
  request_id: string;
  model: string;
  status: string;
  http_status?: number;
  duration_ms?: number;
  routes?: LogRoute[];
  is_winner?: boolean;
  is_stream?: boolean;
  winner_key_name?: string;
  winner_proxy_name?: string;
  winner_route_type?: string;
  proxy_public_ip?: string;
  error_type?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
  first_token_ms?: number;
  created_at: string;
}

export interface DashboardStats {
  channel?: string;
  channel_name?: string;
  nvidia_keys: number;
  enabled_keys: number;
  proxies: number;
  enabled_proxies: number;
  max_proxies?: number;
  max_enabled_proxies?: number;
  direct_routes?: number;
  models: number;
  enabled_models: number;
  requests_today: number;
  success_rate: number;
  avg_latency?: number;
  avg_latency_s?: number;
  active_requests?: number;
  key_status?: Record<string, number>;
  proxy_status?: Record<string, number>;
}

export interface SystemSetting {
  [key: string]: string | number | boolean;
}

export interface RuntimeParam {
  key: string;
  type: "int" | "float" | "bool" | "str";
  value: number | string;
  default: number | string;
  description: string;
  overridden: boolean;
}

export interface TokenUsageDay {
  date: string;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  requests: number;
  success: number;
}

export interface UsageTotals {
  requests: number;
  success: number;
  success_rate: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  avg_latency_s: number | null;
  avg_ttft_ms: number | null;
  cache_hit_rate: number;
}

export interface UsageTotalsPrev {
  requests: number;
  total_tokens: number;
  success_rate: number;
}

export interface ChannelUsage {
  name: string;
  requests: number;
  total_tokens: number;
}

export interface ModelUsage {
  model: string;
  requests: number;
  success: number;
  success_rate: number;
  total_tokens: number;
  avg_latency_s: number | null;
}

export interface UsageResponse {
  granularity: "hour" | "day";
  days: TokenUsageDay[];
  totals: UsageTotals;
  prev_totals: UsageTotalsPrev;
  models: ModelUsage[];
  channels: ChannelUsage[];
  keys: ChannelUsage[];
}

export interface AdminChatRoute {
  name: string;
  kind: "direct" | "proxy";
  key_name: string;
  proxy_name: string;
  status: "winner" | "failed" | "cancelled" | string;
  latency_ms: number;
  error: string;
  http_status: number;
}

export interface AdminChatMeta {
  route_type?: string;
  key_name?: string;
  proxy_name?: string;
  duration_ms?: number;
  first_chunk_ms?: number;
  first_token_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
  usage?: Record<string, number>;
  routes?: AdminChatRoute[];
}

export interface AdminChatResponse {
  request_id: string;
  payload: {
    choices?: {
      message?: {
        role?: string;
        content?: string;
        reasoning_content?: string;
      };
    }[];
  };
  meta: AdminChatMeta;
}
