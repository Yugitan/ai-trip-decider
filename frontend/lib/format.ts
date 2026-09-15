/**
 * 展示层的纯格式化函数。
 *
 * 为什么单独一个模块：这些函数此前内联在 `app/explore/[city]/page.tsx` 里。
 * 那是一个 async 服务端组件，很难单测；而它们恰恰是**最容易出错、最该被测试**的一类代码 ——
 * 边界值（null / 0 / 负数 / 单位切换点）决定了用户看到的是「未知」还是一个错误的数字。
 *
 * 原则（与后端一致）：拿不到或不合法的数据一律显示「未知」，
 * 绝不用 0 或空字符串冒充一个看起来正常的数值。
 */

/**
 * 原型安全的查表。
 *
 * 为什么不能直接写 `TABLE[key] ?? key`（看起来完全正确）：
 * 对象字面量继承 `Object.prototype`，所以 `TABLE["constructor"]` 拿到的是 `Object`
 * 构造函数、`TABLE["__proto__"]` 拿到的是一个对象，而 `??` 只拦 `null`/`undefined` ——
 * 这些值会**穿过回退分支**。于是 `formatTransport("__proto__")` 会返回一个对象，
 * 而它的签名写着 `string`；React 拿到 function/object 当子节点会直接抛错。
 *
 * 这些值都来自后端（枚举、降级模式名），属于不可信输入 —— 查表必须只认自有属性。
 */
export function lookupLabel<T>(
  table: Readonly<Record<string, T>>,
  key: string,
): T | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

/** 出行方式的中文说法（接口返回枚举，展示层负责翻译）。 */
export const TRANSPORT_LABELS: Readonly<Record<string, string>> = {
  walk: "步行",
  metro: "地铁",
  bus: "公交",
  taxi: "打车",
  bike: "骑行",
  ferry: "轮渡",
};

/** 方案原型的中文说法。 */
export const ARCHETYPE_LABELS: Readonly<Record<string, string>> = {
  relaxed: "轻松休闲",
  classic: "经典打卡",
  themed: "主题型",
};

/**
 * 分钟数 → 人话时长。
 *
 * 注意这里**不用** `if (!minutes)` 这种真假值判断：它会同时命中 `0`、`NaN` 与 `null`，
 * 把「没有数据」和「数据是 0」混为一谈。显式判断既更准确，也更容易被测试覆盖。
 */
export function formatDuration(minutes: number | null): string {
  if (minutes === null || !Number.isFinite(minutes) || minutes <= 0) {
    return "时长未知";
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest} 分钟`;
  return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分`;
}

/**
 * 米数 → 人话距离（1 km 以上切换单位）。
 *
 * 负数属于数据异常（后端保证 >= 0），显示为「未知」而不是 `-5 m` ——
 * 让用户看到一个负距离比说「不知道」更糟。
 */
