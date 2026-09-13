import type { Metadata } from "next";
import Link from "next/link";

import { DegradedBanner } from "@/components/degraded-banner";
import { HeroVideo } from "@/components/hero-video";
import { PlannerForm } from "@/components/planner-form";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { SectionHeading } from "@/components/ui/card";

export const metadata: Metadata = {
  title: "广州路线规划 · 你负责决定怎么玩，路线交给我",
  description:
    "告诉我目的地、天数、人数、偏好、节奏和预算，我给你 2–3 套经过校验、可比价、能继续改的广州路线方案 —— 而不是一篇攻略。",
};

/** 全站统一的栅格：最大宽度 + 两侧留白。所有区块共用，横向对齐才不会歪。 */
const SHELL = "mx-auto w-full max-w-7xl px-5 sm:px-8";

const PLAN_STYLES: ReadonlyArray<{
  label: string;
  name: string;
  description: string;
}> = [
  {
    label: "A",
    name: "轻松休闲",
    description:
      "一天 2–3 个点，把吃饭和发呆的时间留出来，走得少、坐得多。",
  },
  {
    label: "B",
    name: "经典打卡",
    description:
      "第一次来广州：把最有代表性的地标串成一条顺路的线，尽量不折返。",
  },
  {
    label: "C",
    name: "主题型",
    description:
      "围绕一个主题（美食 · 拍照 · 文化 · 夜景…）把一条线走深，不追景点数量。",
  },
];

const STEPS: ReadonlyArray<{ step: string; title: string; detail: string }> = [
  {
    step: "1",
    title: "填需求",
    detail: "默认值已经填好，什么都不改也能提交。",
  },
  {
    step: "2",
    title: "拿方案",
    detail: "同一份需求给你 A / B / C 三套取舍不同的路线。",
  },
  {
    step: "3",
    title: "继续改",
    detail: "换掉一个点、调预算、加一天 —— 方案跟着一起变。",
  },
];

const PROMISES: readonly string[] = [
  "不编造路线：没经过可行性校验的点不会出现在方案里。",
  "不伪造营业时间与价格：拿不到的数据就标注为未知。",
  "不把攻略抄一遍：给你的是顺序、时间、交通和取舍理由。",
];

/**
 * 电影感 Hero。
 *
 * 三层视觉：背景视频（氛围）→ 渐变纱幕（保证文字清晰）→ 内容。
 * 视频被刻意压在导航之下（`top-[180px]` 起）、轻微降饱和。
 *
 * ★ 纱幕的预算必须给足 ★
 * 视频的透明度由 `HeroVideo` 的行内样式逐帧接管（0→1 的循环淡入淡出），
 * 所以这里**不能再靠 class 去压暗它** —— 行内样式优先级更高，写 `opacity-70`
 * 只会看着像生效、实际一点作用都没有。亮度只能从纱幕这一侧省：
 * 上一版中间层用了 `via-sand/40`，再叠上左右渐晕，整幅画面最后只剩六成多，
 * 山脊与树线全糊成一片沙色。现在两端实心、中间一律透明，文字所在的那一小块
 * 交给一枚柔和的径向光晕单独兜底 —— 「导航清楚 / 正文可读 / 留白还在」三件事
 * 各归各的层，不再互相拖累。
 *
 * `prefers-reduced-motion` 的用户只会看到静态的纱幕。
 */
function Hero() {
  return (
    <section className="relative isolate min-h-svh w-full overflow-hidden">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 z-0">
        <HeroVideo className="hero-veil absolute inset-x-0 bottom-0 top-[180px] w-full object-cover sm:top-[220px]" />
        {/* 上下两端的沙色渐变：顶部让导航与标题清楚，底部把画面交还给页面 */}
        <div className="absolute inset-0 bg-gradient-to-b from-sand via-transparent to-sand" />
        {/* 文字所在的一小块柔光：只压中心，四周与下方仍然透出山与树 */}
        <div className="absolute inset-0 bg-[radial-gradient(62%_52%_at_50%_46%,rgba(247,244,239,0.62),transparent_72%)]" />
        {/* 左右渐晕：两侧安静下来，但中间保留景深 */}
        <div className="absolute inset-0 bg-gradient-to-r from-sand/55 via-transparent to-sand/55" />
      </div>

      <div className="relative z-10 mx-auto flex w-full max-w-7xl flex-col items-center justify-center px-5 pt-24 pb-28 text-center sm:px-8 sm:pt-32 sm:pb-40">
        <p className="animate-fade-rise inline-flex items-center gap-2 rounded-full border border-line/80 bg-shell/70 px-4 py-1.5 text-xs text-ink-soft backdrop-blur">
          <span aria-hidden="true" className="size-1.5 rounded-full bg-teal" />
          首个城市 · 广州
        </p>

        <h1 className="animate-fade-rise-delay mt-8 max-w-5xl text-balance text-[2.5rem] leading-[1.05] tracking-[-0.02em] text-ink sm:text-6xl md:text-7xl lg:text-8xl">
          你负责决定怎么玩
          <span className="block text-quiet">路线交给我。</span>
        </h1>

        <p className="animate-fade-rise-delay-2 mt-8 max-w-2xl text-base leading-relaxed text-ink-soft sm:text-lg">
          我替你研究、筛选、组合并验证路线，而不是丢给你一篇攻略。
        </p>

        {/* 信任文案给 ink-soft 而不是 ink-faint：#8a93a8 在沙色底上对比度只有约 2.9:1，
            而这一行承载的是「数据有来源」这个卖点，属于需要被读到的信息。 */}
        <p className="animate-fade-rise-delay-2 tnum mt-6 text-sm text-ink-soft">
          200+ 广州地点 · 30+ 精选路线 · 真实距离校验 · 数据有来源
        </p>

        <div className="animate-fade-rise-delay-2 mt-12 flex w-full flex-col items-center gap-3 sm:w-auto sm:flex-row sm:gap-4">
          <Link
            href="#planner"
            className="inline-flex min-h-12 w-full items-center justify-center rounded-full bg-ink px-10 text-base text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100 sm:w-auto"
          >
            开始规划我的行程
          </Link>
          <Link
            href="/explore/guangzhou"
            className="inline-flex min-h-12 w-full items-center justify-center rounded-full border border-line bg-shell/60 px-8 text-base text-ink-soft backdrop-blur transition-colors duration-300 hover:border-ink/20 hover:text-ink sm:w-auto"
          >
            先看看知识库里有什么
          </Link>
        </div>
      </div>
    </section>
  );
}

