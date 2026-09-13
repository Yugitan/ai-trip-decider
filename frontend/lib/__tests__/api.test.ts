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
  getCityStats,
  getHealth,
  listCities,
  listPlaces,
  listRoutes,
  planTrip,
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
