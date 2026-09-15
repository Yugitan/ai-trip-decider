/**
 * 展示层格式化函数的单元测试（`lib/format.ts`）。
 *
 * 这类函数看起来"没什么可测的"，但它们决定了用户看到的是「未知」还是一个错误的数字 ——
 * 而错误的方式往往很隐蔽：`if (!minutes)` 会把 0 和 null 一起当成"没有数据"，
 * `meters >= 1000` 的单位切换点差一个数就会让 999 米显示成 1.0 km。
 *
 * 原则：拿不到或不合法的数据一律显示「未知」，绝不显示一个看起来正常的假数值。
 */

import { describe, expect, it } from "vitest";

import {
  ARCHETYPE_LABELS,
  TRANSPORT_LABELS,
  formatArchetype,
  formatDistance,
  formatDuration,
  formatPrice,
  formatTransit,
  formatTransport,
  lookupLabel,
  safeExternalUrl,
} from "@/lib/format";

describe("formatDuration", () => {
  it("null 表示没有数据，显示「未知」", () => {
    expect(formatDuration(null)).toBe("时长未知");
  });

  it("0 与负数属于无意义的时长，同样显示「未知」而不是「0 分钟」", () => {
    expect(formatDuration(0)).toBe("时长未知");
    expect(formatDuration(-30)).toBe("时长未知");
  });

  it("NaN / Infinity 不得渲染成「NaN 分钟」", () => {
    expect(formatDuration(Number.NaN)).toBe("时长未知");
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe("时长未知");
  });

  it("不足一小时时只显示分钟", () => {
    expect(formatDuration(1)).toBe("1 分钟");
    expect(formatDuration(45)).toBe("45 分钟");
    expect(formatDuration(59)).toBe("59 分钟");
  });

  it("整小时时不显示「0 分」", () => {
    expect(formatDuration(60)).toBe("1 小时");
    expect(formatDuration(120)).toBe("2 小时");
  });

  it("小时加分钟用「X 小时 Y 分」", () => {
    expect(formatDuration(61)).toBe("1 小时 1 分");
    expect(formatDuration(90)).toBe("1 小时 30 分");
    expect(formatDuration(185)).toBe("3 小时 5 分");
  });
});

describe("formatDistance", () => {
  it("null 表示没有数据，显示「未知」", () => {
    expect(formatDistance(null)).toBe("步行距离未知");
  });

  it("负数属于数据异常，显示「未知」而不是「-5 m」", () => {
    expect(formatDistance(-5)).toBe("步行距离未知");
  });

  it("NaN / Infinity 不得渲染成「NaN m」", () => {
    expect(formatDistance(Number.NaN)).toBe("步行距离未知");
    expect(formatDistance(Number.POSITIVE_INFINITY)).toBe("步行距离未知");
  });

  it("不足 1 公里时用米", () => {
    expect(formatDistance(0)).toBe("0 m");
    expect(formatDistance(350)).toBe("350 m");
    expect(formatDistance(999)).toBe("999 m");
  });

  it("★ 单位切换点在 1000 米：999 米仍是米，1000 米变成公里", () => {
    expect(formatDistance(999)).toBe("999 m");
    expect(formatDistance(1000)).toBe("1.0 km");
  });

  it("公里保留一位小数", () => {
    expect(formatDistance(1500)).toBe("1.5 km");
    expect(formatDistance(12345)).toBe("12.3 km");
  });
});

describe("formatTransit", () => {
  it("两端都有时，时长与距离一起给", () => {
    expect(formatTransit(17, 2969)).toBe("17 分钟 · 3.0 km");
    expect(formatTransit(8, 537)).toBe("8 分钟 · 537 m");
  });

  it("★ 只缺一半时，不要连另一半一起丢掉", () => {
    // 旧写法是「时长为 null ⇒ 整格显示未知」，于是有距离也看不到
    expect(formatTransit(null, 1200)).toBe("时长未知 · 1.2 km");
    expect(formatTransit(12, null)).toBe("12 分钟");
  });

  it("两端都没有才是「未知」", () => {
    expect(formatTransit(null, null)).toBe("未知");
  });

  it("0 分钟是真实值（同地铁站旁的两点），不当成「没有数据」", () => {
    expect(formatTransit(0, 30)).toBe("0 分钟 · 30 m");
  });
});

