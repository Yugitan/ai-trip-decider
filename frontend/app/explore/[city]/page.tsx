import Link from "next/link";

import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import {
  ApiError,
  getCityStats,
  listPlaces,
  listRoutes,
  NetworkError,
  type CityStats,
  type PlacePage,
  type RouteSummary,
} from "@/lib/api";

/**
 * 知识库浏览页。
 *
 * 为什么需要这一页：知识库是 M1 的交付物，但如果它只能通过 psql 查看，
 * "交付了 3776 个真实地点"就只是一句无法验证的话。这一页让数据可以在浏览器里
 * 被真实检查 —— 包括它的**缺口**（营业时间未知、身份未经交叉核验）。
 *
 * 三个刻意的设计决定：
 * 1. `force-dynamic`：不在构建期预渲染。否则 `pnpm build` 时会去连后端，
 *    后端没启动就构建失败（CI 里尤其致命）。
 * 2. 后端不可用时展示**明确的故障状态与修复指令**，而不是空白页或假数据。
 * 3. 每一处不确定信息都显示出来：`⚠️ 营业时间未知`、`estimated` 标记、
 *    `derived` 评分来源 —— 用户有权知道哪些数字是靠不住的。
 *
 * 视觉（v2）：层次改由发丝线（`border-line`）与留白建立，卡片不再投影；
 * 数字一律 `tnum` 对齐。语义、文案与角色**一律未改** —— 这一页的价值在信息本身。
 */

import {
  formatArchetype,
  formatDistance,
  formatDuration,
  formatPrice,
  formatTransport,
  safeExternalUrl,
} from "@/lib/format";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 24;

/** 全站统一栅格，与首页保持一致。 */
const SHELL = "mx-auto w-full max-w-7xl px-5 sm:px-8";

function UnknownBadge({ label }: { label: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-coral/20 bg-coral-tint px-2.5 py-1 text-[11px] text-coral"
      title="该信息没有可核验来源，出发前请确认官方信息"
    >
      <span aria-hidden="true">⚠</span>
      {label}
    </span>
  );
}

function BackendDown({ error }: { error: unknown }) {
  const isNetwork = error instanceof NetworkError;
  const message = error instanceof ApiError ? error.message : null;
  const hint = error instanceof ApiError ? error.hint : null;

  return (
    <div className="rounded-card border border-coral/25 bg-coral-tint/50 p-6">
      <h2 className="text-xl text-ink">
        {isNetwork ? "未连接到后端服务" : "读取知识库失败"}
      </h2>
      {message ? (
        <p className="mt-3 text-sm text-ink-soft">{message}</p>
      ) : null}
      <p className="mt-2 text-sm text-ink-soft">
        在项目根目录运行{" "}
        <code className="rounded bg-shell px-1.5 py-0.5 text-[13px] tnum">
          make dev
        </code>{" "}
        启动前后端，然后刷新本页。
      </p>
      {hint ? <p className="mt-1 text-xs text-ink-faint">{hint}</p> : null}
      <p className="mt-4 text-xs text-ink-faint">
        这一页刻意不显示任何占位数据 —— 读不到知识库时，它就说读不到。
      </p>
    </div>
  );
}

function StatsStrip({ city, stats }: { city: string; stats: CityStats }) {
  const hoursRatio = stats.total_places
    ? Math.round((stats.opening_hours_known / stats.total_places) * 100)
    : 0;
  return (
    <section
      aria-label="知识库概况"
      className="rounded-card border border-line bg-shell/70 p-6 sm:p-8"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-2xl tracking-[-0.01em] text-ink">{city} 知识库</h2>
        <Link
          href="/about/data"
          className="text-sm text-teal-dark transition-colors duration-300 hover:text-ink"
        >
          这些数据从哪来 →
        </Link>
      </div>

      <dl className="mt-8 grid grid-cols-2 gap-x-6 gap-y-8 border-t border-line pt-8 sm:grid-cols-4">
        <div>
          <dt className="text-xs text-ink-faint">地点</dt>
          <dd className="tnum font-display mt-2 text-3xl leading-none text-ink">
            {stats.total_places}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-ink-faint">身份可交叉核验</dt>
          <dd className="tnum font-display mt-2 text-3xl leading-none text-ink">
            {stats.verification.verified ?? 0}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-ink-faint">营业时间已知</dt>
          <dd className="tnum font-display mt-2 text-3xl leading-none text-ink">
            {hoursRatio}%
          </dd>
        </div>
        <div>
          <dt className="text-xs text-ink-faint">其余可信度</dt>
          <dd className="tnum font-display mt-2 text-3xl leading-none text-ink">
            {stats.verification.probable ?? 0}
          </dd>
        </div>
      </dl>

      <p className="mt-8 border-t border-line pt-5 text-xs leading-relaxed text-ink-soft">
        <strong className="font-medium text-ink">怎么读这组数字：</strong>
        身份可交叉核验 = 该地点在 OpenStreetMap 上有维基数据条目，可被独立验证；
        其余为「有真实坐标与 OSM 链接，但未做二次核验」。
        营业时间是从 OSM 标签读到的原文，覆盖率天然偏低 —— 缺失的会在地点卡片上标
        <span className="mx-1 text-coral">⚠ 营业时间未知</span>
        ，我们不会用估算值填上。
      </p>
    </section>
  );
}

