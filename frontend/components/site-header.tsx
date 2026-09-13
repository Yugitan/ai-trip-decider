import Link from "next/link";

/**
 * 全站头部（服务端组件，无状态、无网络请求）。
 *
 * 视觉：沙色玻璃（`bg-sand/75 + backdrop-blur`）+ 发丝线底边 —— 内容滚动到下面时
 * 头部仍然可读，但不抢视觉焦点。
 * 字标走展示体（衬线），导航走 UI 体，形成「品牌 vs 界面」的两层字体关系。
 *
 * 当前页由调用方通过 `current` 传入（而不是 `usePathname()`）：
 * 这个组件是服务端组件，且页面测试会直接渲染它 —— 一旦引入 App Router 的
 * 客户端 hook，没有 Router 上下文的环境（单测、`next build` 的静态预渲染路径）就会报错。
 *
 * 响应式：容器允许换行（`flex-wrap`），360px 下导航整体落到第二行，
 * 不会出现横向滚动；主导航 CTA 在手机上隐藏（Hero 里有同样可达的入口）。
 */

export type NavKey = "home" | "explore" | "about" | "dev";

const NAV_ITEMS: ReadonlyArray<{ key: NavKey; href: string; label: string }> = [
  { key: "home", href: "/", label: "首页" },
  { key: "explore", href: "/explore/guangzhou", label: "浏览知识库" },
  { key: "about", href: "/about/data", label: "数据来源" },
];

/**
 * 开发设置入口只在开发构建里渲染。
 *
 * `process.env.NODE_ENV` 在 Next.js 构建时被内联，所以 `next build`
 * （NODE_ENV=production）产出的页面里**根本不会有这段 DOM** —— 不是靠 CSS 藏起来。
 * 用户看不到它，维护者本地 `pnpm dev` 随时一键进 `/dev`。
 */
const SHOW_DEV_ENTRY = process.env.NODE_ENV !== "production";

export function SiteHeader({ current = "home" }: { current?: NavKey }) {
  return (
    <header className="sticky top-0 z-40 border-b border-line/70 bg-sand/75 backdrop-blur-xl">
      <div className="mx-auto flex w-full max-w-7xl flex-wrap items-center justify-between gap-x-3 gap-y-1 px-4 py-2.5 sm:px-8 sm:py-5">
        <Link
          href="/"
          className="inline-flex min-h-11 items-center gap-2.5 text-ink"
          aria-label="TripDecider 首页"
        >
          <span
            aria-hidden="true"
            className="flex size-8 shrink-0 items-center justify-center rounded-[10px] border border-line bg-shell text-teal"
          >
            <svg
              viewBox="0 0 24 24"
              className="size-4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M4 19c4 0 4-6 8-6s4-6 8-6" />
              <circle cx="4" cy="19" r="1.6" />
              <circle cx="20" cy="7" r="1.6" />
            </svg>
          </span>
          <span className="font-display text-[18px] leading-none tracking-tight sm:text-[22px]">
            TripDecider
          </span>
        </Link>

        <div className="flex items-center gap-1 sm:gap-2">
          <nav aria-label="主导航" className="flex items-center sm:gap-0.5">
            {NAV_ITEMS.map((item) => {
              const isCurrent = item.key === current;
              return (
                <Link
                  key={item.key}
                  href={item.href}
                  aria-current={isCurrent ? "page" : undefined}
                  className={`inline-flex min-h-11 items-center rounded-full px-2.5 text-xs transition-colors duration-300 sm:px-3.5 sm:text-sm ${
                    isCurrent
                      ? "text-ink"
                      : "text-ink-soft hover:text-ink"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          {SHOW_DEV_ENTRY ? (
            <Link
              href="/dev"
              aria-current={current === "dev" ? "page" : undefined}
              title="开发期配置面板：仅开发构建可见，生产环境不可用"
              className={`hidden min-h-11 items-center rounded-full border border-dashed px-3 text-xs transition-colors duration-300 sm:inline-flex ${
                current === "dev"
                  ? "border-teal text-teal-dark"
                  : "border-line text-ink-faint hover:border-teal/50 hover:text-teal-dark"
              }`}
            >
              开发设置
            </Link>
          ) : null}

          <Link
            href="/#planner"
            className="hidden min-h-11 items-center rounded-full bg-ink px-5 text-sm text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100 sm:inline-flex"
          >
            开始规划
          </Link>
        </div>
      </div>
    </header>
  );
}
