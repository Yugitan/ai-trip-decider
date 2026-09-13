import Link from "next/link";

/**
 * 全站页脚（服务端组件）。三个页面共用一份，避免各页各写一份而慢慢漂移。
 *
 * 注意：链接名必须与主导航区分开（「数据来源与免责声明」而不是「数据来源」）——
 * 同一个页面里出现两个同名链接，会让屏幕阅读器用户与测试都无法用名字定位。
 */
const FOOTER_LINKS: ReadonlyArray<{ href: string; label: string }> = [
  { href: "/", label: "首页" },
  { href: "/explore/guangzhou", label: "浏览知识库" },
  { href: "/about/data", label: "数据来源与免责声明" },
];

export function SiteFooter() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-8 px-5 py-12 sm:flex-row sm:items-end sm:justify-between sm:px-8">
        <div>
          <p className="font-display text-2xl leading-none tracking-tight text-ink">
            TripDecider
          </p>
          <p className="mt-3 max-w-md text-xs leading-relaxed text-ink-faint">
            先做广州，把路线做对再谈城市数量。地点数据来自 OpenStreetMap（ODbL），
            距离与时长可能为估算值，缺失字段一律标注为未知。
          </p>
        </div>

        <nav aria-label="页脚导航" className="flex flex-wrap gap-x-5 gap-y-1">
          {FOOTER_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="inline-flex min-h-11 items-center text-xs text-ink-faint transition-colors duration-300 hover:text-ink"
            >
              {link.label}
            </Link>
          ))}
        </nav>
      </div>
    </footer>
  );
}
