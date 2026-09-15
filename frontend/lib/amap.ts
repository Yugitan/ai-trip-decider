/**
 * 高德地图 JS API 的装载器。
 *
 * ★ 安全密钥不进前端产物 ★
 * 高德的 Key 天生是浏览器端的（它必须出现在
 * `<script src="https://webapi.amap.com/maps?v=2.0&key=...">` 里），这一点无法回避；
 * 但**安全密钥可以不给浏览器**：在脚本加载之前设
 * `window._AMapSecurityConfig = { serviceHost: "<同源>/_AMapService" }`，
 * 由我们自己的 Next 路由把 `/_AMapService/*` 转发到高德并在转发时补上 `jscode`。
 * 于是 `AMAP_SECURITY_CODE` 只存在于服务端（见 `app/amap-proxy/[...path]/route.ts`），
 * 前端产物里一个字符都没有（有测试钉住）。
 *
 * 官方文档把这条路称为「强烈建议使用（安全）」，另一条（把 securityJsCode 明文写在页面里）
 * 标注「不建议在生产环境使用」。见
 * https://lbs.amap.com/api/javascript-api-v2/guide/abc/jscode
 *
 * ★ 为什么自己注入 <script> 而不装 `@amap/amap-jsapi-loader` ★
 * 我们只需要「设配置 → 插脚本 → 等 AMap 出现」这三步，
 * 现成的 loader 会额外带来一个 npm 依赖、一份版本升级面，以及它自己的加载策略。
 * 这段逻辑本身很短，而且重点是**顺序**（配置必须先于脚本）——
 * 顺序靠 twenty 行能一眼看懂，靠依赖反而看不出来。
 *
 * ★ 这里不做缓存/重试的聪明事 ★
 * 脚本只插一次（`AMAP_SCRIPT_ID` 幂等）；加载失败**不缓存失败结果**，
 * 所以用户点「重试」能真的再试一次，而不是永远看到同一条失败。
 */

/** 注入的 <script> 的 id：既用于幂等，也用于测试里检查「只插了一次」。 */
export const AMAP_SCRIPT_ID = "amap-jsapi-script";

/** 同源代理前缀。与 `next.config.ts` 的 rewrite、`app/amap-proxy` 路由三处必须一致。 */
export const AMAP_SERVICE_PATH = "/_AMapService";

/** 高德 JS API 版本。2.0 起才有 `_AMapSecurityConfig` 这套机制。 */
export const AMAP_API_VERSION = "2.0";

/** 只用到的那些高德对象。刻意最小化：不把整个 SDK 的类型搬进来。 */
export interface AmapLngLat {
  getLng(): number;
  getLat(): number;
}

export interface AmapOverlay {
  setMap(map: AmapMap | null): void;
}

export interface AmapMap {
  add(overlays: AmapOverlay[]): void;
  setFitView(overlays?: AmapOverlay[], immediately?: boolean): void;
  destroy(): void;
}

export interface AmapNamespace {
  Map: new (
    container: HTMLElement | string,
    options?: Record<string, unknown>,
  ) => AmapMap;
  Marker: new (options: Record<string, unknown>) => AmapOverlay;
  Polyline: new (options: Record<string, unknown>) => AmapOverlay;
  LngLat: new (longitude: number, latitude: number) => AmapLngLat;
}

declare global {
  interface Window {
    AMap?: AmapNamespace;
    _AMapSecurityConfig?: { serviceHost?: string; securityJsCode?: string };
  }
}

/** 装载结果。失败时给一句人话原因，由界面如实显示，而不是抛异常让 React 崩掉。 */
export type AmapLoadResult =
  | { ok: true; amap: AmapNamespace }
  | { ok: false; reason: string };

/**
 * 配置里的 JS API Key。空串按「未配置」处理。
 *
 * 与 `hero-video.tsx` 同一约定：`.env.local` 里写 `NEXT_PUBLIC_AMAP_JS_KEY=` 很常见，
 * 直接取值会得到一个空串 Key，脚本会加载出一个「无 key」的错误页面。
 */
export function amapJsKey(raw: string | undefined = process.env.NEXT_PUBLIC_AMAP_JS_KEY): string | null {
  const trimmed = raw?.trim();
  return trimmed !== undefined && trimmed.length > 0 ? trimmed : null;
}

/** 高德 SDK 的脚本地址（Key 在这里，这是浏览器端无法避免的一处）。 */
export function amapSdkUrl(key: string): string {
  return `https://webapi.amap.com/maps?v=${AMAP_API_VERSION}&key=${encodeURIComponent(key)}`;
}

/** 当前页面的同源代理地址：安全密钥由它后面的服务端补，前端看不到。 */
export function amapServiceHost(origin: string): string {
  return `${origin.replace(/\/+$/, "")}${AMAP_SERVICE_PATH}`;
}

/** 同一个页面里只加载一次；失败后清空，允许重试。 */
let pending: Promise<AmapLoadResult> | null = null;

export function loadAmap(): Promise<AmapLoadResult> {
  if (typeof window === "undefined") {
    // 只在 effect 里调用；真走到这里说明用法错了，如实说而不是猜一个结果。
    return Promise.resolve({ ok: false, reason: "地图只能在浏览器里加载" });
  }
  if (window.AMap) return Promise.resolve({ ok: true, amap: window.AMap });

  const key = amapJsKey();
  if (key === null) {
    return Promise.resolve({
      ok: false,
      reason: "未配置 NEXT_PUBLIC_AMAP_JS_KEY",
    });
  }
  if (pending) return pending;

  pending = new Promise<AmapLoadResult>((resolve) => {
    // ★ 顺序：先设安全配置，再插脚本 ★
    // 反过来的话 SDK 已经拿着明文/无密钥的状态初始化了，设置无效（官方文档明确写了这一点）。
    window._AMapSecurityConfig = {
      serviceHost: amapServiceHost(window.location.origin),
    };

    const fail = (reason: string) => {
      pending = null; // 不缓存失败：允许用户重试
      resolve({ ok: false, reason });
    };

    const script = document.createElement("script");
    script.id = AMAP_SCRIPT_ID;
    script.async = true;
    script.src = amapSdkUrl(key);
    script.onload = () => {
      if (window.AMap) {
        resolve({ ok: true, amap: window.AMap });
        return;
      }
      fail("高德脚本已加载，但没有挂上 AMap 对象（Key 可能无效或被域名白名单拒绝）");
    };
    script.onerror = () => {
      fail("地图脚本加载失败（webapi.amap.com 不可达，或被浏览器/扩展拦截）");
    };
    document.head.appendChild(script);
  });

  return pending;
}
