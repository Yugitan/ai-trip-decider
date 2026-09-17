/**
 * 偏好词表（`lib/preferences.ts`）的单元测试。
 *
 * 这张表有两处容易出错、且出错时**看不出来**：
 *
 * 1. `formatPreference` 的兜底分支。后端将来加一个偏好维度（比如 `hot_spring`）时，
 *    界面要么显示 `hot_spring`、要么显示空白。空白会被读成「那一天没设主题」——
 *    和「设了一个我不认识的主题」是两件事，所以这里钉住「不认识就原样返回」。
 * 2. `toApiPreferences` 的输出顺序。它决定 `params_hash`，而 hash 决定会不会
 *    复用缓存。同一组选择如果顺序随勾选先后变化，就会把同一份行程算成两份。
 *    （与后端配置的词表是否一致由 `planner-form-submit.test.tsx` 比对，此处不重复。）
 */

import { describe, expect, it } from "vitest";

import {
  PREFERENCE_OPTIONS,
  formatPreference,
  toApiPreferences,
} from "@/lib/preferences";

describe("formatPreference", () => {
  it("认识的接口取值显示成界面标签", () => {
    expect(formatPreference("food")).toBe("美食");
    expect(formatPreference("night_view")).toBe("夜景");
  });

  it("不认识的取值原样返回，而不是空字符串", () => {
    // 空字符串会让「有主题但界面不认识」看起来像「没有主题」。
    expect(formatPreference("hot_spring")).toBe("hot_spring");
    expect(formatPreference("")).toBe("");
  });
});

describe("toApiPreferences", () => {
  it("按界面标签转成接口取值", () => {
    expect(toApiPreferences(["美食", "文化"])).toEqual(["food", "culture"]);
  });

  it("输出顺序跟选项顺序走，不跟勾选先后走", () => {
    // 两串输入是同一组选择的两种勾选顺序，payload 必须一致，否则缓存命中率白白掉一半。
    expect(toApiPreferences(["博物馆", "美食"])).toEqual(
      toApiPreferences(["美食", "博物馆"]),
    );
    expect(toApiPreferences(["博物馆", "美食"])).toEqual(["food", "museum"]);
  });

  it("不认识的标签直接丢掉，不编造一个取值", () => {
    expect(toApiPreferences(["美食", "泡温泉"])).toEqual(["food"]);
  });

  it("空选择是空数组", () => {
    expect(toApiPreferences([])).toEqual([]);
  });
});

describe("PREFERENCE_OPTIONS", () => {
  it("每个选项都有标签、接口取值和表情，且接口取值不重复", () => {
    for (const option of PREFERENCE_OPTIONS) {
      expect(option.key).not.toBe("");
      expect(option.api).toMatch(/^[a-z_]+$/);
      expect(option.emoji).not.toBe("");
    }
    const apis = PREFERENCE_OPTIONS.map((option) => option.api);
    expect(new Set(apis).size).toBe(apis.length);
  });
});