export default function HomePage() {
  return (
    <div className="min-h-dvh bg-sand">
      <a
        href="#planner"
        className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:inline-flex focus:min-h-11 focus:items-center focus:rounded-full focus:border focus:border-line focus:bg-shell focus:px-4 focus:text-sm focus:text-ink"
      >
        跳到规划表单
      </a>

      <SiteHeader current="home" />

      <main>
        <Hero />

        {/* ── 规划卡 ─────────────────────────────────────────────────────── */}
        {/*
         * 这一栏刻意**居中**（`mx-auto max-w-4xl`），而不是贴着外壳左边缘：
         * 它紧跟在居中的 Hero 之后，是整页的主体动作。之前左对齐 + `max-w-2xl` 的
         * 状态条压着一块 `max-w-4xl` 的表单，宽窄不一又整体偏左，在 1440px 下
         * 右侧会空出三百多像素 —— 看起来就是「表单歪了」。
         * 状态条与表单现在共用同一条栏宽，两条左边缘对齐。
         *
         * `scroll-mt-32 sm:scroll-mt-28`：跳转时给吸顶头部留出空间。
         * 头部在手机上是两行（≈108px），桌面一行（≈84px）—— 原来 96px 的
         * scroll-margin 会让表单顶部刚好被头部盖住，落到视口上方就像位置偏了。
         */}
        <section
          id="planner"
          aria-label="开始规划"
          className={`${SHELL} scroll-mt-32 pb-20 sm:scroll-mt-28 sm:pb-28`}
        >
          <div className="mx-auto w-full max-w-4xl">
            <DegradedBanner />
            <div className="mt-6">
              <PlannerForm />
            </div>
          </div>
        </section>

        {/* ── 三套方案 ───────────────────────────────────────────────────── */}
        <section
          aria-labelledby="plan-styles-title"
          className={`${SHELL} pb-20 sm:pb-28`}
        >
          <SectionHeading
            id="plan-styles-title"
            eyebrow="同一份需求，三种取舍"
            title="你会拿到 A / B / C 三套方案"
            description="它们不是复制粘贴的三个版本：同样是广州一天，走法可以完全不同。"
          />

          <ol className="mt-12 grid divide-y divide-line border-y border-line md:grid-cols-3 md:divide-x md:divide-y-0">
            {PLAN_STYLES.map((style) => (
              <li
                key={style.label}
                className="py-8 md:px-8 md:first:pl-0 md:last:pr-0"
              >
                <span
                  aria-hidden="true"
                  className="font-display block text-4xl leading-none text-quiet/40"
                >
                  {style.label}
                </span>
                <h3 className="font-display mt-5 text-xl text-ink">
                  {style.name}
                </h3>
                <p className="mt-3 text-sm leading-relaxed text-ink-soft">
                  {style.description}
                </p>
              </li>
            ))}
          </ol>

          <p className="mt-8 max-w-3xl text-xs leading-relaxed text-ink-faint">
            三套方案来自同一份需求，差别只在取舍。评分与可行性校验（M2）与外部能力降级（M3）已经就绪，
            把它们串起来的规划接口会在 M4 里程碑上线 —— 在那之前，这里不会先给你看起来很像真的假行程。
          </p>
        </section>

        {/* ── 怎么用 ─────────────────────────────────────────────────────── */}
        <section
          aria-labelledby="how-title"
          className={`${SHELL} pb-20 sm:pb-28`}
        >
          <SectionHeading
            id="how-title"
            eyebrow="怎么用"
            title="三步拿到能直接走的路线"
          />

          <ol className="mt-12 grid divide-y divide-line border-y border-line sm:grid-cols-3 sm:divide-x sm:divide-y-0">
            {STEPS.map((item) => (
              <li
                key={item.step}
                className="py-8 sm:px-8 sm:first:pl-0 sm:last:pr-0"
              >
                <p className="tnum text-xs tracking-[0.18em] text-teal uppercase">
                  STEP {item.step}
                </p>
                <h3 className="mt-4 text-lg font-medium text-ink">
                  {item.title}
                </h3>
                <p className="mt-2 text-sm leading-relaxed text-ink-soft">
                  {item.detail}
                </p>
              </li>
            ))}
          </ol>
        </section>

        {/* ── 立场 ───────────────────────────────────────────────────────── */}
        <section
          aria-labelledby="promise-title"
          className={`${SHELL} pb-24 sm:pb-32`}
        >
          <div className="border-t border-line pt-12">
            <h2
              id="promise-title"
              className="max-w-3xl text-3xl leading-[1.15] text-ink sm:text-4xl"
            >
              我们不做的事
            </h2>
            <ul className="mt-10 grid gap-8 sm:grid-cols-3 sm:gap-10">
              {PROMISES.map((promise) => (
                <li
                  key={promise}
                  className="border-t border-line pt-5 text-sm leading-relaxed text-ink-soft"
                >
                  {promise}
                </li>
              ))}
            </ul>
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  );
}
