import type { Metadata } from "next";
import Link from "next/link";

import { AdminCostDashboard } from "@/components/admin-cost-dashboard";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";

/**
 * 成本后台（PRD §7.1 路由表的 `/admin/cost`，FR-12 AC-12.2）。
 *
 * ★ 它和 `/dev` 面板是两件事 ★
 * `/dev` 是**开发期**的配置面板（只在开发环境注册接口），
 * 而成本是**产品的一部分**：它在生产上也需要被看到，所以这一页在任何环境都存在，
 * 靠 `ADMIN_TOKEN` 保护（没设 Token 时后端只接受本机来源）。
 *
 * 页面本身是**服务端渲染的壳**（标题与说明），数据在客户端取：
 * Token 存在浏览器里，服务端读不到它 —— 硬要在服务端取就得把 Token 送到服务器，
 * 那正好和\"Token 只属于操作者\"这件事相反。
 */

export const metadata: Metadata = {
  title: "成本后台",
  description: "规划成本、缓存命中与熔断阈值（Token 保护）。",
  // 账单页面不该出现在搜索结果里
  robots: { index: false, follow: false },
};

export default function AdminCostPage() {
  return (
    <div className="min-h-dvh bg-sand">
      <SiteHeader current={null} />

      <main className="mx-auto w-full max-w-5xl px-5 pt-10 pb-24 sm:px-8">
        <Link
          href="/"
          className="inline-flex min-h-11 items-center text-sm text-teal-dark transition-colors duration-300 hover:text-ink"
        >
          ← 回到规划
        </Link>

        <p className="mt-4 text-xs tracking-[0.14em] text-ink-soft uppercase">Token 保护</p>
        <h1 className="mt-3 text-3xl leading-[1.1] tracking-[-0.01em] text-ink sm:text-4xl">
          成本后台
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-ink-soft">
          单次规划成本、缓存命中率、按类别与 Provider 的明细，以及真正在生效的熔断阈值。
          <span className="font-medium text-ink">未校准的单价会被显式标注</span>
          —— 那时的金额不代表真实支出。
        </p>
        <p className="mt-2 max-w-2xl text-xs leading-relaxed text-ink-faint">
          同一份数据也可以用 <code className="text-ink">make cost</code> 生成
          （<code className="text-ink">docs/COST_REPORT.md</code>）。
        </p>

        <div className="mt-8">
          <AdminCostDashboard />
        </div>
      </main>

      <SiteFooter />
    </div>
  );
}
