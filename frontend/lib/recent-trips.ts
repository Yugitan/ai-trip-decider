/**
 * 「我的行程」的本地台账（PRD §7.1 路由表最后一行：`/me`，CSR + LocalStorage）。
 *
 * ★ 为什么这一份进 localStorage，而开发面板的 token 只进 sessionStorage ★
 * 开发设置面板里的 admin token 是一个**凭据**（`lib/dev-api.ts`）：长期留在磁盘上
 * 只有坏处，所以关掉标签页就没了。这里的记录正相反 —— 它是**用户自己建过/看过的行程地址**，
 * 关掉标签页就消失等于这个功能不存在。取舍不同，介质就不同，不是前后不一致。
 *
 * ★ 记录里只有两个字段：id 与标题 ★
 * 行程内容仍然由后端按会话授权（别人的浏览器拿这个 id 只会拿到 403），
 * 所以这份记录即便被人读到，也只是一串**打不开的** id。页面上把这句话说出来，
 * 而不是让用户以为"本地存了一份行程副本"。
 *
 * ★ 这份 JSON 完全在用户手里 ★
 * 它可被编辑、可被别的版本写坏、可被另一个标签页写一半、可以在隐私模式下写入直接抛错。
 * 因此读取一律"逐条校验 + 丢弃坏条目"，而不是 `JSON.parse(...) as RecentTrip[]`
 * —— 后者会把一个 undefined 一路带进 React 的渲染里。
 */

/** 存储键。与 `tripdecider.dev.admin_token` 同一命名空间（同源的 localStorage 是共享的）。 */
export const RECENT_TRIPS_KEY = "tripdecider.recent_trips";

/** 只留最近这么多次。再多既没人看，也在让 localStorage 变成一个无上限的数据仓库。 */
export const RECENT_TRIPS_LIMIT = 10;

/** 一条"我建过/看过"的记录。 */
export interface RecentTrip {
  /** 后端 trip_id（`TripOut.trip_id`）—— 行程自己的地址就是 `/trip/{id}`。 */
  id: string;
  /** 标题；后端没给标题时存的是 `未命名行程`，不是一个空串。 */
  title: string;
  /** ISO 时间戳（写入时刻）。 */
  savedAt: string;
}

/**
 * 存储接口只要求用到的那三个方法。
 *
 * 这样测试可以传一个内存实现，不必依赖全局 `localStorage`
 * （Node 在没有 `--localstorage-file` 时会把它屏蔽成 undefined）。
 */
export interface TripStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 浏览器里的 localStorage；隐私模式等取不到时返回 `null`（页面照常渲染，只是没记录）。 */
export function browserTripStorage(): TripStorage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    // 隐私模式下访问 localStorage 会抛错：这一页不该因此整页崩掉。
    return null;
  }
}

/** 一条记录必须**三个字段都是非空字符串**才算数 —— 缺一个就丢弃，不补默认值。 */
function isEntry(value: unknown): value is RecentTrip {
  if (typeof value !== "object" || value === null) return false;
  const entry = value as Record<string, unknown>;
  return (
    typeof entry.id === "string" &&
    entry.id.trim() !== "" &&
    typeof entry.title === "string" &&
    entry.title.trim() !== "" &&
    typeof entry.savedAt === "string" &&
    entry.savedAt.trim() !== ""
  );
}

/** 读回记录（最新在前）。任何解析问题都退化成"没有记录"，而不是抛异常。 */
export function readRecentTrips(storage: TripStorage | null = browserTripStorage()): RecentTrip[] {
  const raw = storage?.getItem(RECENT_TRIPS_KEY);
  if (typeof raw !== "string" || raw === "") return [];

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // 被手改坏了 / 被别的版本写成了别的东西：当作没有记录。
    return [];
  }
  if (!Array.isArray(parsed)) return [];

  const seen = new Set<string>();
  const trips: RecentTrip[] = [];
  for (const entry of parsed) {
    if (!isEntry(entry) || seen.has(entry.id)) continue;
    seen.add(entry.id);
    trips.push({ id: entry.id, title: entry.title, savedAt: entry.savedAt });
  }
  return trips.slice(0, RECENT_TRIPS_LIMIT);
}

/**
 * 记下一次行程（已存在则**移到最前并刷新标题与时间**，不产生重复项）。
 *
 * 写失败（配额满 / 隐私模式）时不抛错也不假装成功：返回的是"内存里应该是的样子"，
 * 这样刚建完行程的用户至少能在当前这一屏看到它，而不是看到一个凭空失败的操作。
 */
export function rememberRecentTrip(
  trip: { id: string; title: string | null },
  storage: TripStorage | null = browserTripStorage(),
  now: () => Date = () => new Date(),
): RecentTrip[] {
  const id = trip.id.trim();
  if (id === "") return readRecentTrips(storage);

  const title = (trip.title ?? "").trim() || "未命名行程";
  const entry: RecentTrip = { id, title, savedAt: now().toISOString() };
  const next = [entry, ...readRecentTrips(storage).filter((item) => item.id !== id)].slice(
    0,
    RECENT_TRIPS_LIMIT,
  );

  try {
    storage?.setItem(RECENT_TRIPS_KEY, JSON.stringify(next));
  } catch {
    // 配额满 / 隐私模式：记录没落盘，但本次渲染仍然能看到它。
  }
  return next;
}

/** 清空记录（`/me` 上那个按钮）。 */
export function forgetRecentTrips(storage: TripStorage | null = browserTripStorage()): void {
  try {
    storage?.removeItem(RECENT_TRIPS_KEY);
  } catch {
    // 同 rememberRecentTrip：清不掉也不该把页面打崩。
  }
}

/**
 * 记录时间的显示口径：`2026-09-15 22:10`，**本地时区**（用户看的是墙上的表）。
 *
 * 刻意的两个选择：
 * 1. **不用 `toLocaleString`**：它的输出随 Node/浏览器/ICU 数据而变，
 *    而这个值会出现在页面与快照里 —— 口径不稳定就没有可钉住的断言；
 * 2. 坏时间戳返回**空串**（调用方据此不显示时间），而不是显示 `Invalid Date`。
 */
export function formatSavedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    ` ${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}