describe("枚举翻译", () => {
  it("已知出行方式翻译成中文", () => {
    expect(formatTransport("walk")).toBe("步行");
    expect(formatTransport("metro")).toBe("地铁");
    expect(formatTransport("ferry")).toBe("轮渡");
  });

  it("未知出行方式原样返回（便于发现后端新增了取值，而不是显示空白）", () => {
    expect(formatTransport("hovercraft")).toBe("hovercraft");
  });

  it("已知方案原型翻译成中文", () => {
    expect(formatArchetype("relaxed")).toBe("轻松休闲");
    expect(formatArchetype("classic")).toBe("经典打卡");
    expect(formatArchetype("themed")).toBe("主题型");
  });

  it("未知方案原型原样返回", () => {
    expect(formatArchetype("extreme")).toBe("extreme");
  });

  it("★ 标签表只认自有属性：Object.prototype 上的键不得被当成已知枚举", () => {
    // `TRANSPORT_LABELS["constructor"]` 拿到的是 `Object` 构造函数、
    // `["__proto__"]` 拿到的是一个对象，而 `??` 只拦 null/undefined ——
    // 它们会**穿过回退分支**，让一个声明返回 string 的函数返回函数/对象。
    // React 拿到 function/object 当子节点会直接抬错，整页崩。
    for (const key of [
      "constructor",
      "toString",
      "__proto__",
      "hasOwnProperty",
      "valueOf",
      "isPrototypeOf",
    ]) {
      expect(lookupLabel(TRANSPORT_LABELS, key)).toBeUndefined();
      expect(formatTransport(key)).toBe(key);
      expect(formatArchetype(key)).toBe(key);
      expect(typeof formatTransport(key)).toBe("string");
    }
  });

  it("★ 大写键也拿不到原型上的成员（表单已 toLowerCase，这里钉住直调情形）", () => {
    expect(formatTransport("Constructor")).toBe("Constructor");
    expect(formatArchetype("__Proto__")).toBe("__Proto__");
  });

  it("标签表覆盖后端会返回的全部取值", () => {
    // 后端的 seed/curated 只产出这 6 种出行方式与 3 种 archetype，
    // 表里少一个就会在界面上露出英文枚举。
    expect(Object.keys(TRANSPORT_LABELS).sort()).toEqual(
      ["bike", "bus", "ferry", "metro", "taxi", "walk"].sort(),
    );
    expect(Object.keys(ARCHETYPE_LABELS).sort()).toEqual(
      ["classic", "relaxed", "themed"].sort(),
    );
  });
});

describe("formatPrice", () => {
  it("两端都缺时显示「未知」（包含 null / NaN / 负数）", () => {
    expect(formatPrice(null, null)).toBe("未知");
    expect(formatPrice(Number.NaN, null)).toBe("未知");
    expect(formatPrice(null, -1)).toBe("未知");
  });

  it("★ 只有下限时不能说成确定价格，用「起」标出", () => {
    expect(formatPrice(20, null)).toBe("20 元起");
  });

  it("只有上限时用「最多」", () => {
    expect(formatPrice(null, 50)).toBe("最多 50 元");
  });

  it("上下限相同时只显示一个数（不写「20–20 元」）", () => {
    expect(formatPrice(20, 20)).toBe("20 元");
  });

  it("正常区间用破折号连接", () => {
    expect(formatPrice(20, 50)).toBe("20–50 元");
  });

  it("★ 上下限反了（数据矛盾）时显示「未知」，不用任何一端猜一个区间", () => {
    expect(formatPrice(50, 20)).toBe("未知");
  });

  it("0 元是合法取值（免费），不能当成「未知」", () => {
    expect(formatPrice(0, 0)).toBe("0 元");
  });
});

describe("safeExternalUrl", () => {
  it("放行 http / https 链接", () => {
    expect(safeExternalUrl("https://www.openstreetmap.org/node/1")).toBe(
      "https://www.openstreetmap.org/node/1",
    );
    expect(safeExternalUrl("http://example.com/a")).toBe("http://example.com/a");
  });

  it("★ 拦下 javascript: —— 否则点击链接就会执行脚本", () => {
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("JavaScript:alert(1)")).toBeNull();
    expect(safeExternalUrl("  javascript:alert(1)")).toBeNull();
  });

  it("拦下 data: 与 vbscript:", () => {
    expect(safeExternalUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(safeExternalUrl("vbscript:msgbox(1)")).toBeNull();
  });

  it("拦下 file: 等其它协议", () => {
    expect(safeExternalUrl("file:///etc/passwd")).toBeNull();
    expect(safeExternalUrl("ftp://example.com/x")).toBeNull();
  });

  it("空值与无法解析的字符串返回 null", () => {
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl(undefined)).toBeNull();
    expect(safeExternalUrl("")).toBeNull();
    expect(safeExternalUrl("不是链接")).toBeNull();
    expect(safeExternalUrl("/relative/path")).toBeNull();
  });

  it("返回规范化后的 URL（调用方拿到的就是最终 href）", () => {
    expect(safeExternalUrl("https://example.com")).toBe("https://example.com/");
  });
});