export function formatDistance(meters: number | null): string {
  if (meters === null || !Number.isFinite(meters) || meters < 0) {
    return "步行距离未知";
  }
  return meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${meters} m`;
}

/**
 * 站间交通 → 人话：时长与距离**各自判断**，缺哪一半就说哪一半未知。
 *
 * 为什么不能写成 `minutes === null ? "未知" : …`：那样会在「有时长没距离」时把
 * 时长一起丢掉，或反过来。只缺一半就只说一半，比整格变成「未知」有用得多，
 * 而且不会让人以为"这一段没有任何数据"。
 */
export function formatTransit(minutes: number | null, meters: number | null): string {
  const time = minutes === null ? null : `${minutes} 分钟`;
  const distance = meters === null ? null : formatDistance(meters);
  if (time === null && distance === null) return "未知";
  if (time === null) return `时长未知 · ${distance}`;
  if (distance === null) return time;
  return `${time} · ${distance}`;
}

/** 出行方式枚举 → 中文；未知枚举原样返回，便于发现后端新增了取值。 */
export function formatTransport(value: string): string {
  return lookupLabel(TRANSPORT_LABELS, value) ?? value;
}

/** 方案原型枚举 → 中文；未知枚举原样返回。 */
export function formatArchetype(value: string): string {
  return lookupLabel(ARCHETYPE_LABELS, value) ?? value;
}

/**
 * 票价区间 → 人话。
 *
 * 与 `formatDuration` / `formatDistance` 同一原则：**不知道就说不知道**。
 * 具体地：
 * - 两端都缺 → 「未知」；
 * - 只有下限 → 「20 元起」（说「20 元」就是编造上界）；
 * - 只有上限 → 「最多 50 元」；
 * - 上下限反了（数据矛盾）→ 「未知」，**不以任何一端猜一个区间**。
 */
export function formatPrice(min: number | null, max: number | null): string {
  const low = isUsableAmount(min) ? min : null;
  const high = isUsableAmount(max) ? max : null;
  if (low === null && high === null) return "未知";
  if (low === null) return `最多 ${high} 元`;
  if (high === null) return `${low} 元起`;
  if (low > high) return "未知";
  return low === high ? `${low} 元` : `${low}–${high} 元`;
}

function isUsableAmount(value: number | null): value is number {
  return value !== null && Number.isFinite(value) && value >= 0;
}

/**
 * 金额字符串 → 展示值。
 *
 * 后端用 Decimal，JSON 里是**字符串**（`"18.32"`）。这里只做尾零收敛（`"20.00"` → `"20"`），
 * 不做任何浮点换算：一旦 `Number()` 之后再 `toFixed()`，就等于在前端重算了一遍钱。
 * 非数值内容原样返回，不猜。
 */
export function formatAmount(value: string): string {
  if (!/^\d+(\.\d+)?$/.test(value)) return value;
  return value.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

/** 预算口径 → 后缀。未知口径返回空串，不硬编一个"每人"上去。 */
function budgetScopeSuffix(scope: string | null): string {
  if (scope === "per_person") return "/人";
  if (scope === "total") return "/总计";
  return "";
}

/**
 * 预算区间 → 人话。与 `formatPrice` 同一原则：
 * 只有下限就说「起」，只有上限就说「最多」，两端反了就说「未知」。
 *
 * 参数是 `TripRoute` 上的三个字段（`budget_min` / `budget_max` / `budget_scope`），
 * 放在这里而不是某个组件里：**结果卡片与对比视图必须说同一句话** ——
 * 同一份数据在两处显示成两个样子，比只说一处更糟。
 */
export function formatBudget(
  min: string | null,
  max: string | null,
  scope: string | null,
): string {
  const suffix = budgetScopeSuffix(scope);
  const low = min === null ? null : formatAmount(min);
  const high = max === null ? null : formatAmount(max);
  const lowIsAmount = low !== null && /^\d/.test(low);
  const highIsAmount = high !== null && /^\d/.test(high);

  if (!lowIsAmount && !highIsAmount) return "未知";
  if (!lowIsAmount) return `最多 ¥${high}${suffix}`;
  if (!highIsAmount) return `¥${low} 起${suffix}`;
  if (Number(low) > Number(high)) return "未知";
  return low === high ? `¥${low}${suffix}` : `¥${low}–${high}${suffix}`;
}

/** 允许出现在 `<a href>` 里的协议白名单。 */
const SAFE_URL_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:"]);

/**
 * 把外部来源 URL 收敛成一个可以安全放进 `<a href>` 的值。
 *
 * 为什么需要：`<a href="javascript:...">` 在点击时会执行脚本，
 * `data:` URL 同理。当前来源 URL 全部由抓取脚本构造成
 * `https://www.openstreetmap.org/...`，实践上安全 —— 但这是一层廉价的防御纵深，
 * 等 M3 引入联网搜索、来源变成外部输入时就是必需的。
 *
 * 无法解析或协议不在白名单时返回 `null`，调用方据此**降级为纯文本**而不是
 * 悄悄丢掉来源署名（来源可追溯是这个项目的硬性要求）。
 */
export function safeExternalUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return SAFE_URL_PROTOCOLS.has(url.protocol) ? url.toString() : null;
  } catch {
    return null;
  }
}
