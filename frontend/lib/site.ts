/**
 * 站点级 URL 的唯一出处。
 *
 * 为什么单独一个文件：`metadataBase`、OG 图的绝对地址、JSON-LD 里的 `url`
 * 都需要「这个站部署在哪儿」这一个答案 —— 答案如果散在三处，早晚会漂移
 * （同 R14 / #41 的思路：**没有约束的两份事实一定会不一致**）。
 *
 * 环境变量用 `SITE_URL` 而不是 `NEXT_PUBLIC_*`：它只被**服务端**的
 * metadata / OG 路由读取，不进浏览器产物，所以不该占用浏览器可见名单
 * （那份名单是钉死的，加一个都要过审查 —— 见 `lib/__tests__/env-example.test.ts`）。
 * 本机取不到时落到 `localhost:3000`：OG 图路由在同一进程内渲染，
 * 本机开发时预览器拿相对地址也能拼出可用的 URL。
 */
export function siteUrl(): string {
  return (process.env.SITE_URL ?? "http://localhost:3000").replace(/\/+$/, "");
}

/** OG / JSON-LD 共用的站点名（与根布局 `openGraph` 保持一致）。 */
export const SITE_NAME = "TripDecider";
