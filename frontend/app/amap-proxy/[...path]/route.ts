/**
 * 高德 JS API 的同源代理 —— 安全密钥**只在这一层出现**。
 *
 * 浏览器里的 SDK 会去请求 `<origin>/_AMapService/...`（由 `lib/amap.ts` 设的
 * `serviceHost` 决定），`next.config.ts` 再把它转到这个路由。
 * 密钥从 `AMAP_SECURITY_CODE` 读（**没有 `NEXT_PUBLIC_` 前缀 ⇒ Next 不会把它内联进产物**），
 * 追加到上游 query 上转发出去，浏览器全程看不到它。
 *
 * 为什么值得多这一跳：官方文档说得很直白 ——
 * 明文方式「不建议在生产环境使用」，代理转发「强烈建议使用（安全）」。
 * 而本项目的铁律是「任何 Key 只存在于后端环境变量，禁止进入前端产物」：
 * JS API Key 是这条铁律的**已知例外**（它在浏览器脚本 URL 里，绕不过去），
 * 那就至少别让安全密钥也变成第二个例外。
 *
 * 只导出 GET：其余方法由 Next 自己回 405，不需要我们编一套。
 * 上游的错误（如 `INVALID_USER_KEY` / `USERKEY_PLAT_NOMATCH`）**原样透传** ——
 * 高德的错误码是排障时的第一手信息，改写它只会让人查不到问题。
 */

import { buildUpstreamUrl } from "@/lib/amap-proxy";

/** 代理必须每次都真转发：不能进 Next 的缓存，否则密钥换了/水位变了还拿旧的。 */
export const dynamic = "force-dynamic";

/** 上游连不上时的原话。 */
const UPSTREAM_UNREACHABLE = "无法连接高德接口（上游请求失败）";

export async function GET(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const securityCode = process.env.AMAP_SECURITY_CODE?.trim();
  if (!securityCode) {
    // 缺密钥不是「静默降级」的场景：代理存在的唯一目的就是补它，
    // 不给调用方一个看起来成功、实际没有密钥的响应。
    return Response.json(
      {
        error:
          "服务端未配置 AMAP_SECURITY_CODE（写在 frontend/.env.local，不要加 NEXT_PUBLIC_ 前缀）",
      },
      { status: 503 },
    );
  }

  const { path } = await context.params;
  const search = new URL(request.url).search;
  const upstream = buildUpstreamUrl(path ?? [], search, securityCode);

  let response: Response;
  try {
    response = await fetch(upstream, {
      method: "GET",
      cache: "no-store",
      headers: { Accept: "application/json, text/plain, */*" },
    });
  } catch {
    return Response.json({ error: UPSTREAM_UNREACHABLE }, { status: 502 });
  }

  // 状态码与 Content-Type 原样透传：SDK 靠它们判断成败，排障的人靠它们看高德的原话。
  return new Response(response.body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-store",
    },
  });
}
