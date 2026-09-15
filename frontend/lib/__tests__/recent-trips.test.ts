/**
 * 「我的行程」本地台账的测试（`lib/recent-trips.ts`）。
 *
 * 这一份代码处理的是一段**完全在用户手里**的 JSON：可被编辑、可被别的版本写坏、
 * 可被另一个标签页写一半、可在隐私模式下写入直接抛错。所以这里的断言重点不是
 * "正常路径对不对"（那是最容易的部分），而是**坏数据与坏环境下的行为**：
 * 逐个坏条目要被丢掉、坏整体要退化成"没有记录"、写失败不能把页面打崩、
 * 也不能假装写成功了。
 *
 * 全部用例都传内存实现，不碰全局 `localStorage` —— Node 在没有
 * `--localstorage-file` 时会把它屏蔽成 undefined，测试不该依赖运行环境。
 */

import { describe, expect, it, vi } from "vitest";

import {
  browserTripStorage,
  forgetRecentTrips,
  formatSavedAt,
  readRecentTrips,
  RECENT_TRIPS_KEY,
  RECENT_TRIPS_LIMIT,
  rememberRecentTrip,
  type TripStorage,
} from "@/lib/recent-trips";

/** 内存版存储；`raw` 让用例直接看落盘的字符串。 */
function fakeStorage(initial?: string) {
  const data = new Map<string, string>();
  if (initial !== undefined) data.set(RECENT_TRIPS_KEY, initial);
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key),
    raw: data,
  };
}

function entry(id: string, title = "1 天行程", savedAt = "2026-09-15T10:00:00.000Z") {
  return { id, title, savedAt };
}

describe("readRecentTrips", () => {
  it("没有存储（隐私模式 / SSR）时返回空列表", () => {
    expect(readRecentTrips(null)).toEqual([]);
    expect(readRecentTrips(fakeStorage())).toEqual([]);
  });

  it("★ 坏 JSON 退化成空列表，而不是抛异常", () => {
    expect(readRecentTrips(fakeStorage("{不是 JSON"))).toEqual([]);
    expect(readRecentTrips(fakeStorage(""))).toEqual([]);
  });

  it("★ JSON 合法但不是数组时同样退化成空列表", () => {
    expect(readRecentTrips(fakeStorage('{"id":"x"}'))).toEqual([]);
    expect(readRecentTrips(fakeStorage("null"))).toEqual([]);
    expect(readRecentTrips(fakeStorage('"一串字符串"'))).toEqual([]);
  });

  it("★ 逐条校验：缺字段 / 类型不对 / 空串的条目一律丢弃", () => {
    const raw = JSON.stringify([
      entry("good-1"),
      { id: "no-title", savedAt: "2026-09-15T10:00:00.000Z" },
      { id: "no-saved-at", title: "标题" },
      { id: 42, title: "数字 id", savedAt: "2026-09-15T10:00:00.000Z" },
      { id: "blank-id", title: "  ", savedAt: "2026-09-15T10:00:00.000Z" },
      null,
      "不是对象",
      entry("good-2"),
    ]);

    expect(readRecentTrips(fakeStorage(raw)).map((trip) => trip.id)).toEqual(["good-1", "good-2"]);
  });

  it("同 id 只保留最前的一条（坏数据里也可能出现重复）", () => {
    const raw = JSON.stringify([
      entry("dup", "新的标题"),
      entry("dup", "旧的标题"),
      entry("other"),
    ]);

    expect(readRecentTrips(fakeStorage(raw)).map((trip) => trip.title)).toEqual([
      "新的标题",
      // 第二条默认标题来自 entry() 的默认值（它不是 dup）
      "1 天行程",
    ]);
  });

  it("超过上限时只读回最近的那些", () => {
    const many = Array.from({ length: RECENT_TRIPS_LIMIT + 5 }, (_, index) =>
      entry(`trip-${index}`),
    );
    expect(readRecentTrips(fakeStorage(JSON.stringify(many)))).toHaveLength(RECENT_TRIPS_LIMIT);
  });
});

