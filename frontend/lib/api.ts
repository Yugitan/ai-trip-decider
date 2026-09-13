/**
 * 后端 API 客户端。
 *
 * 约定（PRD §7.2）：所有响应都是统一 envelope
 *   { ok: true, data, meta } | { ok: false, error, meta }
 * 客户端把 envelope 拆开：成功返回 data，失败抛 ApiError（带 code/message/hint）。
 *
 * 安全：这里只能访问我们自己的后端。任何第三方 Key 都不得出现在前端。
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

/**
 * 某次请求里 LLM 的真实使用情况（后端 `meta.llm`）。
 *
 * 它存在的意义是让"我们用了大模型"变成**可核验的事实**：没配 Key 时
 * `enabled=false`，降级时 `fallback_reasons` 非空，每次调用花了多少 token
 * 也直接写在这里。界面负责把它翻译成人话，而不只是转述字段名。
 */
export interface LlmStatus {
  enabled: boolean;
  used: boolean;
  provider: string;
  model: string | null;
  prompt_version: string;
  calls: number;
  cache_hits: number;
  tokens_in: number;
  tokens_out: number;
  /** 金额是**字符串**：后端用 Decimal，前端不做浮点换算 */
  cost_cny: string;
  cost_calibrated: boolean;
  /** 任务 → 归属：llm / cache / rule */
  tasks: Record<string, string>;
  fallback_reasons: string[];
}

export interface ApiMeta {
  request_id: string | null;
  cached: boolean;
  cache_layer: string | null;
  degraded_modes: string[];
  elapsed_ms: number | null;
  llm?: LlmStatus | null;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  hint: string;
  context: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly code: string;
  readonly hint: string;
  readonly status: number;
  readonly requestId: string | null;

  constructor(body: ApiErrorBody, status: number, requestId: string | null) {
    super(body.message);
    this.name = "ApiError";
    this.code = body.code;
    this.hint = body.hint;
    this.status = status;
    this.requestId = requestId;
  }
}

export interface ApiResult<T> {
  data: T;
  meta: ApiMeta;
}

interface Envelope<T> {
  ok: boolean;
  data: T | null;
  error: ApiErrorBody | null;
  meta: ApiMeta;
}

export class NetworkError extends Error {
  constructor(cause: unknown) {
    super("无法连接到规划服务，请确认后端已启动（make dev-backend）");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

/**
 * 取消异常。用名字判断而不是 `instanceof DOMException`：
 * 不同运行时（浏览器 / jsdom / Node）抛出的类型不完全一致，名字是稳定契约。
 */
function isAbortError(cause: unknown): boolean {
  return (
    typeof cause === "object" &&
    cause !== null &&
    (cause as { name?: unknown }).name === "AbortError"
  );
}

/**
 * 统一请求出口。导出是为了让其它 API 模块（如开发面板）复用 envelope 解析 ——
 * 各自再写一份 fetch 就意味着错误映射会慢慢漂移。
 */
export async function request<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      cache: "no-store",
    });
  } catch (cause) {
    // 请求被**取消**（用户离开页面；未来加超时控制）不是"连不上后端"。
    // 把 AbortError 报成"后端未启动"会把排障引到完全错误的方向，所以原样抛出去。
    if (isAbortError(cause)) throw cause;
    throw new NetworkError(cause);
  }

  let envelope: Envelope<T>;
  try {
    envelope = (await response.json()) as Envelope<T>;
  } catch (cause) {
    // 后端返回了非 JSON（例如反向代理的错误页）——不要假装成功
    throw new ApiError(
      {
        code: "INVALID_RESPONSE",
        message: `服务返回了无法解析的内容（HTTP ${response.status}）`,
        hint: "请稍后重试；若持续出现请检查后端日志。",
        context: {},
      },
      response.status,
      response.headers.get("X-Request-Id"),
    );
  }

  if (!envelope.ok || envelope.error) {
    throw new ApiError(
      envelope.error ?? {
        code: "INTERNAL",
        message: "服务出了点问题，请重试。",
        hint: "",
        context: {},
      },
      response.status,
      envelope.meta?.request_id ?? response.headers.get("X-Request-Id"),
    );
  }

  if (envelope.data === null) {
    throw new ApiError(
      {
        code: "EMPTY_RESPONSE",
        message: "服务没有返回数据",
        hint: "请重试。",
        context: {},
      },
      response.status,
      // 与上面错误分支取法一致：`meta.request_id` 优先，其次看响应头。
      // 文案让用户"把 X-Request-Id 反馈给我们"，这里就不能把它丢掉。
      envelope.meta?.request_id ?? response.headers.get("X-Request-Id"),
    );
  }

  return { data: envelope.data, meta: envelope.meta };
}

// ── 健康检查 ────────────────────────────────────────────────────────────────

export interface HealthData {
  status: "ok" | "degraded";
  version: string;
  env: string;
  schema_state: string;
  degraded_modes: string[];
  providers: Record<string, string>;
  database: {
    ok: boolean;
    latency_ms: number | null;
    active_places?: number;
    routes?: number;
    cities?: number;
    kb_version?: string | null;
    schema_ready?: boolean;
  };
  config: {
    scoring_version: string;
    limits_version: string;
    ttl_version: string;
    pricing_version: string;
    pricing_calibrated: boolean;
  };
}

