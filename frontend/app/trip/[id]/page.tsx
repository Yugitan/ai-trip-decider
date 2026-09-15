import type { Metadata } from "next";
import Link from "next/link";

import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { TripWorkspace } from "@/components/trip-workspace";

/**
 * 行程自己的页面 `/trip/{id}`（PRD §7.1 的路由表里那一行）。
 *
 * ★ 为什么要有它 ★
 * 在此之前，行程只渲染在首页表单下方：**刷新就回到空白表单**，收藏与复制地址
 * 都留不下东西 —— 而"这一份行程"其实是后端的真实对象（有自己的 trip_id、版本链、
 * 分享状态），它的地址不应该只存在于一次性的页面状态里。
 *
 * ★ 与分享页 `/t/{slug}` 的分工（两者不是同一个东西的两个壳）★
 * - `/trip/{id}`：**只有创建它的浏览器**能打开（后端按会话判定归属，别人的浏览器 403），
 *   带改路线 / 撤销 / 分享 —— 这是"你自己的行程"；
 * - `/t/{slug}`：免登录、只读，给别人看的那一份。
 * 页面上把这条边界写出来，而不是让用户自己去撞 403。
 *
 * ★ 为什么这一页是「外壳 + 客户端取数」★
 * `GET /trips/{id}` 依赖浏览器里的游客会话 cookie（`credentials: "include"`），
 * 服务端渲染拿不到它 —— 硬要在服务端取，要么必须手把手转发 cookie，要么就得放弃鉴权，
 * 两者都比"让客户端取"更糟。所以服务端这部分只负责标题、说明与排版，
 * 行程由 `TripWorkspace`（客户端组件）在浏览器里取回来。
 */

export const metadata: Metadata = {
  title: "我的行程 · 可以继续改",
  description:
    "上一版规划出来的行程：站点顺序、停留时间、站间交通、预算与不确定性，都可以在这里继续修改。",
};

const SHELL = "mx-auto w-full max-w-4xl px-5 sm:px-8";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function TripPage({ params }: PageProps) {
  const { id } = await params;

  return (
    <>
      <SiteHeader />
      <main className={`${SHELL} pt-12 pb-20`}>
        <p className="text-xs tracking-[0.14em] text-ink-soft uppercase">你自己的行程</p>
        <h1 className="mt-4 text-3xl leading-[1.1] tracking-[-0.01em] text-ink sm:text-4xl">
          这一版随时可以回来继续改
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-ink-soft">
          地址里的这一版就是它的固定坐标：刷新、收藏、关掉再打开都停在它上面。
          改路线与撤销会生成
          <span className="font-medium text-ink">新的一版</span>
          （每条历史版本都保留，地址会跟着换成新的那一条）。
        </p>
        <p className="mt-2 max-w-2xl text-xs leading-relaxed text-ink-faint">
          这个地址只在你这台浏览器里能打开 —— 行程按游客会话隔离，不需要登录。
          想给别人看，用页面里的「分享这套路线」生成公开链接。
        </p>

        <div className="mt-8">
          <TripWorkspace tripId={id} mode="page" />
        </div>

        <p className="mt-8 text-xs leading-relaxed text-ink-faint">
          找不到自己刚才那份行程？回
          <Link href="/#planner" className="mx-1 text-teal-dark underline-offset-2 hover:underline">
            首页重新规划
          </Link>
          一次即可（命中缓存时不会重复计费）。
        </p>
      </main>
      <SiteFooter />
    </>
  );
}
