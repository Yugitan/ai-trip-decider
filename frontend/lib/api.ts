/**
 * 后端 API 客户端。
 *
 * 约定（PRD §7.2）：所有响应都是统一 envelope
 *   { ok: true, data, meta } | { ok: false, error, meta }
 * 客户端把 envelope 拆开：成功返回 data，失败抛 ApiError（带 code/message/hint）。
 *
 * 安全：这里只能访问我们自己的后端。任何第三方 Key 都不得出现在前端。
 */

/**
 * 后端地址（**服务端**用）。Next 只读 `frontend/.env*`，所以本机开发时通常取不到值，
 * 落到后端 `make dev-backend` 监听的地址。
 */
const SERVER_SIDE_BASE_URL = (
  process.env.API_BASE_URL ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");

/**
 * 算出访问后端的基地址。
 *
 * **浏览器**：空串 = 同源。请求打到 `/api/v1/...`，由 `next.config.ts` 的 `rewrites`
 * 转发给后端。这不是为了省一次 CORS，而是**必须**：页面在 `localhost:3000`、
 * 后端在 `127.0.0.1:8000` 时两者是跨站的，后端签发的游客会话 cookie 就成了
 * 第三方 cookie —— 浏览器会直接丢掉它，于是每个请求都是新会话，
 * `GET /trips/{id}` 永远 403，规划结果永远读不回来。
 *
 * **服务端（RSC / SSR）**：必须用**绝对**地址直连后端 —— Node 的 `fetch`
 * 收到相对 URL 会直接抛 `Failed to parse URL`。这条路径不带 cookie，
 * 只用来取公开/只读数据（如分享页），所以同源与否在这里没有意义。
 *
 * 独立成函数是为了能被单测写出两个环境的取值：模块加载时它就被定下来了，
 * 测试里没法再换一个 `window`。
 */
export function resolveApiBaseUrl(options: { serverSide: boolean }): string {
  if (process.env.NEXT_PUBLIC_API_BASE_URL) return process.env.NEXT_PUBLIC_API_BASE_URL;
  return options.serverSide ? SERVER_SIDE_BASE_URL : "";
}

export const API_BASE_URL = resolveApiBaseUrl({
  serverSide: typeof window === "undefined",
});

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
 * **必须带凭证**（`credentials: "include"`），否则后端的会话 cookie 会被浏览器丢掉。
 *
 * 这是一个真实踩过的坑：前端（`localhost:3000`）与后端（`127.0.0.1:8000`）是**跨域**的，
 * 而 `fetch` 的默认凭证模式是 `same-origin` —— 跨域响应上的 `Set-Cookie: td_session=…`
 * 既不会被发送、也不会被保存。后果是所有按会话隔离的接口
 * （`GET /trips/{id}`、`revise`、`undo`、`share`）都拿不到自己的行程，
 * 稳定返回 403 `FORBIDDEN`「这个行程不属于当前会话」：
 * 每次请求都是一个全新的会话，而行程是**按浏览器会话隔离**的。
 *
 * 注意这里必须与 `lib/plan-stream.ts` 的 `withCredentials: true` 一致：
 * 进度流（SSE）本来就是带凭证的，如果 POST 不带，同一次「开始规划」会分裂成两个会话 ——
 * 界面能收到进度，却永远读不到这份进度对应的行程。
 *
 * 后端侧不需要任何改动：CORS 已经回 `Access-Control-Allow-Credentials: true`
 * 与精确的 `Access-Control-Allow-Origin`（见 `backend/app/main.py`）。
 */
export const REQUEST_CREDENTIALS: RequestCredentials = "include";

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
      credentials: REQUEST_CREDENTIALS,
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

/**
 * 完整行程（`GET /api/v1/trips/{id}`，PRD §7.2）。
 *
 * 为什么 `plan.completed` 之后还要再请求一次：SSE 事件里只有**元信息**
 * （方案数 / 是否命中缓存 / 模型用量 / `trip_id`），路线与站点从来不进事件流。
 * 所以"点开始规划之后看不到东西"的唯一修法就是拿 `trip_id` 把行程读回来。
 *
 * 与后端 `app/schemas/trips.py` 的 `TripOut` / `TripRouteOut` / `TripStopOut` 一一对应。
 * 几个**刻意保留的不确定性字段**（界面的诚实性就靠它们，客户端不得当作可省略的装饰）：
 * - `budget_estimated` / `budget_unknown_items`：金额里有多少是估算、哪些项根本没价格；
 * - `transport_source`（`amap` / `osrm` / `estimated` / `manual`）：这趟交通是实测还是估算；
 * - `feasibility.violations` / `warnings`：可行性校验的硬性失败与软性提醒；
 * - `route_count_note`：少于 3 套方案时**为什么**少，而不是凑数。
 */
export interface TripStop {
  seq: number;
  place_id: string;
  name: string;
  category: string;
  latitude: number;
  longitude: number;
  district: string | null;
  arrive_time: string;
  depart_time: string;
  stay_min: number;
  transport_mode: string | null;
  transport_min: number | null;
  transport_distance_m: number | null;
  /** amap | osrm | estimated | manual —— 界面对 `estimated` 必须如实标注 */
  transport_source: string | null;
  why_recommended: string | null;
  tips: string | null;
  /** 校验码（如 `ESTIMATED_TRANSIT` / `HOURS_UNKNOWN`），不是给人看的句子 */
  warnings?: string[];
  snapshot?: Record<string, unknown>;
}

/** 可行性校验项。字段可缺省：这是**接口返回的不可信输入**，读取方必须自己容错。 */
export interface TripFeasibilityItem {
  code?: string;
  severity?: string;
  at_seq?: number | null;
  /** 后端已经写好的人话，界面优先用它而不是自己编 */
  message?: string;
  detail?: Record<string, unknown>;
}

export interface TripFeasibility {
  feasible?: boolean;
  /** 硬性失败（不该出现在用户手里的方案里） */
  violations?: TripFeasibilityItem[];
  /** 软性提醒（信息缺口、估算值…） */
  warnings?: TripFeasibilityItem[];
  metrics?: Record<string, number>;
}

export interface TripRoute {
  id: string;
  /** A / B / C */
  label: string;
  archetype: string;
  theme: string | null;
  name: string;
  /** 由**代码**算出来的站数/时长/步行距离，与模型写的文案严格分开 */
  one_liner: string | null;
  place_count: number;
  total_duration_min: number;
  walking_distance_m: number | null;
  transit_time_min: number | null;
  transit_distance_m: number | null;
  /** Decimal 在 JSON 里是**字符串**：前端不做浮点换算 */
  budget_min: string | null;
  budget_max: string | null;
  budget_scope: string | null;
  budget_estimated?: boolean;
  budget_unknown_items?: string[];
  recommend_score: number;
  score_breakdown?: Record<string, unknown>;
  feasibility?: TripFeasibility;
  best_for?: string[];
  highlights?: string[];
  pros?: string[];
  cons?: string[];
  /** 模型写的推荐理由（不含数字，见后端 llm_planner 的边界） */
  recommendation_reason: string | null;
  route_source: string | null;
  template_route_id: string | null;
  stops: TripStop[];
}

export interface TripOut {
  trip_id: string;
  request_id: string;
  city: string;
  title: string | null;
  days: number;
  revision_no: number;
  route_count: number;
  route_count_requested: number;
  /** 少于期望方案数时的原因（如候选地点不足），null 表示正好给足 */
  route_count_note?: string | null;
  intent?: Record<string, unknown>;
  degraded_modes: string[];
  total_cost_cny: string;
  generation_ms: number | null;
  created_at: string | null;
  is_public: boolean;
  share_slug: string | null;
  routes: TripRoute[];
}

/**
 * 读取一份完整行程。
 *
 * 归属由后端按会话判定（trip 只能被创建它的浏览器读到），所以这个请求
 * **依赖 `credentials: "include"`** —— 缺了它必然 403。
 */
export function getTrip(tripId: string): Promise<ApiResult<TripOut>> {
  return request<TripOut>(`/api/v1/trips/${encodeURIComponent(tripId)}`);
}

// ── 改路线 / 撤销 / 分享（M5）──────────────────────────────────────────────

/**
 * 修改指令的长度上限，与后端 `RevisionRequest.instruction`（`max_length=300`）一致。
 * 前端先按这个数限制输入，而不是等一个 422 回来才知道太长。
 */
export const REVISION_INSTRUCTION_LIMIT = 300;

/**
 * 两版行程的差异（后端 `DiffSummary.as_dict()`）。
 *
 * ★ 刻意**不收** `route_count_before/after` ★
 * 后端那两个字段实际上是「首要路线的**站点**数」（见 `trip_service.best_route_signature`），
 * 名字会骗人。界面不展示不确定含义的数字。
 *
 * 字段全部可缺省：这是接口返回的不可信输入，而且后端把它声明为 `dict[str, Any]`。
 */
export interface RevisionDiff {
  removed?: string[];
  added?: string[];
  walking_before_m?: number;
  walking_after_m?: number;
  walking_delta_m?: number;
  budget_before?: string | null;
  budget_after?: string | null;
  days_before?: number | null;
  days_after?: number | null;
  /** 后端拼好的一整句人话；界面优先用它，不自己再拼一遍 */
  sentence?: string;
}

export interface RevisionOut {
  /** 新版本的 trip_id（**与修改前不是同一条**） */
  trip_id: string;
  revision_no: number;
  diff: RevisionDiff;
  /** 非空表示"没听懂"：后端会反问一句，而不是静默丢搓或报错（AC-8.7） */
  needs_clarification: string | null;
}

/**
 * 用一句话改路线（PRD FR-08）。
 *
 * 后端**不联网**、不花模型的钱：规则引擎把指令解析成约束后本地重算（AC-8.2）。
 * 解析不出增量时**不报错** —— 返回 `needs_clarification` 反问一句。
 */
export function reviseTrip(
  tripId: string,
  instruction: string,
): Promise<ApiResult<RevisionOut>> {
  return request<RevisionOut>(`/api/v1/trips/${encodeURIComponent(tripId)}/revise`, {
    method: "POST",
    body: JSON.stringify({ instruction }),
  });
}

/**
 * 撤销到上一版（PRD AC-8.5）。
 *
 * 返回的是**上一版那份行程本身**：撤销是沿版本链后退，不是原地改，
 * 所以调用方要跟着切到新的 `trip_id`。已经是第一版时后端会返回
 * `INVALID_INPUT`（"这已经是最早的版本了"）—— 它会变成一条可读的错误，而不是一个空响应。
 */
export function undoTrip(tripId: string): Promise<ApiResult<TripOut>> {
  return request<TripOut>(`/api/v1/trips/${encodeURIComponent(tripId)}/undo`, {
    method: "POST",
  });
}

export interface ShareOut {
  slug: string;
  /** 已经拼好的前端分享页地址（`/t/{slug}`） */
  url: string;
  is_public: boolean;
}

/**
 * 生成 / 取消公开链接（PRD FR-09）。
 *
 * ★ 取消分享是**真的失效**，不是前端藏起来 ★
 * 后端在 `public=false` 时让 `GET /public/trips/{slug}` 立刻 404（AC-9.7），
 * 界面的文案必须把这一点说清楚。
 */
export function shareTrip(
  tripId: string,
  isPublic: boolean,
): Promise<ApiResult<ShareOut>> {
  return request<ShareOut>(`/api/v1/trips/${encodeURIComponent(tripId)}/share`, {
    method: "POST",
    body: JSON.stringify({ public: isPublic }),
  });
}

/**
 * 读一份**别人分享出来**的行程（无鉴权，只返回已公开的那些）。
 *
 * 它在服务端被调用（分享页是 RSC），所以不依赖浏览器 cookie ——
 * 这正是它跟 `getTrip` 的区别。
 */
export function getSharedTrip(slug: string): Promise<ApiResult<TripOut>> {
  return request<TripOut>(`/api/v1/public/trips/${encodeURIComponent(slug)}`);
}

/**
 * 「复制这套路线」：把别人分享的行程复制成**属于当前访客**的一份可编辑副本（PRD AC-9.4）。
 *
 * 后端返回的是复制出来的那一份完整行程（HTTP 201），调用方拿 `trip_id` 跳到
 * `/trip/{id}` 去改。两点注意：
 * - 它读的是**别人已公开**的 slug，但写出来的副本归当前会话 —— 与 `getSharedTrip`
 *   一样不要求登录，不同的是复制之后就能改了；
 * - 原行程不受任何影响（复制，不是接管）。
 */
export function copyPublicTrip(slug: string): Promise<ApiResult<TripOut>> {
  return request<TripOut>(`/api/v1/public/trips/${encodeURIComponent(slug)}/copy`, {
    method: "POST",
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
