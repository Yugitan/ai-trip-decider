import type { Metadata } from "next";
import Link from "next/link";

import { PlanProgress } from "@/components/plan-progress";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";

/**
 * 生成中页面（PRD §5.1 步骤 3 / §7.1 路由表的 `/plan/[requestId]`，§17.3）。
 *
 * ★ 这一页为什么必须存在 ★
 * 首页把进度**内嵌**在表单下面，于是"这次规划"没有自己的地址：
 * 一刷新就回到空白表单，而此刻后端其实还在跑（或已经跑完）。
 * 有了这个地址，刷新、收藏、把链接发给别人都停在同一次规划上 ——
 * 后端也支持客户端后到（通道被回收时会回查数据库，见 `stream_plan`）。
 *
 * ★ 与首页内嵌流程的关系：同一个订阅器，不是第二份实现 ★
 * 进度逻辑只有一份（`components/plan-progress.tsx` + `lib/plan-stream.ts`），
 * 首页那一份只是把它嵌在表单下方。两处订阅的是同一个 `stream_url`。
 *
 * `requestId` 不在这里做 uuid 校验：非法 id 会让后端返回 `TRIP_NOT_FOUND`，
 * 而事件流那一层的错误处理已经能把这件事说成一句人话 ——
 * 在这里再写一遍校验，等于同一件事有两个判定点。
 */

export const metadata: Metadata = {
  title: "正在规划",
  // 一次性的生成过程没有存档价值，也不该被搜索引擎抓到
  robots: { index: false, follow: false },
};

export default async function PlanPage({
  params,
}: {
  params: Promise<{ requestId: string }>;
}) {
  const { requestId } = await params;

  return (
    <div className="min-h-dvh bg-sand">
      <SiteHeader current={null} />

      <main className="mx-auto w-full max-w-3xl px-5 pt-10 pb-24 sm:px-8">
        <Link
          href="/#planner"
          className="inline-flex min-h-11 items-center text-sm text-teal-dark transition-colors duration-300 hover:text-ink"
        >
          ← 回到规划
        </Link>

        <h1 className="mt-4 text-3xl leading-[1.1] tracking-[-0.01em] text-ink sm:text-4xl">
          正在生成路线方案
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-soft">
          四个阶段都是后端真实进度：理解偏好 → 筛选候选 → 校验可行性 → 生成方案。
          中途关掉页面也不会丢，回到这个地址就能接着看。
        </p>

        <div className="mt-6">
          <PlanProgress requestId={requestId} />
        </div>
      </main>

      <SiteFooter />
    </div>
  );
}
