// 用 ?? 而非 ||：Docker 构建显式注入空串（同源相对路径），
// 空串是 falsy，|| 会把它错误地回落到 127.0.0.1，导致 all-in-one
// 镜像在非本机访问时所有 API 请求打到访客自己的回环地址。
// 仅在变量未设置（本地开发）时才回落本机后端。
export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

export const TOKEN_KEY = "nvidia2api_admin_token";
export const CHANNEL_KEY = "nvidia2api_channel";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  const v = localStorage.getItem(TOKEN_KEY);
  // 历史 bug 的残留防御：异常响应曾把字面量 "undefined" / "null" 写进
  // localStorage，导致"看起来已登录"却每次请求都 401。当成未登录处理。
  if (!v || v === "undefined" || v === "null") return null;
  return v;
}

/** 当前选中的渠道 slug；切换渠道后所有管理接口自动带上 X-Channel */
export function getChannel(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem(CHANNEL_KEY) ?? "";
}

export function setChannel(slug: string) {
  if (typeof window === "undefined") return;
  // 值未变化时不写 localStorage、不派发事件：否则 layout 监听事件后会再走
  // loadChannels -> setChannel -> 派发事件，形成无限请求循环（GET /api/admin/channels 刷屏）。
  if (slug === localStorage.getItem(CHANNEL_KEY)) return;
  localStorage.setItem(CHANNEL_KEY, slug);
  window.dispatchEvent(new CustomEvent("nvidia2api:channel-change", { detail: slug }));
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

// 在途 GET 去重：轮询/channel 切换/组件重挂载经常对同一端点打重复请求，
// 合并为一个 Promise 直接砍半重复流量。键含 token+渠道，防止串响应。
const _inflightGet = new Map<string, Promise<unknown>>();

export async function request<T = unknown>(
  path: string,
  options: RequestInit = {},
  timeoutMs = 60_000,
  allowRetry = true,
): Promise<T> {
  const isGet = !options.method || options.method === "GET";
  if (isGet && !options.signal) {
    const key = `${path}|${getToken()}|${getChannel()}|${timeoutMs}`;
    const hit = _inflightGet.get(key);
    if (hit) return hit as Promise<T>;
    const p = _requestInner<T>(path, options, timeoutMs, allowRetry)
      .finally(() => { _inflightGet.delete(key); });
    _inflightGet.set(key, p);
    return p;
  }
  return _requestInner<T>(path, options, timeoutMs, allowRetry);
}

async function _requestInner<T = unknown>(
  path: string,
  options: RequestInit,
  timeoutMs: number,
  allowRetry: boolean,
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

  // 超时兜底：后端挂起时不让按钮永远停在 loading
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const signal = options.signal ?? controller.signal;

  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, { ...options, headers, signal });
  } catch (e) {
    if ((e as Error)?.name === "AbortError") {
      // 幂等 GET 在超时后重试一次：桥接 --reload / 上游瞬断造成的空窗，
      // 避免前端"完全加载不出内容"；非 GET（写操作）绝不自动重试防重复提交。
      if (allowRetry && (!options.method || options.method === "GET")) {
        await new Promise((r) => setTimeout(r, 1000));
        return request<T>(path, options, timeoutMs, false);
      }
      throw new ApiError(`请求超时（${Math.round(timeoutMs / 1000)}s）`, 0);
    }
    if (allowRetry && (!options.method || options.method === "GET")) {
      // 连接被拒/中断等瞬时网络错误：同样重试一次
      await new Promise((r) => setTimeout(r, 1000));
      return request<T>(path, options, timeoutMs, false);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }

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
  get: <T = unknown>(path: string, timeoutMs?: number) =>
    request<T>(path, { method: "GET" }, timeoutMs),
  post: <T = unknown>(path: string, body?: unknown, timeoutMs?: number) =>
    request<T>(path, {
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }, timeoutMs),
  patch: <T = unknown>(path: string, body: unknown, timeoutMs?: number) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }, timeoutMs),
  del: <T = unknown>(path: string, timeoutMs?: number) =>
    request<T>(path, { method: "DELETE" }, timeoutMs),
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
  disable_key_invalid: boolean;
  disable_proxy_unhealthy: boolean;
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
  is_anonymous?: boolean; // 无鉴权渠道的匿名线路（无需 Key）
  status: string;
  enabled?: boolean;
  rpm_limit: number;
  minute_request_count: number;
  remaining_rpm?: number;
  success_count: number;
  failure_count: number;
  last_used_at: string | null;
  /** 列表接口（ChannelKeyListSerializer）已裁掉这两个时间戳与 last_error
   *  （keys 页不渲染，占 339 行响应的 31%）；详情接口仍返回完整字段。 */
  created_at?: string;
  updated_at?: string;
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
  latency_ms: number | null;
  last_check_at: string | null;
  public_ip?: string;
  success_count: number;
  failure_count: number;
  /**
   * 列表接口（ProxyListSerializer）不再返回 created_at / updated_at / username /
   * password / url / region / city / isp —— 代理池页面一个都不渲染，而它们占
   * 300 行响应的 46%。标成可选，真去读时类型系统会强制处理 undefined，
   * 而不是运行时拿到一个不存在的值。详情接口仍返回完整字段。
   */
  created_at?: string;
  updated_at?: string;
}

export interface Model {
  id: number;
  model_name: string;
  display_name?: string;
  alias?: string;
  aliases?: string[];
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
  quota: number;
  used_quota: number;
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
  generation_speed?: number | null;
  // 交付量观测（仅流式请求有值；null = 非流式或修复前的历史行）。
  // 截断归因关键：completion_tokens=0 时靠这三个数区分"上游静默被掐"（该换线
  // 重跑）与"思考流了很久被掐"（绝不能换线——重跑会把已交付的思考再发一遍）。
  stream_chunks?: number | null;
  content_chars?: number | null;
  reasoning_chars?: number | null;
  created_at: string;
  // 客户端实际传入的思考参数
  client_thinking?: Record<string, unknown>;
  // 实际下发到上游的思考强度参数
  upstream_thinking?: Record<string, unknown>;
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
  group: string;
  overridden: boolean;
}

/**
 * 用量统计字段统一标为可空。
 *
 * 后端用 `Sum()`/`Count()` 聚合，空区间会返回 null；把这些类型写成非空
 * `number` 会让 TS 误以为可以安全调用 `.toFixed()` / `.toLocaleString()`，
 * 运行时却在渲染期抛错、整页白屏。标成可空后，类型系统会强制调用方兜底。
 */
export interface TokenUsageDay {
  date: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cached_tokens: number | null;
  total_tokens: number | null;
  requests: number | null;
  success: number | null;
}

export interface UsageTotals {
  requests: number | null;
  success: number | null;
  success_rate: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cached_tokens: number | null;
  total_tokens: number | null;
  avg_latency_s: number | null;
  avg_ttft_ms: number | null;
  cache_hit_rate: number | null;
}

export interface UsageTotalsPrev {
  requests: number | null;
  total_tokens: number | null;
  success_rate: number | null;
}

export interface ChannelUsage {
  name: string;
  requests: number | null;
  total_tokens: number | null;
}

export interface ModelUsage {
  model: string;
  requests: number | null;
  success: number | null;
  success_rate: number | null;
  total_tokens: number | null;
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
