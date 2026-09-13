import type { Metadata } from "next";
import Link from "next/link";

import { DegradedBanner } from "@/components/degraded-banner";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";

export const metadata: Metadata = {
  title: "数据来源与免责声明",
  description:
    "TripDecider 的地点、路线与距离数据从哪来、可能有哪些偏差、降级模式下会发生什么，以及我们明确不承诺的事情。",
};

const SOURCES: ReadonlyArray<{ title: string; body: string }> = [
  {
    title: "地点：OpenStreetMap（ODbL）",
    body: "地点的坐标、类别、名称与营业时间原文来自 OpenStreetMap（© OpenStreetMap contributors，ODbL 授权），经本项目的规则管线筛选与丰富化后入库。每张地点卡上的「来源」链接可点回原始 OSM 条目。具体规模与知识库版本号可在浏览页顶部与后端 /health 接口读到 —— 这里刻意不写死数字，否则它会随每次建库而过时。",
  },
  {
    title: "路线与编辑性评分：人工整理",
    body: "精选路线模板、地点别名与热度分值由本项目人工整理与校准（分值卡片上会标注是「人工校准」还是「规则推导」）。这部分是编辑判断，不是客观事实。",
  },
  {
    title: "距离与耗时：路网或估算值",
    body: "路线里的点间距离优先使用路网数据；没有路网数据时会退化为直线距离乘以系数，并在方案中标注为估算值。我们宁可标 unknown，也不会编一个看起来精确的数字。",
  },
  {
    title: "排序与取舍：本地评分算法",
    body: "方案不靠「感觉」排序，而由一套可复现的评分与约束算法生成（评分版本、限制版本都随 /health 暴露）。同一个需求、同一份数据，应该得到可解释的同一套结果。",
  },
  {
    title: "票价：暂时没有",
    body: "我们没有可靠的票价来源，所以知识库里的票价字段全部为空、界面上如实显示「未知」。宁可不提供这个字段，也不用估算值把它填满 —— 一张错的票价会让预算直接失准。",
  },
];

const LIMITS: readonly string[] = [
  "营业时间、票价、活动状态随时可能变化，出行前请以场馆官方信息为准。",
  "估算距离与实际步行/驾车距离存在偏差；拥堵、排队、天气都不在估算范围内。",
  "方案是建议，不是保证。临时闭馆、下雨、临时交通管制都可能让某一步走不通。",
  "票价目前为空（我们没有可靠来源），因此不参与方案预算；我们也不代订任何门票、餐位或交通。",
];

const DEGRADED: ReadonlyArray<{ mode: string; effect: string }> = [
  { mode: "未配置 LLM", effect: "方案由规则引擎生成：结构完整、可解释，但文案更朴素。" },
  { mode: "未配置搜索", effect: "只用本地知识库：不会引入库外的新地点，时效性以知识库版本为准。" },
  { mode: "地图能力受限", effect: "距离改用路网或估算值，方案里会明确标注。" },
];

/** 区块：统一的小标题 + 发丝线分隔，页面因此不需要一堆白卡片。 */
function Section({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="mt-16 border-t border-line pt-10">
      <h2 id={id} className="text-2xl tracking-[-0.01em] text-ink sm:text-3xl">
        {title}
      </h2>
      {description ? (
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-ink-soft">
          {description}
        </p>
      ) : null}
      <div className="mt-8">{children}</div>
    </section>
  );
}

export default function DataSourcesPage() {
  return (
    <div className="min-h-dvh bg-sand">
      <SiteHeader current="about" />

      <main className="mx-auto w-full max-w-4xl px-5 pt-16 pb-24 sm:px-8">
        <Link
          href="/"
          className="inline-flex min-h-11 items-center text-sm text-teal-dark transition-colors duration-300 hover:text-ink"
        >
          ← 回到规划
        </Link>

        <h1 className="mt-6 text-4xl leading-[1.08] tracking-[-0.01em] text-ink sm:text-5xl">
          数据来源与免责声明
        </h1>
        <p className="mt-6 max-w-2xl text-base leading-relaxed text-ink-soft">
          这个产品的卖点是「路线靠谱」，所以数据从哪来、哪里可能不准，必须先说清楚。
        </p>

        <div className="mt-8 max-w-2xl">
          <DegradedBanner />
        </div>

        <Section id="sources-title" title="数据从哪来">
          <div className="divide-y divide-line border-y border-line">
            {SOURCES.map((source) => (
              <article key={source.title} className="py-6">
                <h3 className="text-base font-medium text-ink">
                  {source.title}
                </h3>
                <p className="mt-2.5 text-sm leading-relaxed text-ink-soft">
                  {source.body}
                </p>
              </article>
            ))}
          </div>
        </Section>

        <Section id="limits-title" title="我们不保证什么">
          <ul className="divide-y divide-line border-y border-line">
            {LIMITS.map((limit) => (
              <li
                key={limit}
                className="flex gap-3 py-4 text-sm leading-relaxed text-ink-soft"
              >
                <span aria-hidden="true" className="text-coral">
                  ·
                </span>
                <span>{limit}</span>
              </li>
            ))}
          </ul>
        </Section>

        <Section
          id="degraded-title"
          title="降级模式下会发生什么"
          description="后端缺少某些外部能力时不会假装一切正常，而是进入降级模式，并把当前模式显示在页面顶部的状态条里。"
        >
          <dl className="divide-y divide-line border-y border-line">
            {DEGRADED.map((item) => (
              <div key={item.mode} className="py-5">
                <dt className="text-sm font-medium text-ink">{item.mode}</dt>
                <dd className="mt-1.5 text-sm leading-relaxed text-ink-soft">
                  {item.effect}
                </dd>
              </div>
            ))}
          </dl>
        </Section>

        <Section id="privacy-title" title="你的输入去了哪里">
          <p className="max-w-2xl text-sm leading-relaxed text-ink-soft">
            你在规划卡里填的内容只会作为一次请求发给我们自己的后端，用来生成方案。前端不保存任何第三方密钥，也不会把输入写入浏览器本地存储。补充要求有 500
            字上限，超出部分不会被记录。
          </p>
        </Section>

        <Section id="fix-title" title="发现数据错了怎么办">
          <p className="max-w-2xl text-sm leading-relaxed text-ink-soft">
            地点关闭、营业时间变了、价格不对：请在项目仓库里提一个 issue，写清楚地点名和你看到的实际情况。我们会先把它标成待核实，再决定是否从方案里撤下
            —— 宁可少一个点，也不给一个错的点。
          </p>
        </Section>

        <p className="mt-16 text-xs text-ink-faint">
          本页内容随知识库版本更新；当前后端版本与知识库规模可在首页顶部的状态条中查看。
        </p>
      </main>

      <SiteFooter />
    </div>
  );
}