export function getHealth(): Promise<ApiResult<HealthData>> {
  return request<HealthData>("/api/v1/health");
}

// ── 规划（M4 实现；前端已按最终契约接好）────────────────────────────────────

/**
 * 规划请求体 —— **这是线上格式，不是界面标签**。
 *
 * `city` 用 slug（`guangzhou`），`preferences` 用 `config/scoring.yaml` 的
 * `preference_dimensions` 键（`food` / `photo` …），两者都不是中文。
 * 界面上的中文到这里的换算只发生在 `components/planner-form.tsx` 的 `buildPayload()` 一处：
 * 曾经把中文标签直接发过来，后端稳定 422（
 * 它只认枚举值，而且**应该**只认枚举值 —— 放宽等于让接口语义随 UI 文案漂移）。
 */
export interface PlanRequest {
  city: string;
  days: number;
  people: number;
  preferences: string[];
  pace: "relaxed" | "balanced" | "packed";
  budget: { amount: number; scope: "per_person" | "total" } | null;
  free_text: string;
}

export function planTrip(payload: PlanRequest): Promise<ApiResult<{ request_id: string; stream_url: string }>> {
  return request("/api/v1/trips:plan", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ── 只读目录（知识库浏览）────────────────────────────────────────────────────
//
// 这组端点把知识库变成"软件里能看见的东西"。
// 注意所有"不确定性"字段都原样保留：verification_status / unknown_fields /
// sources / metrics_are_estimated —— 界面据此显示"信息可能变化"提示，
// 客户端**不得**把它们当作可省略的装饰字段。

export interface SourceRef {
  name: string;
  url: string | null;
  source_type: string;
  credibility: number;
  field_scope: string[];
  checked_at: string | null;
  http_status: number | null;
}

export interface CitySummary {
  slug: string;
  name: string;
  name_en: string | null;
  province: string | null;
  description: string | null;
  timezone: string;
  status: string;
  place_count: number;
  route_count: number;
  kb_version: string | null;
}

export interface PlaceSummary {
  id: string;
  name: string;
  name_en: string | null;
  category: string;
  district: string | null;
  latitude: number;
  longitude: number;
  address: string | null;
  recommended_duration_min: number | null;
  opening_hours_raw: string | null;
  price_min: number | null;
  price_max: number | null;
  indoor: boolean | null;
  best_time: string[];
  tags: string[];
  aliases: string[];
  scores: Record<string, number>;
  score_source: "curated" | "derived";
  verification_status: string;
  confidence: number | null;
  unknown_fields: string[];
  data_quality_flags: string[];
  sources: SourceRef[];
}

export interface PlacePage {
  city: string;
  filters: { q: string | null; category: string | null; district: string | null };
  page: { total: number; limit: number; offset: number; has_more: boolean };
  items: PlaceSummary[];
}

export interface CityStats {
  city: string;
  total_places: number;
  categories: { category: string; label: string; count: number }[];
  verification: Record<string, number>;
  opening_hours_known: number;
  opening_hours_unknown: number;
}

export interface RouteStop {
  seq: number;
  place_id: string;
  place_name: string;
  category: string;
  stay_min: number | null;
  note: string | null;
  transport_to_next: string | null;
  transport_to_next_min: number | null;
  distance_to_next_m: number | null;
}

export interface RouteSummary {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  route_type: string;
  archetype_hint: string | null;
  pace: string | null;
  difficulty: string | null;
  duration_min: number | null;
  walking_distance_m: number | null;
  estimated_transport_time_min: number | null;
  recommended_start_time: string | null;
  recommended_end_time: string | null;
  best_for: string[];
  stop_count: number;
  stops: RouteStop[];
  /** 恒为 true：时长与步行距离是估算值，界面必须如实标注 */
  metrics_are_estimated: boolean;
  /** 没有价格来源时恒为 null —— 不用编造的数字冒充 */
  estimated_budget: unknown | null;
  verification_status: string;
  source_name: string | null;
}

export function listCities(): Promise<ApiResult<{ items: CitySummary[] }>> {
  return request<{ items: CitySummary[] }>("/api/v1/cities");
}

export function getCityStats(city: string): Promise<ApiResult<CityStats>> {
  return request<CityStats>(`/api/v1/cities/${encodeURIComponent(city)}/stats`);
}

export function listPlaces(
  city: string,
  params: { q?: string; category?: string; limit?: number; offset?: number } = {},
): Promise<ApiResult<PlacePage>> {
  const query = new URLSearchParams();
  if (params.q) query.set("q", params.q);
  if (params.category) query.set("category", params.category);
  query.set("limit", String(params.limit ?? 24));
  query.set("offset", String(params.offset ?? 0));
  return request<PlacePage>(`/api/v1/cities/${encodeURIComponent(city)}/places?${query}`);
}

export function listRoutes(
  city: string,
  params: { archetype?: string; route_type?: string; with_stops?: boolean } = {},
): Promise<ApiResult<{ city: string; total: number; items: RouteSummary[] }>> {
  const query = new URLSearchParams();
  if (params.archetype) query.set("archetype", params.archetype);
  if (params.route_type) query.set("route_type", params.route_type);
  query.set("with_stops", String(params.with_stops ?? true));
  return request<{ city: string; total: number; items: RouteSummary[] }>(
    `/api/v1/cities/${encodeURIComponent(city)}/routes?${query}`,
  );
}