function CategoryChips({
  city,
  stats,
  activeCategory,
  query,
}: {
  city: string;
  stats: CityStats;
  activeCategory?: string;
  query?: string;
}) {
  const buildHref = (category?: string) => {
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (category) params.set("category", category);
    const suffix = params.toString();
    // 与 `lib/api.ts` 保持一致：city 来自 URL 段，必须编码。
    // 不编码时 `a/b` 会被拼成两个路径段，链到另一个页面。
    return `/explore/${encodeURIComponent(city)}${suffix ? `?${suffix}` : ""}`;
  };

  const chipClass = (active: boolean) =>
    `inline-flex min-h-11 items-center rounded-full border px-4 text-sm transition-colors duration-300 ${
      active
        ? "border-teal/40 bg-teal-tint text-teal-dark"
        : "border-line bg-shell/60 text-ink-soft hover:border-ink/20 hover:text-ink"
    }`;

  return (
    <nav aria-label="按类别筛选" className="flex flex-wrap gap-2">
      <Link
        href={buildHref()}
        aria-current={activeCategory ? undefined : "page"}
        className={chipClass(!activeCategory)}
      >
        全部 <span className="tnum ml-1.5 text-xs opacity-70">{stats.total_places}</span>
      </Link>
      {stats.categories.map((row) => (
        <Link
          key={row.category}
          href={buildHref(row.category)}
          aria-current={activeCategory === row.category ? "page" : undefined}
          className={chipClass(activeCategory === row.category)}
        >
          {row.label}{" "}
          <span className="tnum ml-1.5 text-xs opacity-70">{row.count}</span>
        </Link>
      ))}
    </nav>
  );
}

