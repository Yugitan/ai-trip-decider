/**
 * 偏好维度：界面标签 ↔ 接口枚举值（`config/scoring.yaml` 的 `preference_dimensions`）。
 *
 * ★ 为什么单独一个文件 ★ 这张表是**跨语言契约**的一半（另一半在后端配置里，
 * `planner-form-submit.test.tsx` 会拿两份比对）。而「每天的主题」用的是**同一套词表** ——
 * 主题曾经有两种别的解释（方案定位、现成路线名），那两种都得另造一份中文表。
 *
 * 放在 `lib/` 而不是某个组件里：`planner-form`（选偏好/主题）与 `trip-result`
 * （把快照里的主题键显示成中文）都要用它，而前者经由 `trip-workspace` 依赖后者 ——
 * 定义在组件里会形成 import 环。
 */

export const PREFERENCE_OPTIONS: ReadonlyArray<{
  key: string;
  api: string;
  emoji: string;
}> = [
  { key: "美食", api: "food", emoji: "🍜" },
  { key: "拍照", api: "photo", emoji: "📷" },
  { key: "文化", api: "culture", emoji: "🏛️" },
  { key: "夜景", api: "night_view", emoji: "🌃" },
  { key: "亲子", api: "family", emoji: "🧒" },
  { key: "情侣", api: "couple", emoji: "💞" },
  { key: "CityWalk", api: "citywalk", emoji: "🚶" },
  { key: "自然", api: "nature", emoji: "🌿" },
  { key: "购物", api: "shopping", emoji: "🛍️" },
  { key: "博物馆", api: "museum", emoji: "🖼️" },
];

/** 界面标签 → 接口取值；按选项顺序输出，保证同一组选择的 payload 可稳定比对。 */
export function toApiPreferences(labels: readonly string[]): string[] {
  return PREFERENCE_OPTIONS.filter((option) => labels.includes(option.key)).map(
    (option) => option.api,
  );
}

/**
 * 接口取值 → 界面标签。**不认识的值原样返回**：后端将来加一个偏好维度时，
 * 界面显示 `hot_spring` 也好过显示空白 —— 空白会让人以为那一天没设主题。
 */
export function formatPreference(value: string): string {
  return PREFERENCE_OPTIONS.find((option) => option.api === value)?.key ?? value;
}
