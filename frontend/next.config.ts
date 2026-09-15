import type { NextConfig } from "next";

/**
 * 后端地址。**只在服务端使用**（rewrites 的转发目标）——
 * 刻意不再通过 `env` 内联进前端产物，原因见下面的 `rewrites`。
 *
 * 不读根目录的 `.env`：Next 只加载 `frontend/.env*`，所以这里取不到值时就落到
 * 本地开发的默认地址（后端 `make dev-backend` 监听的正是它）。
 */
const BACKEND_URL = (
  process.env.API_BASE_URL ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,

  /**
   * ★ 浏览器只跟 Next 自己的源说话：`/api/*` 由服务端转发给后端 ★
   *
   * 为什么必须这样，而不是让浏览器直连 `127.0.0.1:8000`：
   * 页面在 `localhost:3000`、接口在 `127.0.0.1:8000`，两者是**跨站**的，
   * 于是后端签发的游客会话 cookie（`td_session`）在浏览器眼里就是**第三方 cookie** ——
   * Chrome 现在的默认策略会直接把它丢掉（实测 `Network.getAllCookies` 为空），
   * 而签名 cookie 一旦存不下来，每个请求都会落到一个全新的会话上：
   * `GET /trips/{id}` 稳定 403「这个行程不属于当前会话」，
   * 用户点完「开始规划」一条路线都看不到。
   *
   * 这不是加不加 `credentials: "include"` 的问题（那个也必须有，见 `lib/api.ts`）：
   * 凭证模式对了，跨站的 Set-Cookie 依然会被丢弃。
   * 走同源代理之后 cookie 是第一方的，附带的好处是**整个 CORS 都不再需要**。
   *
   * 代价：浏览器请求多一跳（本机回环，可忽略）；部署时前端必须由 Next 自己伺服
   * （`next dev` / `next start`），静态导出（`output: "export"`）与这一条不兼容。
   */
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
      {
        /**
         * ★ 高德地图的同源代理入口 ★
         *
         * `/<origin>/_AMapService` 是 **高德规定的固定前缀**（官方文档："_AMapService
         * 为代理请求固定前缀，不可省略或修改"），所以浏览器侧的路径必须长这样；
         * 服务端由 `app/amap-proxy/[...path]/route.ts` 接手，在那里补上安全密钥。
         *
         * 为什么不在 `next.config.ts` 里直接把 query 拼给高德：
         * rewrite 的 destination 是**构建期求值**的静态串，加 `?jscode=...` 就等于把密钥
         * 写进构建产物，而且改密钥必须重新构建。放在路由里则每次请求都从环境变量读。
         *
         * 也不能把路由放在 `app/_AMapService/`：App Router 里 `_` 开头的目录是
         * **私有目录**（不参与路由），建出来的路由根本不会被注册。
         */
        source: "/_AMapService/:path*",
        destination: "/amap-proxy/:path*",
      },
    ];
  },

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
