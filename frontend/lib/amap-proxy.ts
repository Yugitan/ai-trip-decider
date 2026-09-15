/**
 * 高德「代理服务器转发」的纯逻辑部分（可单测；网络那一半在 route handler 里）。
 *
 * 官方要求服务端把两类请求转给两个不同的上游：
 *
 * | 浏览器请求                      | 上游                            | 用途          |
 * | ------------------------------- | ------------------------------- | ------------- |
 * | `/_AMapService/v4/map/styles`   | `https://webapi.amap.com/...`   | 自定义地图样式 |
 * | `/_AMapService/<其余所有路径>`  | `https://restapi.amap.com/...`  | Web 服务 API  |
 *
 * 并在转发时**追加 `jscode=<安全密钥>`**。密钥只在服务端出现，不进前端产物。
 * 见 https://lbs.amap.com/api/javascript-api-v2/guide/abc/jscode
 */

/** 样式接口走 webapi 上游；其余走 restapi 上游。 */
const STYLE_UPSTREAM = "https://webapi.amap.com";
const REST_UPSTREAM = "https://restapi.amap.com";

/** 需要改走上游的路径前缀（官方文档的 nginx 示例里也是单独一条 location）。 */
const STYLE_PATH_PREFIX = "v4/map/styles";

/** 安全密钥在 query 里的参数名（官方固定为 `jscode`）。 */
export const SECURITY_CODE_PARAM = "jscode";

/** 这条路径应该发给哪个上游。 */
export function amapUpstreamFor(pathSegments: readonly string[]): string {
  const path = pathSegments.join("/");
  return path.startsWith(STYLE_PATH_PREFIX) ? STYLE_UPSTREAM : REST_UPSTREAM;
}

/**
 * 拼出上游 URL。
 *
 * ★ 三件必须做对的事 ★
 * 1. `jscode` **强制覆盖**：调用方自带的同名参数一律丢掉 ——
 *    否则任何人只要往 `/_AMapService/...?jscode=xxx` 里塞一个值，
 *    就变成了「由浏览器决定用什么密钥」，代理层也就白设了；
 * 2. 其余查询参数**原样保留**（`key`、坐标、页码……都是高德接口自己要的）；
 * 3. 路径段逐段 `encodeURIComponent`：分段后再编码，斜杠才是路径分隔符，
 *    否则 `a%2Fb` 会被拼成两个路径段（与 `lib/api.ts` 的 URL 构造同一原则）。
 */
export function buildUpstreamUrl(
  pathSegments: readonly string[],
  search: string,
  securityCode: string,
): string {
  const path = pathSegments.map((segment) => encodeURIComponent(segment)).join("/");
  const query = new URLSearchParams(search);
  query.set(SECURITY_CODE_PARAM, securityCode);
  return `${amapUpstreamFor(pathSegments)}/${path}?${query.toString()}`;
}
