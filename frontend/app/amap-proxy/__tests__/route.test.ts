import { afterEach, describe, expect, it, vi } from "vitest";

import { GET } from "@/app/amap-proxy/[...path]/route";

/**
 * 代理路由的测试。
 *
 * 这个 handler 是**唯一**接触安全密钥的地方，所以两条底线都要钉住：
 * 1. 密钥必须有值才转发 —— 缺了要报 503，不能"看着成功但没带密钥"；
 * 2. 密钥**不能出现在响应里**，也不能由调用方指定（覆盖逻辑在 `lib/amap-proxy.ts`，
 *    这里验的是它确实被用上了）。
 */

const SECURITY_CODE = "security-code-should-never-leak";

function context(path: string[]) {
  return { params: Promise.resolve({ path }) };
}

function proxyRequest(url: string, method = "GET") {
  return new Request(url, { method });
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("GET /_AMapService/*", () => {
  it("没配安全密钥 → 503 并说清去哪儿配，不转发任何请求", async () => {
    vi.stubEnv("AMAP_SECURITY_CODE", "");
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const response = await GET(
      proxyRequest("http://localhost:3000/_AMapService/v3/ip?key=k"),
      context(["v3", "ip"]),
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error:
        "服务端未配置 AMAP_SECURITY_CODE（写在 frontend/.env.local，不要加 NEXT_PUBLIC_ 前缀）",
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("转发到 restapi，并带上服务端的安全密钥", async () => {
    vi.stubEnv("AMAP_SECURITY_CODE", SECURITY_CODE);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response('{"status":"1"}', {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );

    const response = await GET(
      proxyRequest("http://localhost:3000/_AMapService/v3/ip?key=js-key&jscode=attacker"),
      context(["v3", "ip"]),
    );

    const firstCall = fetchSpy.mock.calls[0];
    if (!firstCall) throw new Error("期望代理真的发了一次上游请求，实际一次都没有");
    const forwarded = new URL(String(firstCall[0]));
    expect(forwarded.origin).toBe("https://restapi.amap.com");
    expect(forwarded.pathname).toBe("/v3/ip");
    expect(forwarded.searchParams.get("key")).toBe("js-key");
    expect(forwarded.searchParams.get("jscode")).toBe(SECURITY_CODE);

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/json");
    // ★ 响应里不能出现密钥（转发它是给高德看的，不是给浏览器的）★
    expect(await response.text()).not.toContain(SECURITY_CODE);
  });

  it("上游连不上 → 502，并把原因说出来（不伪装成 200）", async () => {
    vi.stubEnv("AMAP_SECURITY_CODE", SECURITY_CODE);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("ECONNREFUSED"));
    const response = await GET(
      proxyRequest("http://localhost:3000/_AMapService/v3/ip"),
      context(["v3", "ip"]),
    );
    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: "无法连接高德接口（上游请求失败）",
    });
  });

  it("高德的错误状态码原样透传（排障要看高德的原话）", async () => {
    vi.stubEnv("AMAP_SECURITY_CODE", SECURITY_CODE);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response('{"info":"INVALID_USER_KEY","status":"0"}', {
        status: 403,
        headers: { "content-type": "application/json" },
      }),
    );
    const response = await GET(
      proxyRequest("http://localhost:3000/_AMapService/v3/ip"),
      context(["v3", "ip"]),
    );
    expect(response.status).toBe(403);
    expect(await response.text()).toContain("INVALID_USER_KEY");
  });
});