describe("rememberRecentTrip", () => {
  it("★ 新的排在最前，并把内容真的写回存储", () => {
    const storage = fakeStorage();
    const result = rememberRecentTrip(
      { id: "trip-a", title: "沙面一日" },
      storage,
      () => new Date("2026-09-15T10:00:00.000Z"),
    );

    expect(result).toEqual([entry("trip-a", "沙面一日", "2026-09-15T10:00:00.000Z")]);
    expect(JSON.parse(storage.raw.get(RECENT_TRIPS_KEY) ?? "null")).toEqual(result);
  });

  it("★ 同一个 id 再记一次是「移到最前 + 刷新标题」，不是多出一条", () => {
    const storage = fakeStorage(JSON.stringify([entry("trip-b"), entry("trip-a", "旧标题")]));

    const result = rememberRecentTrip(
      { id: "trip-a", title: "新标题" },
      storage,
      () => new Date("2026-09-15T12:00:00.000Z"),
    );

    expect(result.map((trip) => trip.id)).toEqual(["trip-a", "trip-b"]);
    expect(result[0]?.title).toBe("新标题");
    expect(result[0]?.savedAt).toBe("2026-09-15T12:00:00.000Z");
  });

  it("后端没给标题时存一个说人话的标题，而不是空串", () => {
    const storage = fakeStorage();
    expect(rememberRecentTrip({ id: "trip-a", title: null }, storage)[0]?.title).toBe(
      "未命名行程",
    );
    expect(rememberRecentTrip({ id: "trip-b", title: "   " }, storage)[0]?.title).toBe(
      "未命名行程",
    );
  });

  it("★ 空 id 不写入（否则会多出一条点不开的记录）", () => {
    const storage = fakeStorage(JSON.stringify([entry("trip-a")]));

    const result = rememberRecentTrip({ id: "   ", title: "标题" }, storage);

    expect(result.map((trip) => trip.id)).toEqual(["trip-a"]);
    expect(JSON.parse(storage.raw.get(RECENT_TRIPS_KEY) ?? "null")).toEqual([
      entry("trip-a"),
    ]);
  });

  it("超出上限时丢掉最旧的那条", () => {
    const storage = fakeStorage();
    for (let index = 0; index < RECENT_TRIPS_LIMIT + 3; index += 1) {
      rememberRecentTrip({ id: `trip-${index}`, title: "行程" }, storage);
    }

    const result = readRecentTrips(storage);
    expect(result).toHaveLength(RECENT_TRIPS_LIMIT);
    expect(result[0]?.id).toBe(`trip-${RECENT_TRIPS_LIMIT + 2}`);
    expect(result.map((trip) => trip.id)).not.toContain("trip-0");
  });

  it("★ 写失败（配额满 / 隐私模式）不抛异常，且返回的记录仍然可见", () => {
    const storage: TripStorage = {
      getItem: () => null,
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: () => undefined,
    };

    expect(() => rememberRecentTrip({ id: "trip-a", title: "标题" }, storage)).not.toThrow();
    expect(rememberRecentTrip({ id: "trip-a", title: "标题" }, storage)).toHaveLength(1);
  });

  it("没有存储时不会抛异常；返回值仍是「这次应该看到的那一条」（只是没落盘）", () => {
    expect(() => rememberRecentTrip({ id: "trip-a", title: "标题" }, null)).not.toThrow();
    // 两种"写不下去"（没有存储 / 写失败）返回同一形状的结果：
    // 当前这一屏照常能看到刚建的那一条，用户不会面对一个凭空失败的操作。
    expect(rememberRecentTrip({ id: "trip-a", title: "标题" }, null)).toEqual([
      { id: "trip-a", title: "标题", savedAt: expect.any(String) },
    ]);
  });
});

describe("forgetRecentTrips", () => {
  it("把记录整条删掉", () => {
    const storage = fakeStorage(JSON.stringify([entry("trip-a")]));

    forgetRecentTrips(storage);

    expect(storage.raw.has(RECENT_TRIPS_KEY)).toBe(false);
    expect(readRecentTrips(storage)).toEqual([]);
  });

  it("删除失败（隐私模式）也不会把页面打崩", () => {
    const storage: TripStorage = {
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => {
        throw new Error("SecurityError");
      },
    };

    expect(() => forgetRecentTrips(storage)).not.toThrow();
  });
});

describe("formatSavedAt", () => {
  it("按本地时区显示到分钟（用户看的是墙上的表）", () => {
    // 不带 Z 的 ISO 串按本地时间解析 → 结果与运行机器的时区无关
    expect(formatSavedAt("2026-09-15T22:10:00")).toBe("2026-09-15 22:10");
    expect(formatSavedAt("2026-01-05T09:07:00")).toBe("2026-01-05 09:07");
  });

  it("★ 坏时间戳返回空串（调用方据此不显示时间），而不是 Invalid Date", () => {
    expect(formatSavedAt("")).toBe("");
    expect(formatSavedAt("不是时间")).toBe("");
  });
});

describe("browserTripStorage", () => {
  /**
   * ★ 这里必须自己装一份全局 localStorage ★
   * 本机 Node（26）在没有 `--localstorage-file` 时**把它禁掉**，而 vitest 的 jsdom 环境
   * 不会覆盖 Node 已有的同名全局 —— 实测测试进程里 `typeof localStorage === "undefined"`
   * （`lib/__tests__/dev-api.test.ts` 里也记过同一件事）。所以这一组用例用 `vi.stubGlobal`
   * 显式装/卸，断言才是在测我们的代码而不是测运行环境。
   */
  it("拿得到时原样返回全局 localStorage", () => {
    const storage = fakeStorage();
    vi.stubGlobal("localStorage", storage);

    expect(browserTripStorage()).toBe(storage);
  });

  it("没有 localStorage（SSR / 被禁掉）时返回 null", () => {
    vi.stubGlobal("localStorage", undefined);

    expect(browserTripStorage()).toBeNull();
  });

  it("★ 连读取这个全局都会抛错（隐私模式最凶的一种表现）时也返回 null，而不是把页面打崩", () => {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError: localStorage 被禁用");
      },
    });

    try {
      expect(browserTripStorage()).toBeNull();
    } finally {
      vi.stubGlobal("localStorage", undefined);
    }
  });
});
