import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * 高德装载器的测试。
 *
 * 这个文件最要紧的一条是**顺序**：`window._AMapSecurityConfig` 必须在 <script> 被插入
 * 之前设好。官方文档写得很明确：「你这个设置必须是在 JS API 脚本加载之前进行设置，
 * 否则设置无效」—— 而顺序错了的后果是"地图能显示但请求不走我们的代理"，
 * 于是安全密钥被当成明文配置失效，页面看起来一切正常。
 * 所以下面用「记录 appendChild 那一刻的 _AMapSecurityConfig」来钉住它。
 */

/** 每个用例都拿一份全新的模块实例：模块内的 `pending` 是跨用例共享的状态。 */
async function freshAmap() {
  vi.resetModules();
  return await import("@/lib/amap");
}

/** 假的 AMap 命名空间：装载器只检查它是否存在。 */
function fakeAmap() {
  return { Map: class {}, Marker: class {}, Polyline: class {}, LngLat: class {} };
}

function injectedScripts(): HTMLScriptElement[] {
  return Array.from(
    document.querySelectorAll<HTMLScriptElement>("script"),
  ).filter((element) => element.id === "amap-jsapi-script" || element.src.includes("webapi.amap.com"));
}

/**
 * 取第 `index` 份注入的脚本。
 *
 * 不用 `injectedScripts()[0]` 直接下标：tsconfig 开了 `noUncheckedIndexedAccess`，
 * 下标结果是 `T | undefined`，而"脚本没插进来"正是这个测试要报的错 ——
 * 与其用 `!` 把类型系统按下去，不如在这里把缺失变成一个**说人话的失败**。
 */
function scriptAt(index: number): HTMLScriptElement {
  const script = injectedScripts()[index];
  if (!script) {
    throw new Error(`没有任何注入的脚本（期望第 ${index} 份存在）`);
  }
  return script;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  delete window.AMap;
  delete window._AMapSecurityConfig;
  document.head.innerHTML = "";
});

describe("amapJsKey", () => {
  it("未配置 / 空串 / 只有空白都算「没配」", async () => {
    const { amapJsKey } = await freshAmap();
    expect(amapJsKey(undefined)).toBeNull();
    expect(amapJsKey("")).toBeNull();
    expect(amapJsKey("   ")).toBeNull();
  });

  it("去掉首尾空白（.env 里手抖打空格很常见）", async () => {
    const { amapJsKey } = await freshAmap();
    expect(amapJsKey("  abc123  ")).toBe("abc123");
  });
});

describe("amapSdkUrl", () => {
  it("带上版本号，并对 Key 做编码", async () => {
    const { amapSdkUrl } = await freshAmap();
    const url = amapSdkUrl("a b/&c");
    expect(url.startsWith("https://webapi.amap.com/maps?v=2.0&key=")).toBe(true);
    expect(url).toContain("a%20b%2F%26c");
  });
});

describe("amapServiceHost", () => {
  it("拼成同源代理地址，且不出现双斜杠", async () => {
    const { amapServiceHost } = await freshAmap();
    expect(amapServiceHost("http://localhost:3000")).toBe(
      "http://localhost:3000/_AMapService",
    );
    expect(amapServiceHost("http://localhost:3000/")).toBe(
      "http://localhost:3000/_AMapService",
    );
  });
});

describe("loadAmap", () => {
  it("没配 Key 时直接给出原因，不插任何脚本", async () => {
    const { loadAmap } = await freshAmap();
    vi.stubEnv("NEXT_PUBLIC_AMAP_JS_KEY", "");
    const result = await loadAmap();
    expect(result).toEqual({
      ok: false,
      reason: "未配置 NEXT_PUBLIC_AMAP_JS_KEY",
    });
    expect(injectedScripts()).toHaveLength(0);
  });

  it("★ 安全配置先于脚本：appendChild 那一刻 serviceHost 已经设好 ★", async () => {
    const { loadAmap } = await freshAmap();
    vi.stubEnv("NEXT_PUBLIC_AMAP_JS_KEY", "test-key");

    let serviceHostWhenAppended: string | undefined;
    const original = document.head.appendChild.bind(document.head);
    const spy = vi
      .spyOn(document.head, "appendChild")
      .mockImplementation((node: Node) => {
        // 记下「插入脚本的那一瞬间」配置长什么样 —— 这才是真正要断言的顺序
        serviceHostWhenAppended = window._AMapSecurityConfig?.serviceHost;
        return original(node);
      });

    const pending = loadAmap();
    expect(serviceHostWhenAppended).toBe(
      `${window.location.origin}/_AMapService`,
    );
    // 走代理时**不能**同时给明文 securityJsCode（那等于把密钥放回了浏览器）
    expect(window._AMapSecurityConfig).toEqual({
      serviceHost: `${window.location.origin}/_AMapService`,
    });
    spy.mockRestore();

    const script = scriptAt(0);
    expect(script.src).toContain("key=test-key");
    expect(script.async).toBe(true);

    window.AMap = fakeAmap() as never;
    script.dispatchEvent(new Event("load"));
    const result = await pending;
    expect(result.ok).toBe(true);
  });

  it("脚本已加载但没有 AMap 对象 → 如实说出「Key 可能无效」", async () => {
    const { loadAmap } = await freshAmap();
    vi.stubEnv("NEXT_PUBLIC_AMAP_JS_KEY", "test-key");
    const pending = loadAmap();
    scriptAt(0).dispatchEvent(new Event("load"));
    const result = await pending;
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toContain("没有挂上 AMap");
  });

  it("脚本加载失败 → 说出原因，且**不缓存失败**（重试能真的再试）", async () => {
    const { loadAmap } = await freshAmap();
    vi.stubEnv("NEXT_PUBLIC_AMAP_JS_KEY", "test-key");

    const first = loadAmap();
    scriptAt(0).dispatchEvent(new Event("error"));
    const failed = await first;
    expect(failed.ok).toBe(false);
    if (!failed.ok) expect(failed.reason).toContain("webapi.amap.com");

    // 第二次调用必须重新插一份脚本，而不是把失败结论一直记着
    const second = loadAmap();
    expect(injectedScripts()).toHaveLength(2);
    window.AMap = fakeAmap() as never;
    scriptAt(1).dispatchEvent(new Event("load"));
    expect((await second).ok).toBe(true);
  });

  it("并发调用只插一次脚本，且共享同一个结果", async () => {
    const { loadAmap } = await freshAmap();
    vi.stubEnv("NEXT_PUBLIC_AMAP_JS_KEY", "test-key");

    const [a, b] = [loadAmap(), loadAmap()];
    expect(injectedScripts()).toHaveLength(1);
    window.AMap = fakeAmap() as never;
    scriptAt(0).dispatchEvent(new Event("load"));
    expect(await a).toEqual(await b);
  });

  it("页面里已经有 AMap 时不再插脚本（第二次挂地图的情形）", async () => {
    const { loadAmap } = await freshAmap();
    const existing = fakeAmap();
    window.AMap = existing as never;
    const result = await loadAmap();
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.amap).toBe(existing);
    expect(injectedScripts()).toHaveLength(0);
  });

  it("没有 window（服务端）时不猜结果，直接说明只能在浏览器里加载", async () => {
    const { loadAmap } = await freshAmap();
    vi.stubGlobal("window", undefined);
    const result = await loadAmap();
    expect(result).toEqual({ ok: false, reason: "地图只能在浏览器里加载" });
  });
});
