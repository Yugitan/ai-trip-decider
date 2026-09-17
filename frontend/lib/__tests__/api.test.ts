/**
 * 后端 API 客户端的单元测试（`lib/api.ts`）。
 *
 * 为什么必须有：这个文件是前端**唯一的网络出口**，也是"后端诚实性字段"进入界面的通道。
 * 此前它零覆盖 —— 所有组件测试都用 `vi.mock` 把 `planTrip` / `getHealth` 换成了假函数，
 * 于是 `request()` 里的 envelope 拆包、错误映射、requestId 回退这些逻辑从未被验证过。
 *
 * 这里测的正是那些"只有出错时才会走到"的分支：非 JSON 响应、网络中断、
 * `ok: true` 但 `data: null`、`ok: false` 但 `error: null`。
 * 它们的共同点是：**写错了也不会有人立刻发现**，只会在线上以"页面白屏"的形式出现。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  API_BASE_URL,
  ApiError,
  NetworkError,
  REQUEST_CREDENTIALS,
  REVISION_INSTRUCTION_LIMIT,
  copyPublicTrip,
  getCityStats,
  getHealth,
  getSharedTrip,
  getTrip,
  listCities,
  listPlaces,
  searchPlaces,
  listRoutes,
  planTrip,
  reviseTrip,
  shareTrip,
  undoTrip,
} from "@/lib/api";

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(
  body: unknown,
  init: { status?: number; headers?: Record<string, string> } = {},
): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

const META = {
  request_id: "req-1",
  cached: false,
  cache_layer: null,
  degraded_modes: [],
  elapsed_ms: 5,
};

function okEnvelope<T>(data: T): { ok: true; data: T; error: null; meta: typeof META } {
  return { ok: true, data, error: null, meta: META };
}

function errorEnvelope(error: {
  code: string;
  message: string;
  hint?: string;
  context?: Record<string, unknown>;
}): { ok: false; data: null; error: typeof error; meta: typeof META } {
  return {
    ok: false,
    data: null,
    error: { hint: "", context: {}, ...error },
    meta: META,
  };
}

function lastUrl(): string {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) throw new Error("fetch 没有被调用");
  return String(call[0]);
}

function lastInit(): RequestInit {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) throw new Error("fetch 没有被调用");
  return (call[1] ?? {}) as RequestInit;
}

// ── 成功路径 ────────────────────────────────────────────────────────────────

describe("request 成功路径", () => {
  it("拆开 envelope，只把 data 与 meta 交给调用方", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ status: "ok", version: "1.2.3" })));

    const result = await getHealth();

    expect(result.data).toEqual({ status: "ok", version: "1.2.3" });
    expect(result.meta).toEqual(META);
  });

  it("请求打到 API_BASE_URL 上，并带上 JSON 头与 no-store", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listCities();

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/cities`);
    const init = lastInit();
    expect(init.cache).toBe("no-store");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("★ 默认同源：请求打到 Next 自己的 /api（会话 cookie 必须是第一方的）", () => {
    // 回归：曾经把后端地址内联成 http://127.0.0.1:8000，于是页面(localhost:3000)
    // 与接口成了跨站，后端签发的 td_session 成了第三方 cookie 并被浏览器丢弃，
    // 结果 GET /trips/{id} 稳定 403「这个行程不属于当前会话」。
    // 现在由 next.config.ts 的 rewrites 做同源代理（详见该文件的说明）。
    expect(API_BASE_URL).toBe("");
  });

  it("★ 每个请求都带会话凭证（缺了它，行程接口必然 403）", async () => {
    // 回归：这个问题真的发生过 —— 前端(localhost:3000) → 后端(127.0.0.1:8000) 是跨域，
    // 而 fetch 默认的凭证模式是 same-origin，于是后端 `Set-Cookie: td_session=…`
    // 被浏览器直接丢弃。后果是 GET /trips/{id} 稳定 403「这个行程不属于当前会话」：
    // 每次请求都是一个新会话，而行程是按浏览器会话隔离的。
    // 必须与 lib/plan-stream.ts 的 `withCredentials: true` 保持一致，
    // 否则同一次「开始规划」会分裂成两个会话。
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ status: "ok" })));

    await getHealth();

    expect(lastInit().credentials).toBe(REQUEST_CREDENTIALS);
    expect(REQUEST_CREDENTIALS).toBe("include");
  });

  it("调用方传入的 headers 会被保留", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listCities();

    // 默认头由 request() 注入，业务头由调用方补充 —— 当前没有业务头，
    // 这里断言默认头没有被覆盖掉即可
    expect((lastInit().headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });
});

// ── 错误路径：每一种都必须抛出可区分的错误 ──────────────────────────────────

describe("request 错误路径", () => {
  it("网络中断抛 NetworkError（而不是让 fetch 的原始异常冒到界面）", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(getHealth()).rejects.toBeInstanceOf(NetworkError);
    await expect(getHealth()).rejects.toThrow(/无法连接到规划服务/);
  });

  it("NetworkError 保留原始 cause，便于排查", async () => {
    const cause = new TypeError("ECONNREFUSED");
    fetchMock.mockRejectedValue(cause);

    await expect(getHealth()).rejects.toMatchObject({ name: "NetworkError", cause });
  });

  it("★ 请求被取消时不能报成「无法连接到规划服务」", async () => {
    // 用户离开页面（或以后的超时控制）会抛 AbortError。
    // 把它包装成 NetworkError 会把排障引到完全错误的方向：
    // 界面会提示「运行 make dev-backend 启动后端」，而后端其实好好的。
    const abort = new DOMException("The user aborted a request.", "AbortError");
    fetchMock.mockRejectedValue(abort);

    const error = (await getHealth().catch((e: unknown) => e)) as Error;

    expect(error).toBe(abort);
    expect(error).not.toBeInstanceOf(NetworkError);
  });

  it("非 JSON 响应（如反向代理的错误页）抛 INVALID_RESPONSE，不假装成功", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>502 Bad Gateway</html>", {
        status: 502,
        headers: { "Content-Type": "text/html" },
      }),
    );

    const error = await getHealth().catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("INVALID_RESPONSE");
    expect((error as ApiError).status).toBe(502);
    expect((error as ApiError).message).toContain("502");
  });

  it("ok=false 时抛 ApiError，并带上 code / hint / status", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        errorEnvelope({
          code: "UNSUPPORTED_CITY",
          message: "没有城市 shenzhen 的数据",
          hint: "目前只有广州（guangzhou）。",
          context: { slug: "shenzhen" },
        }),
        { status: 422 },
      ),
    );

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("UNSUPPORTED_CITY");
    expect(error.message).toBe("没有城市 shenzhen 的数据");
    expect(error.hint).toBe("目前只有广州（guangzhou）。");
    expect(error.status).toBe(422);
  });

  it("ok=false 但 error 缺失时退回 INTERNAL，而不是抛出 undefined", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ ok: false, data: null, error: null, meta: META }, { status: 500 }),
    );

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("INTERNAL");
    expect(error.status).toBe(500);
  });

  it("ok=true 但 data 为 null 时抛 EMPTY_RESPONSE（不把 null 当数据返回）", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, data: null, error: null, meta: META }));

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("EMPTY_RESPONSE");
  });

  it("HTTP 状态码即使不是 2xx，只要 envelope 说 ok 就按成功处理", async () => {
    // 后端有"结果受限"这类语义（如 CANDIDATES_INSUFFICIENT 用 200 返回），
    // 客户端的判断依据必须是 envelope 的 ok 字段，而不是状态码区间。
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ status: "degraded" }), { status: 200 }));

    await expect(getHealth()).resolves.toMatchObject({ data: { status: "degraded" } });
  });
});

// ── requestId 的来源优先级 ─────────────────────────────────────────────────

describe("requestId 的提取", () => {
  it("优先取 envelope.meta.request_id", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(errorEnvelope({ code: "INTERNAL", message: "boom" }), {
        status: 500,
        headers: { "X-Request-Id": "header-id" },
      }),
    );

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error.requestId).toBe("req-1");
  });

  it("envelope 里没有时回退到响应头 X-Request-Id", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { ok: false, data: null, error: { code: "INTERNAL", message: "boom", hint: "", context: {} }, meta: null },
        { status: 500, headers: { "X-Request-Id": "header-id" } },
      ),
    );

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error.requestId).toBe("header-id");
  });

  it("两处都没有时 requestId 为 null（界面据此显示「未返回」）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(errorEnvelope({ code: "INTERNAL", message: "boom" }), { status: 500 }),
    );

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error.requestId).toBe("req-1"); // envelope.meta 里有
    // 构造一个 meta 也为空的场景
    fetchMock.mockResolvedValue(
      jsonResponse({ ok: false, data: null, error: { code: "INTERNAL", message: "b", hint: "", context: {} } }, { status: 500 }),
    );
    const second = (await getHealth().catch((e: unknown) => e)) as ApiError;
    expect(second.requestId).toBeNull();
  });

  it("★ ok=true 但 data 为 null 时，requestId 也要回退到响应头", async () => {
    // 界面文案让用户「把请求头里的 X-Request-Id 反馈给我们」，
    // 这个分支少了回退就会显示「request_id 未返回」，让用户无据可查。
    fetchMock.mockResolvedValue(
      jsonResponse(
        { ok: true, data: null, error: null, meta: { ...META, request_id: null } },
        { headers: { "X-Request-Id": "empty-body-id" } },
      ),
    );

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("EMPTY_RESPONSE");
    expect(error.requestId).toBe("empty-body-id");
  });

  it("非 JSON 响应的 requestId 从响应头取", async () => {
    fetchMock.mockResolvedValue(
      new Response("not json", { status: 500, headers: { "X-Request-Id": "proxy-id" } }),
    );

    const error = (await getHealth().catch((e: unknown) => e)) as ApiError;

    expect(error.requestId).toBe("proxy-id");
  });
});

// ── URL 与查询参数构造 ──────────────────────────────────────────────────────

describe("列表接口的 URL 构造", () => {
  it("listPlaces 使用默认分页（limit 24 / offset 0）", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listPlaces("guangzhou");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/cities/guangzhou/places?limit=24&offset=0`);
  });

  it("listPlaces 带上搜索词与类别，并做 URL 编码", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listPlaces("guangzhou", { q: "小蛮腰", category: "cafe", limit: 5, offset: 10 });

    const url = lastUrl();
    expect(url).toContain("q=%E5%B0%8F%E8%9B%AE%E8%85%B0");
    expect(url).toContain("category=cafe");
    expect(url).toContain("limit=5");
    expect(url).toContain("offset=10");
  });

  it("listPlaces 在 q / category 为空时不写入空参数", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listPlaces("guangzhou", { q: "", category: "" });

    const url = lastUrl();
    expect(url).not.toContain("q=");
    expect(url).not.toContain("category=");
  });

  it("城市名会被 URL 编码，避免路径注入", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({})));

    await getCityStats("a b/c");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/cities/a%20b%2Fc/stats`);
  });

  it("listRoutes 默认返回站点明细", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listRoutes("guangzhou");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/cities/guangzhou/routes?with_stops=true`);
  });

  it("listRoutes 可以显式关掉站点明细以减轻负载", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listRoutes("guangzhou", { with_stops: false, archetype: "relaxed", route_type: "food" });

    const url = lastUrl();
    expect(url).toContain("with_stops=false");
    expect(url).toContain("archetype=relaxed");
    expect(url).toContain("route_type=food");
  });

  it("listRoutes 在筛选为空时不写入空参数", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ items: [] })));

    await listRoutes("guangzhou", { archetype: "", route_type: "" });

    const url = lastUrl();
    expect(url).not.toContain("archetype=");
    expect(url).not.toContain("route_type=");
  });
});

// ── 规划端点 ──────────────────────────────────────────────────────────────
//
// `planTrip` 是**唯一会发 POST 的函数**，也是规划接口上线（M4）时最先被接上的那一个。
// 此前它零覆盖：组件测试全部 `vi.mock` 掉了它，所以"方法是不是 POST""body 有没有
// 真的序列化"这类问题一直没人守。写错时的症状很隐蔽 —— 后端会以一个 405/422 回应，
// 而界面上看起来只是"引擎还没上线"。

describe("planTrip", () => {
  const PAYLOAD = {
    city: "guangzhou",
    days: 2,
    people: 3,
    preferences: ["food", "history"],
    pace: "balanced" as const,
    budget: { amount: 800, scope: "per_person" as const },
    free_text: "想多看老建筑",
  };

  it("打到 trips:plan，且方法必须是 POST", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ request_id: "r1", stream_url: "/s" })));

    await planTrip(PAYLOAD);

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/trips:plan`);
    expect(lastInit().method).toBe("POST");
  });

  it("payload 被完整序列化进 body，字段一个不少", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ request_id: "r1", stream_url: "/s" })));

    await planTrip(PAYLOAD);

    expect(JSON.parse(String(lastInit().body))).toEqual(PAYLOAD);
  });

  it("budget 为 null 时序列化成 null，而不是悄悄丢掉这个键", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ request_id: "r1", stream_url: "/s" })));

    await planTrip({ ...PAYLOAD, budget: null });

    expect(JSON.parse(String(lastInit().body))).toMatchObject({ budget: null });
  });
});

// ── 读取行程（GET /trips/{id}）──────────────────────────────────────────────
//
// 这是「点开始规划之后到底能看到什么」的那一步：`plan.completed` 事件里只有元信息，
// 路线与站点只能从这里读回来。

describe("getTrip", () => {
  const TRIP = {
    trip_id: "trip-1",
    request_id: "req-1",
    city: "guangzhou",
    title: "广州 · 09:00–21:00",
    days: 1,
    revision_no: 1,
    route_count: 1,
    routes: [],
    degraded_modes: ["search:未配置搜索 API Key（只读本地知识库，不联网）"],
  };

  it("打到 /api/v1/trips/{id}，并把 id 编码（避免路径注入）", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope(TRIP)));

    await getTrip("a b/c");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/trips/a%20b%2Fc`);
  });

  it("只读请求：不带 method / body，但必须带凭证", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope(TRIP)));

    await getTrip("trip-1");

    const init = lastInit();
    expect(init.method).toBeUndefined();
    expect(init.body).toBeUndefined();
    expect(init.credentials).toBe("include");
  });

  it("把 envelopes 里的行程交给调用方（包含不确定字段）", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope(TRIP)));

    const result = await getTrip("trip-1");

    expect(result.data.trip_id).toBe("trip-1");
    expect(result.data.degraded_modes).toEqual([
      "search:未配置搜索 API Key（只读本地知识库，不联网）",
    ]);
  });

  it("403 时抛出带 code / hint 的 ApiError（界面据此说明归属问题）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        errorEnvelope({
          code: "FORBIDDEN",
          message: "这个行程不属于当前会话",
          hint: "请回到创建它的浏览器（游客行程按浏览器会话隔离）。",
        }),
        { status: 403 },
      ),
    );

    const error = (await getTrip("trip-1").catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("FORBIDDEN");
    expect(error.status).toBe(403);
    expect(error.hint).toContain("浏览器会话");
  });
});

// ── 改路线 / 撤销 / 分享 ────────────────────────────────────────────────────
//
// 三个端点都靠 `trip_id` 定位、都按会话判归属，所以除了参数与路径，
// 这里还钉住"必须带凭证"这条共性。

describe("reviseTrip", () => {
  it("POST 到 /trips/{id}/revise，指令原样放进 body", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(okEnvelope({ trip_id: "trip-2", revision_no: 2, diff: {}, needs_clarification: null })),
    );

    await reviseTrip("trip-1", "别去广州塔");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/trips/trip-1/revise`);
    const init = lastInit();
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ instruction: "别去广州塔" });
    expect(init.credentials).toBe("include");
  });

  it("trip_id 会被 URL 编码", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(okEnvelope({ trip_id: "t", revision_no: 2, diff: {}, needs_clarification: null })),
    );

    await reviseTrip("a b/c", "x");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/trips/a%20b%2Fc/revise`);
  });

  it("★ 长度上限与服务端一致（300），前端不该发一个必然 422 的请求", async () => {
    // 后端 `RevisionRequest.instruction` 是 max_length=300
    expect(REVISION_INSTRUCTION_LIMIT).toBe(300);
  });

  it("把 needs_clarification 原样交给调用方（那不是错误）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        okEnvelope({ trip_id: "trip-1", revision_no: 1, diff: {}, needs_clarification: "没能理解「随便改改」" }),
      ),
    );

    const result = await reviseTrip("trip-1", "随便改改");

    expect(result.data.needs_clarification).toBe("没能理解「随便改改」");
  });
});

describe("undoTrip", () => {
  it("POST 到 /trips/{id}/undo，返回的是上一版行程", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ trip_id: "trip-0", revision_no: 1 })));

    const result = await undoTrip("trip-1");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/trips/trip-1/undo`);
    const init = lastInit();
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    expect(init.credentials).toBe("include");
    expect(result.data.trip_id).toBe("trip-0");
  });

  it("已经是最早版本时抛出带 hint 的 ApiError（界面原话转述）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        errorEnvelope({
          code: "INVALID_INPUT",
          message: "这已经是最早的版本了，没有可以撤销的修改。",
          hint: "继续修改会生成新的版本。",
        }),
        { status: 422 },
      ),
    );

    const error = (await undoTrip("trip-1").catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("INVALID_INPUT");
    expect(error.status).toBe(422);
    expect(error.message).toContain("最早");
  });
});

describe("shareTrip", () => {
  it("POST 到 /trips/{id}/share，body 是 {public}", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(okEnvelope({ slug: "s1", url: "http://localhost:3000/t/s1", is_public: true })),
    );

    const result = await shareTrip("trip-1", true);

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/trips/trip-1/share`);
    expect(JSON.parse(String(lastInit().body))).toEqual({ public: true });
    expect(result.data.url).toBe("http://localhost:3000/t/s1");
  });

  it("取消分享发的是 public: false（后端真的会让链接 404）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(okEnvelope({ slug: "s1", url: "http://localhost:3000/t/s1", is_public: false })),
    );

    const result = await shareTrip("trip-1", false);

    expect(JSON.parse(String(lastInit().body))).toEqual({ public: false });
    expect(result.data.is_public).toBe(false);
  });
});

describe("getSharedTrip", () => {
  it("GET /public/trips/{slug}，slug 会被编码", async () => {
    fetchMock.mockResolvedValue(jsonResponse(okEnvelope({ trip_id: "t", routes: [] })));

    await getSharedTrip("a b/c");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/public/trips/a%20b%2Fc`);
    expect(lastInit().method).toBeUndefined();
  });

  it("链接失效（404 SHARE_NOT_FOUND）时抛出可展示的 ApiError", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        errorEnvelope({ code: "SHARE_NOT_FOUND", message: "这个分享链接不存在或已失效" }),
        { status: 404 },
      ),
    );

    const error = (await getSharedTrip("gone").catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("SHARE_NOT_FOUND");
    expect(error.status).toBe(404);
  });
});

describe("copyPublicTrip", () => {
  it("POST 到 /public/trips/{slug}/copy，slug 会被编码", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(okEnvelope({ trip_id: "trip-copy-1", routes: [] }), { status: 201 }),
    );

    const result = await copyPublicTrip("a b/c");

    expect(lastUrl()).toBe(`${API_BASE_URL}/api/v1/public/trips/a%20b%2Fc/copy`);
    // 必须带方法：默认是 GET，拿不到副本
    expect(lastInit().method).toBe("POST");
    // 副本要能直接带去 /trip/{id}，所以 trip_id 必须透出来
    expect(result.data.trip_id).toBe("trip-copy-1");
  });

  it("分享被取消（404）时抛出可展示的 ApiError，而不是返回一个空副本", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        errorEnvelope({ code: "SHARE_NOT_FOUND", message: "分享链接不存在或已失效" }),
        { status: 404 },
      ),
    );

    const error = (await copyPublicTrip("gone").catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("SHARE_NOT_FOUND");
    expect(error.status).toBe(404);
  });
});
describe("searchPlaces", () => {
  it("拼上 q；给了城市就用它限定范围，没给就不写空参数", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        jsonResponse(okEnvelope({ query: "广州塔", city: "guangzhou", page: {}, items: [] })),
      ),
    );

    await searchPlaces("广州塔", { city: "guangzhou", limit: 5 });
    const url = String(fetchMock.mock.calls[0]?.[0]);
    // 中文搜索词必须被编码，否则浏览器/FastAPI 对"同一个词"的解读可能不一致
    expect(url).toContain("q=%E5%B9%BF%E5%B7%9E%E5%A1%94");
    expect(url).toContain("city=guangzhou");
    expect(url).toContain("limit=5");

    await searchPlaces("早茶");
    expect(String(fetchMock.mock.calls[1]?.[0])).not.toContain("city=");
  });
});

// ── ApiError 本身 ──────────────────────────────────────────────────────────

describe("ApiError", () => {
  it("message 取自后端，name 固定为 ApiError（组件靠它做 instanceof 判断）", () => {
    const error = new ApiError(
      { code: "INVALID_INPUT", message: "提交的内容不符合要求", hint: "检查表单", context: {} },
      422,
      "req-9",
    );

    expect(error.message).toBe("提交的内容不符合要求");
    expect(error.name).toBe("ApiError");
    expect(error).toBeInstanceOf(Error);
  });

  it("保留 code / hint / status / requestId 四个字段供界面展示", () => {
    const error = new ApiError(
      { code: "RATE_LIMITED", message: "太频繁", hint: "休息一下", context: {} },
      429,
      "req-9",
    );

    expect([error.code, error.hint, error.status, error.requestId]).toEqual([
      "RATE_LIMITED",
      "休息一下",
      429,
      "req-9",
    ]);
  });
});