function PlaceCard({ place }: { place: PlacePage["items"][number] }) {
  const hoursUnknown = place.unknown_fields.includes("opening_hours");
  const priceUnknown = place.unknown_fields.includes("price_min");
  const popularity = place.scores.popularity;
  const priceLabel = formatPrice(place.price_min, place.price_max);
  // 来源 URL 来自数据库（未来可能来自联网搜索），必须过一遍协议白名单再放进 href
  const source = place.sources[0];
  const sourceUrl = safeExternalUrl(source?.url);

  return (
    <li className="flex flex-col rounded-card border border-line bg-shell/60 p-5 transition-colors duration-300 hover:border-ink/15">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-[15px] leading-snug font-medium text-ink">
          {place.name}
        </h3>
        {popularity !== undefined ? (
          <span
            className="tnum shrink-0 rounded-full border border-teal/25 bg-teal-tint px-2 py-0.5 text-xs text-teal-dark"
            title={`热度分值（${place.score_source === "curated" ? "人工校准" : "规则推导"}）；分值是排序用的编辑性评分，不是事实`}
          >
            {popularity.toFixed(2)}
          </span>
        ) : null}
      </div>

      {place.aliases.length > 0 ? (
        <p className="mt-1.5 text-xs text-ink-faint">
          又称：{place.aliases.slice(0, 3).join(" · ")}
        </p>
      ) : null}

      <dl className="mt-4 space-y-1.5 text-xs text-ink-soft">
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-faint">建议停留</dt>
          <dd className="tnum">
            {place.recommended_duration_min
              ? `${place.recommended_duration_min} 分钟`
              : "未知"}
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-faint">营业时间</dt>
          <dd>{place.opening_hours_raw ?? <span className="text-coral">未知</span>}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-faint">票价</dt>
          {/* 票价与营业时间同等对待：**有值就显示**。
              早期版本只在 `unknown_fields` 含 price_min 时显示「⚠ 票价未知」，
              于是这两个字段取回来了却从不上屏 —— 只知道缺什么，永远看不到有什么。
              这里直接由数据决定显示什么，不依赖后端自己的缺口记账（两者不一致时以数据为准）。 */}
          <dd className="tnum">
            {priceLabel === "未知" ? (
              <span className="text-coral">{priceLabel}</span>
            ) : (
              priceLabel
            )}
          </dd>
        </div>
        {place.district ? (
          <div className="flex gap-2">
            <dt className="shrink-0 text-ink-faint">行政区</dt>
            <dd>{place.district}</dd>
          </div>
        ) : null}
      </dl>

      {place.tags.length > 0 ? (
        <ul className="mt-4 flex flex-wrap gap-1.5">
          {place.tags.slice(0, 5).map((tag) => (
            <li
              key={tag}
              className="rounded-full bg-sand px-2.5 py-0.5 text-[11px] text-ink-soft"
            >
              {tag}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-line/70 pt-4">
        {hoursUnknown ? <UnknownBadge label="营业时间未知" /> : null}
        {priceUnknown ? <UnknownBadge label="票价未知" /> : null}
        {source && sourceUrl ? (
          <a
            href={sourceUrl}
            target="_blank"
            rel="noreferrer noopener"
            className="text-[11px] text-teal-dark transition-colors duration-300 hover:text-ink"
          >
            来源：{source.name}
          </a>
        ) : source ? (
          // URL 不可用（缺失或协议不被允许）时保留来源署名，只是不给可点击的链接 ——
          // 来源可追溯是硬性要求，不能因为链接不可用就把出处一起抹掉。
          <span className="text-[11px] text-ink-faint">
            来源：{source.name}（链接不可用）
          </span>
        ) : null}
      </div>
    </li>
  );
}

function RouteCard({ route }: { route: RouteSummary }) {
  return (
    <li className="rounded-card border border-line bg-shell/60 p-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-lg font-medium text-ink">{route.name}</h3>
        <div className="flex items-center gap-2.5 text-xs">
          {route.archetype_hint ? (
            <span className="rounded-full border border-teal/25 bg-teal-tint px-2.5 py-0.5 text-teal-dark">
              {formatArchetype(route.archetype_hint)}
            </span>
          ) : null}
          <span className="tnum text-ink-faint">{route.stop_count} 站</span>
        </div>
      </div>

      {route.description ? (
        <p className="mt-3 text-sm leading-relaxed text-ink-soft">
          {route.description}
        </p>
      ) : null}

      <dl className="mt-5 flex flex-wrap gap-x-8 gap-y-2 text-xs text-ink-soft">
        <div className="flex gap-2">
          <dt className="text-ink-faint">总时长</dt>
          <dd className="tnum">{formatDuration(route.duration_min)}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-ink-faint">步行</dt>
          <dd className="tnum">{formatDistance(route.walking_distance_m)}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-ink-faint">推荐时段</dt>
          <dd className="tnum">
            {route.recommended_start_time && route.recommended_end_time
              ? `${route.recommended_start_time}–${route.recommended_end_time}`
              : "未知"}
          </dd>
        </div>
      </dl>

      {route.stops.length > 0 ? (
        <ol className="mt-5 space-y-2.5 border-t border-line pt-5">
          {route.stops.map((stop) => (
            <li
              key={stop.seq}
              className="flex flex-wrap items-baseline gap-x-2.5 text-sm"
            >
              <span className="tnum text-xs text-ink-faint">
                {String(stop.seq + 1).padStart(2, "0")}
              </span>
              <span className="font-medium text-ink">{stop.place_name}</span>
              <span className="tnum text-xs text-ink-faint">
                停留 {stop.stay_min ?? "?"} 分钟
              </span>
              {stop.transport_to_next ? (
                <span className="text-xs text-ink-soft">
                  → {formatTransport(stop.transport_to_next)}
                  {stop.transport_to_next_min
                    ? ` ${stop.transport_to_next_min} 分`
                    : ""}
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}

      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-5">
        <UnknownBadge label="时长与距离为估算值" />
        <UnknownBadge label="预算未估算" />
      </div>
    </li>
  );
}

export default async function ExplorePage({
  params,
  searchParams,
}: {
  params: Promise<{ city: string }>;
  searchParams: Promise<{ q?: string; category?: string }>;
}) {
  const { city } = await params;
  const { q, category } = await searchParams;

  let stats: CityStats;
  let places: PlacePage;
  let routes: RouteSummary[];
  let routeTotal: number;
  try {
    const [statsResult, placesResult, routesResult] = await Promise.all([
      getCityStats(city),
      listPlaces(city, { q, category, limit: PAGE_SIZE }),
      // 浏览页只展示前 6 条，避免页面过长（完整列表由接口分页提供）
      listRoutes(city, { with_stops: true }),
    ]);
    stats = statsResult.data;
    places = placesResult.data;
    routeTotal = routesResult.data.total;
    routes = routesResult.data.items.slice(0, 6);
  } catch (error) {
    return (
      <>
        <SiteHeader current="explore" />
        <main className={`${SHELL} pt-16 pb-24`}>
          <h1 className="text-4xl tracking-[-0.01em] text-ink sm:text-5xl">
            知识库浏览
          </h1>
          <div className="mt-10">
            <BackendDown error={error} />
          </div>
        </main>
        <SiteFooter />
      </>
    );
  }

  return (
    <>
      <SiteHeader current="explore" />
      <main className={`${SHELL} pt-16 pb-24`}>
        <header className="border-b border-line pb-12">
          <p className="text-xs tracking-[0.14em] text-ink-soft uppercase">
            知识库 · 广州
          </p>
          <h1 className="mt-6 text-4xl leading-[1.08] tracking-[-0.01em] text-ink sm:text-5xl">
            知识库浏览
          </h1>
          <p className="mt-6 max-w-3xl text-sm leading-relaxed text-ink-soft sm:text-base">
            下面是路线决策引擎实际会用的地点与路线模板。这里刻意把数据的
            <strong className="font-medium text-ink">缺口</strong>
            一起展示出来 —— 标着 <span className="text-coral">⚠</span> 的字段没有可核验来源，
            出发前请确认官方信息。我们宁可显示「未知」，也不填一个看起来合理的猜测值。
          </p>
        </header>

        <div className="mt-12">
          <StatsStrip city={city === "guangzhou" ? "广州" : city} stats={stats} />
        </div>

        <section className="mt-20" aria-labelledby="places-heading">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <h2
              id="places-heading"
              className="text-2xl tracking-[-0.01em] text-ink"
            >
              地点库
              <span className="tnum ml-2.5 text-sm font-normal text-ink-faint">
                {places.page.total} 条
                {q ? `（搜索「${q}」）` : ""}
              </span>
            </h2>
            <form
              action={`/explore/${encodeURIComponent(city)}`}
              method="get"
              className="flex gap-2"
            >
              <label htmlFor="place-search" className="sr-only">
                搜索地点名称或别名
              </label>
              <input
                id="place-search"
                name="q"
                type="search"
                defaultValue={q ?? ""}
                placeholder="试试「小蛮腰」或「太古仓」"
                className="min-h-11 w-56 rounded-btn border border-line bg-shell/60 px-4 text-sm text-ink placeholder:text-ink-faint focus:border-teal"
              />
              {category ? (
                <input type="hidden" name="category" value={category} />
              ) : null}
              <button
                type="submit"
                className="min-h-11 rounded-full bg-ink px-5 text-sm font-medium text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100"
              >
                搜索
              </button>
            </form>
          </div>

          <div className="mt-6">
            <CategoryChips
              city={city}
              stats={stats}
              activeCategory={category}
              query={q}
            />
          </div>

          {places.items.length === 0 ? (
            <p className="mt-8 rounded-card border border-line bg-shell/60 p-6 text-sm text-ink-soft">
              没有匹配的地点。换个关键词，或点上面的「全部」看看库里有什么。
            </p>
          ) : (
            <ul className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {places.items.map((place) => (
                <PlaceCard key={place.id} place={place} />
              ))}
            </ul>
          )}

          {places.page.has_more ? (
            <p className="mt-6 text-xs text-ink-faint">
              仅显示前 {PAGE_SIZE} 条（按热度排序）。完整列表可用接口分页：
              {/* break-all：这个接口路径里没有可换行的位置，不加就会在 375px 上把页面撑宽 */}
              <code className="ml-1 rounded bg-shell px-1.5 py-0.5 break-all">
                /api/v1/cities/{city}/places?limit=100&amp;offset=
                {places.page.offset + PAGE_SIZE}
              </code>
            </p>
          ) : null}
        </section>

        <section className="mt-24" aria-labelledby="routes-heading">
          <h2 id="routes-heading" className="text-2xl tracking-[-0.01em] text-ink">
            路线模板
            <span className="tnum ml-2.5 text-sm font-normal text-ink-faint">
              共 {routeTotal} 条，这里展示前 {routes.length} 条
            </span>
          </h2>
          <p className="mt-3 max-w-3xl text-sm leading-relaxed text-ink-soft">
            规划时优先命中这些模板（走本地知识库，不联网、零成本），命中不到才从零组合。
          </p>
          <ul className="mt-8 grid grid-cols-1 gap-4 lg:grid-cols-2">
            {routes.map((route) => (
              <RouteCard key={route.id} route={route} />
            ))}
          </ul>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
