import type { Metadata } from "next";
import Link from "next/link";

import { ApiError, getSharedTrip, type TripOut } from "@/lib/api";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { TripResultView } from "@/components/trip-result";

/**
 * 公开分享页 `GET /api/v1/public/trips/{slug}`（PRD FR-09 / AC-9.3）。
 *
 * ★ 为什么这一页必须存在 ★
 * 后端的 `POST /trips/{id}/share` 返回的是 `{frontend_url}/t/{slug}`。
 * 在它存在之前，那个链接是一个**死链** —— 界面上"生成分享链接"能成功，
 * 但点开是 404。分享功能的价值全在"别人能打开"，所以这一页不是锦上添花。
 *
 * 三个刻意的选择：
 * 1. **服务端渲染**：拿的是 `getSharedTrip`（无鉴权、只返回已公开的行程），
 *    所以任何浏览器打开都能看到内容，不依赖 cookie —— 这正是它与 `/trips/{id}` 的区别。
 * 2. **`force-dynamic`**：取消分享必须**立刻**失效（AC-9.7）。
 *    一旦静态化/缓存，取消之后链接还会继续可用一段时间 —— 那就等于骗人。
 * 3. **只读**：没有修改/撤销/分享按钮。复制成自己的一份（AC-9.4）还没接，
 *    这一页不提供任何看起来能改的入口。
 */

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ slug: string }>;
}

const SHELL = "mx-auto w-full max-w-4xl px-5 sm:px-8";

/**
 * 标题里带上行程名，分享出去才像样（PRD §21 提到了 OG 图，尚未实现）。
 * 代价是多一次请求：`request()` 用 `cache: "no-store"`，Next 不会替我们合并这两次。
 * 换来的是一条有意义的标题 —— 分享链接的预览文本就是这个。
 */
export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { slug } = await params;
  try {
    const { data } = await getSharedTrip(slug);
    return {
      title: data.title ? `${data.title} · 分享的路线` : "分享的路线",
      description: `${data.days} 天 · ${data.route_count} 套方案（广州）`,
    };
  } catch {
    return { title: "这个分享链接不可用" };
  }
}

export default async function SharedTripPage({ params }: PageProps) {
  const { slug } = await params;

  let trip: TripOut | null = null;
  let failure: { title: string; message: string; hint: string | null } | null = null;

  try {
    trip = (await getSharedTrip(slug)).data;
  } catch (error: unknown) {
    // 404 / SHARE_NOT_FOUND 是最常见的情况（取消分享、链接写错、被清理过）。
    // 刻意**不**把它渲染成"出错了"：对读者来说这是"这个链接没了"，不是系统故障。
    if (error instanceof ApiError) {
      failure = {
        title: "这个分享链接打不开了",
        message: error.message,
        hint: error.hint === "" ? null : error.hint,
      };
    } else {
      failure = {
        title: "没能读到这份分享的行程",
        message: "暂时连不上规划服务。",
        hint: "稍后刷新重试；如果一直打不开，可能是链接已经失效。",
      };
    }
  }

  return (
    <>
      <SiteHeader />
      <main className={`${SHELL} pt-12 pb-20`}>
        <p className="text-xs tracking-[0.14em] text-ink-soft uppercase">别人分享的行程</p>
        <h1 className="mt-4 text-3xl leading-[1.1] tracking-[-0.01em] text-ink sm:text-4xl">
          这份路线是别人规划好的
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-ink-soft">
          下面是原样的行程快照：站点顺序、停留时间、站间交通与预算，以及它带着的不确定性。
          分享者取消分享后，这个链接会立刻失效。
        </p>

        <div className="mt-8">
          {trip === null ? (
            <div
              data-testid="share-unavailable"
              className="rounded-card border border-coral/25 bg-coral-tint/50 p-6"
            >
              <h2 className="text-lg text-ink">{failure?.title ?? "这个分享链接不可用"}</h2>
              <p className="mt-2 text-sm text-ink-soft">{failure?.message}</p>
              {failure?.hint ? (
                <p className="mt-1 text-xs text-ink-faint">{failure.hint}</p>
              ) : null}
              <p className="mt-4 text-xs text-ink-faint">
                这一页不显示任何占位行程 —— 读不到就说读不到。
              </p>
            </div>
          ) : (
            <TripResultView trip={trip} />
          )}
        </div>

        <div className="mt-10 rounded-card border border-line bg-shell/60 p-6">
          <h2 className="text-lg text-ink">也想让别人给你排一条？</h2>
          <p className="mt-2 text-sm leading-relaxed text-ink-soft">
            填一份需求（有默认值，不填也能提交），拿到 A/B/C 三套取舍不同的广州路线。
          </p>
          <Link
            href="/#planner"
            className="mt-4 inline-flex min-h-11 items-center justify-center rounded-full bg-ink px-6 text-sm text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100"
          >
            我也要规划一次
          </Link>
        </div>
      </main>
      <SiteFooter />
    </>
  );
}
