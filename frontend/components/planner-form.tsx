"use client";

import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";

import { planTrip, type PlanRequest } from "@/lib/api";
import {
  subscribeToPlan,
  type PlanCompletedEvent,
  type PlanFailedEvent,
  type PlanProgressEvent,
} from "@/lib/plan-stream";
import { LlmSummary } from "@/components/llm-summary";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ChipButton, ChipToggle } from "@/components/ui/chip";
import { Segmented, type SegmentedOption } from "@/components/ui/segmented";

export const FREE_TEXT_LIMIT = 500;

const DEFAULT_CITY = "广州";
const DEFAULT_DAYS = 1;
const DEFAULT_PEOPLE = 2;
const DEFAULT_BUDGET = 300;

type Pace = PlanRequest["pace"];
type BudgetScope = NonNullable<PlanRequest["budget"]>["scope"];

/**
 * 界面标签 ↔ 接口取值。
 *
 * 这是一个**真实的坑**：后端收的是枚举 key（`guangzhou` / `food`），界面显示的是中文。
 * 早期版本把中文标签直接当 payload 发出去，于是点「开始规划」稳定 422，
 * 而后端其实完全正常 —— 错误只在两端语义不同。
 *
 * 因此这里把两列写在同一个字面量里（而不是靠某个 `find()` 兼做转换）：
 * 新增一个城市/偏好时，`label` 与 `api` 必须一起给，漏了就有一条测试会红
 * （见 `components/__tests__/planner-form-submit.test.tsx` 里与 `config/scoring.yaml` 对照的那条）。
 */
const CITY_OPTIONS: ReadonlyArray<{ label: string; api: string; open: boolean }> = [
  { label: "广州", api: "guangzhou", open: true },
  // 未开放的城市**没有**接口取值：宁可让选项 disabled，也不要发一个后端不认的值
  { label: "深圳", api: "", open: false },
  { label: "成都", api: "", open: false },
];

const DAY_OPTIONS: readonly SegmentedOption<number>[] = [
  { value: 1, label: "1 天" },
  { value: 2, label: "2 天" },
  { value: 3, label: "3 天" },
];

const PACE_OPTIONS: readonly SegmentedOption<Pace>[] = [
  { value: "relaxed", label: "轻松" },
  { value: "balanced", label: "适中" },
  { value: "packed", label: "紧凑" },
];

const SCOPE_OPTIONS: readonly SegmentedOption<BudgetScope>[] = [
  { value: "per_person", label: "每人" },
  { value: "total", label: "总计" },
];

/**
 * 偏好选项：`key` 是界面标签（也是组件状态里存的值），`api` 是接口枚举值。
 * `api` 必须与后端 `config/scoring.yaml` 的 `preference_dimensions` 键**完全一致**
 * （不是超集、也不是子集）：少一个 → 用户选了就 422；多一个 → 后端不认。
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

/** 一条示例必须能「点一下就直接提交」，所以只做结构化字段的映射。 */
const EXAMPLES: readonly string[] = [
  "广州 2 天，情侣，喜欢美食和拍照，不想太累",
  "广州 3 天，亲子，博物馆和自然，节奏适中",
  "广州 1 天，CityWalk 加夜景，预算 200/人",
];

const LABEL_CLASS = "block text-sm font-medium text-ink-soft";
const CONTROL_CLASS =
  "min-h-11 w-full rounded-btn border border-line bg-shell px-3 text-base text-ink placeholder:text-ink-faint focus:border-teal";

const CN_NUMERALS: Record<string, number> = {
  一: 1,
  两: 2,
  二: 2,
  三: 3,
  四: 4,
  五: 5,
  六: 6,
  七: 7,
  八: 8,
  九: 9,
  十: 10,
};

const NUMBER_TOKEN = "([0-9一二两三四五六七八九十]+)";

const EXAMPLE_KEYWORDS: ReadonlyArray<{
  key: string;
  patterns: readonly string[];
}> = [
  { key: "美食", patterns: ["美食", "小吃", "吃"] },
  { key: "拍照", patterns: ["拍照", "摄影", "出片"] },
  { key: "文化", patterns: ["文化", "历史", "老城"] },
  { key: "夜景", patterns: ["夜景", "夜游", "灯光", "晚上"] },
  { key: "亲子", patterns: ["亲子", "带娃", "小朋友", "孩子"] },
  { key: "情侣", patterns: ["情侣", "约会"] },
  { key: "CityWalk", patterns: ["citywalk", "city walk", "漫步", "走走", "徒步"] },
  { key: "自然", patterns: ["自然", "公园", "爬山", "绿道"] },
  { key: "购物", patterns: ["购物", "逛街"] },
  { key: "博物馆", patterns: ["博物馆", "看展", "展览"] },
];

const EXAMPLE_PACES: ReadonlyArray<{
  pace: Pace;
  patterns: readonly string[];
}> = [
  {
    pace: "relaxed",
    patterns: ["不想太累", "轻松", "休闲", "慢", "别太赶", "relaxed"],
  },
  { pace: "packed", patterns: ["紧凑", "满满", "多去", "赶", "packed"] },
  { pace: "balanced", patterns: ["适中", "正常", "随意", "balanced"] },
];

export interface ExampleDraft {
  days: number | null;
  people: number | null;
  preferences: string[];
  pace: Pace | null;
  budget: { amount: number; scope: BudgetScope } | null;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function digitsOnly(raw: string): string {
  return raw.replace(/[^0-9]/g, "");
}

function resolveCount(
  raw: string,
  options: { fallback: number; min: number; max: number },
): number {
  const digits = digitsOnly(raw);
  if (digits === "") return options.fallback;
  const parsed = Number.parseInt(digits, 10);
  if (Number.isNaN(parsed)) return options.fallback;
  return clamp(parsed, options.min, options.max);
}

function toInteger(token: string | undefined): number | null {
  if (token === undefined) return null;
  if (/^[0-9]+$/.test(token)) return Number.parseInt(token, 10);
  const value = CN_NUMERALS[token];
  return value === undefined ? null : value;
}

/**
 * 把一句自然语言示例映射成结构化字段。
 * 只做「能确定」的映射：识别不到就保持原值，不猜。
 */
export function parseExample(text: string): ExampleDraft {
  const compact = text.toLowerCase().replace(/\s+/g, "");

  const daysToken = new RegExp(`${NUMBER_TOKEN}天`).exec(compact);
  const peopleToken = new RegExp(`${NUMBER_TOKEN}人`).exec(compact);
  const budgetToken = /预算([0-9]+)/.exec(compact);

  const matchedKeys = new Set<string>();
  for (const group of EXAMPLE_KEYWORDS) {
    if (group.patterns.some((pattern) => compact.includes(pattern))) {
      matchedKeys.add(group.key);
    }
  }

  let pace: Pace | null = null;
  for (const candidate of EXAMPLE_PACES) {
    if (pace === null && candidate.patterns.some((p) => compact.includes(p))) {
      pace = candidate.pace;
    }
  }

  const budgetAmount = toInteger(budgetToken?.[1]);
  const budget =
    budgetAmount === null
      ? null
      : {
          amount: clamp(budgetAmount, 0, 1_000_000),
          scope: (compact.includes("/人") || compact.includes("每人")
            ? "per_person"
            : "total") as BudgetScope,
        };

  return {
    days: toInteger(daysToken?.[1]),
    people: toInteger(peopleToken?.[1]),
    // 按选项顺序输出，保证 payload 稳定可比对
    preferences: PREFERENCE_OPTIONS.filter((option) =>
      matchedKeys.has(option.key),
    ).map((option) => option.key),
    pace,
    budget,
  };
}

interface Failure {
  kind: "network" | "api" | "unknown";
  status: number | null;
  code: string | null;
  message: string;
  hint: string | null;
  requestId: string | null;
}

function describeFailure(error: unknown): Failure {
  if (typeof error === "object" && error !== null) {
    const candidate = error as {
      name?: unknown;
      code?: unknown;
      status?: unknown;
      message?: unknown;
      hint?: unknown;
      requestId?: unknown;
    };

    if (candidate.name === "NetworkError") {
      return {
        kind: "network",
        status: null,
        code: null,
        message: "无法连接到规划服务。",
        hint: "在项目根目录运行 make dev-backend 启动后端后重试。",
        requestId: null,
      };
    }

    if (typeof candidate.code === "string") {
      return {
        kind: "api",
        status: typeof candidate.status === "number" ? candidate.status : null,
        code: candidate.code,
        message:
          typeof candidate.message === "string"
            ? candidate.message
            : "后端没有接受这次请求。",
        hint:
          typeof candidate.hint === "string" && candidate.hint.length > 0
            ? candidate.hint
            : null,
        requestId:
          typeof candidate.requestId === "string" ? candidate.requestId : null,
      };
    }
  }

  return {
    kind: "unknown",
    status: null,
    code: null,
    message: "发生了未预期的错误，请重试。",
    hint: null,
    requestId: null,
  };
}

/** 接口尚不存在时框架返回的状态码：404 未知路径 / 405 方法不允许 / 501 未实现。 */
const NOT_IMPLEMENTED_STATUSES: ReadonlySet<number> = new Set([404, 405, 501]);

/** 请求被**拒绝**（不是"没实现"）的状态码：400 请求体不合法 / 422 校验失败。 */
const REJECTED_STATUSES: ReadonlySet<number> = new Set([400, 422]);

/**
 * 诚实的上下文说明：把「地址/服务不对」「输入被拒绝」「其它失败」三种情况分开说。
 *
 * ★ 为什么不再写「引擎正在开发中」★
 * M4 已完成，规划接口是真实存在的 —— 此时再收到 404/405/501，
 * 真实原因几乎一定是**后端没启动、版本太旧或地址配错了**。
 * 继续沿用旧文案会把排障引到完全错误的方向（"等”一个已经上线的功能）。
 *
 * ★ 同样刻意区分 400/422 ★
 * 早期版本把 400/422 也归进"引擎开发中"，并附上一句"需求已被前端完整校验" ——
 * 但 422 恰恰意味着**后端认为这份需求不合法**，那句话就成了误导。
 */
function engineNote(failure: Failure): string {
  if (failure.kind === "network") {
    return "后端服务未启动时无法提交规划请求；请求内容已保留在下面，启动后端后可以直接重试。";
  }
  if (failure.status !== null && NOT_IMPLEMENTED_STATUSES.has(failure.status)) {
    return `后端没有这个接口（HTTP ${failure.status}）：通常是后端没启动、版本过旧，或前端配的地址不对。下面是刚刚发出去的请求内容，便于对照：`;
  }
  if (failure.status !== null && REJECTED_STATUSES.has(failure.status)) {
    return `后端认为这份需求不合法（HTTP ${failure.status}），请按上面的提示调整后重试。下面是刚刚发出去的请求内容，便于对照：`;
  }
  return "这次请求没有被后端接受（既不是「地址不对」也不是「输入不合法」）。下面是刚刚发出去的请求内容，便于对照：";
}

type Submission =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "failed"; failure: Failure }
  | { kind: "accepted"; requestId: string; streamUrl: string };

export function PlannerForm() {
  const [city, setCity] = useState(DEFAULT_CITY);
  const [days, setDays] = useState(DEFAULT_DAYS);
  const [peopleText, setPeopleText] = useState(String(DEFAULT_PEOPLE));
  const [preferences, setPreferences] = useState<string[]>([]);
  const [pace, setPace] = useState<Pace>("relaxed");
  const [budgetText, setBudgetText] = useState(String(DEFAULT_BUDGET));
  const [budgetScope, setBudgetScope] = useState<BudgetScope>("per_person");
  const [freeText, setFreeText] = useState("");
  const [submission, setSubmission] = useState<Submission>({ kind: "idle" });
  const [outgoing, setOutgoing] = useState<PlanRequest | null>(null);
  const [exampleNote, setExampleNote] = useState<string | null>(null);

  const people = resolveCount(peopleText, {
    fallback: DEFAULT_PEOPLE,
    min: 1,
    max: 20,
  });
  const budgetDigits = digitsOnly(budgetText);
  const budgetAmount = resolveCount(budgetText, {
    fallback: DEFAULT_BUDGET,
    min: 0,
    max: 1_000_000,
  });

  const atFreeTextLimit = freeText.length >= FREE_TEXT_LIMIT;
  const isSubmitting = submission.kind === "submitting";

  function buildPayload(): PlanRequest {
    // ⚠️ 发出去的是**接口取值**，不是界面上那两个字：中文标签只活在界面状态里
    const cityOption = CITY_OPTIONS.find((option) => option.label === city);
    return {
      city: cityOption?.api ?? city,
      days,
      people,
      preferences: toApiPreferences(preferences),
      pace,
      budget:
        budgetDigits === ""
          ? null
          : { amount: budgetAmount, scope: budgetScope },
      free_text: freeText.trim(),
    };
  }

  function togglePreference(key: string, next: boolean) {
    setPreferences((current) => {
      const kept = current.filter((item) => item !== key);
      if (!next) return kept;
      return PREFERENCE_OPTIONS.filter(
        (option) => option.key === key || kept.includes(option.key),
      ).map((option) => option.key);
    });
  }

  function applyExample(example: string) {
    const draft = parseExample(example);
    const filled: string[] = [DEFAULT_CITY];

    setCity(DEFAULT_CITY);
    if (draft.days !== null) {
      setDays(clamp(draft.days, 1, 3));
      filled.push(`${clamp(draft.days, 1, 3)} 天`);
    }
    if (draft.people !== null) {
      setPeopleText(String(clamp(draft.people, 1, 20)));
      filled.push(`${clamp(draft.people, 1, 20)} 人`);
    }
    if (draft.preferences.length > 0) {
      setPreferences(draft.preferences);
      filled.push(draft.preferences.join(" + "));
    }
    if (draft.pace !== null) {
      setPace(draft.pace);
      const label = PACE_OPTIONS.find(
        (option) => option.value === draft.pace,
      )?.label;
      filled.push(label === undefined ? draft.pace : label);
    }
    if (draft.budget !== null) {
      setBudgetText(String(draft.budget.amount));
      setBudgetScope(draft.budget.scope);
      filled.push(
        `${draft.budget.amount} 元${
          draft.budget.scope === "per_person" ? "/人" : "总计"
        }`,
      );
    }

    setExampleNote(`已按示例填入：${filled.join(" · ")}`);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSubmitting) return;

    const payload = buildPayload();
    // 先把真正发出去的 payload 落到界面上：即使请求失败也有据可查
    setOutgoing(payload);
    setSubmission({ kind: "submitting" });

    try {
      const result = await planTrip(payload);
      setSubmission({
        kind: "accepted",
        requestId: result.data.request_id,
        streamUrl: result.data.stream_url,
      });
    } catch (error: unknown) {
      setSubmission({ kind: "failed", failure: describeFailure(error) });
    }
  }

  return (
    <Card className="overflow-hidden shadow-lift">
      <form onSubmit={handleSubmit} aria-labelledby="planner-title">
        <div className="border-b border-line px-5 py-5 sm:px-7 sm:py-6">
          <h2
            id="planner-title"
            className="text-xl font-semibold tracking-tight text-ink sm:text-2xl"
          >
            先说说你想怎么玩
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-ink-soft">
            默认值已经替你填好了 —— 什么都不改也能直接提交，之后再慢慢调。
          </p>
        </div>

        <div className="grid gap-6 px-5 py-6 sm:grid-cols-2 sm:px-7">
          <div>
            <label htmlFor="planner-city" className={LABEL_CLASS}>
              目的地
            </label>
            <div className="relative mt-2">
              <select
                id="planner-city"
                name="city"
                value={city}
                onChange={(event) => setCity(event.currentTarget.value)}
                className={`${CONTROL_CLASS} appearance-none pr-11`}
              >
                {CITY_OPTIONS.map((option) => (
                  <option
                    key={option.label}
                    value={option.label}
                    disabled={!option.open}
                  >
                    {option.open ? option.label : `${option.label}（即将开放）`}
                  </option>
                ))}
              </select>
              <svg
                aria-hidden="true"
                viewBox="0 0 24 24"
                className="pointer-events-none absolute top-1/2 right-3.5 size-4 -translate-y-1/2 text-ink-faint"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M6 9l6 6 6-6" />
              </svg>
            </div>
          </div>

          <Segmented
            name="planner-days"
            legend="天数"
            value={days}
            options={DAY_OPTIONS}
            onChange={setDays}
          />

          <div>
            <label htmlFor="planner-people" className={LABEL_CLASS}>
              人数
            </label>
            <div className="relative mt-2">
              <input
                id="planner-people"
                name="people"
                type="text"
                inputMode="numeric"
                pattern="[0-9]*"
                autoComplete="off"
                value={peopleText}
                onChange={(event) =>
                  setPeopleText(digitsOnly(event.currentTarget.value))
                }
                aria-describedby="planner-people-hint"
                className={`${CONTROL_CLASS} tnum pr-11`}
              />
              <span
                aria-hidden="true"
                className="pointer-events-none absolute top-1/2 right-3.5 -translate-y-1/2 text-sm text-ink-faint"
              >
                人
              </span>
            </div>
            <p id="planner-people-hint" className="mt-1.5 text-xs text-ink-faint">
              {digitsOnly(peopleText) === ""
                ? "留空按 2 人计算，不会阻止你提交。"
                : "1–20 人，超出范围会自动收敛。"}
            </p>
          </div>

          <Segmented
            name="planner-pace"
            legend="节奏"
            value={pace}
            options={PACE_OPTIONS}
            onChange={setPace}
          />

          <div className="sm:col-span-2">
            <label htmlFor="planner-budget" className={LABEL_CLASS}>
              预算
            </label>
            <div className="mt-2 grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,18rem)] sm:items-center">
              <div className="relative">
                <input
                  id="planner-budget"
                  name="budget"
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  autoComplete="off"
                  value={budgetText}
                  onChange={(event) =>
                    setBudgetText(digitsOnly(event.currentTarget.value))
                  }
                  aria-describedby="planner-budget-hint"
                  className={`${CONTROL_CLASS} tnum pr-11`}
                />
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute top-1/2 right-3.5 -translate-y-1/2 text-sm text-ink-faint"
                >
                  元
                </span>
              </div>
              <Segmented
                name="planner-budget-scope"
                legend="预算口径"
                hideLegend
                value={budgetScope}
                options={SCOPE_OPTIONS}
                onChange={setBudgetScope}
              />
            </div>
            <p id="planner-budget-hint" className="mt-1.5 text-xs text-ink-faint">
              {budgetDigits === ""
                ? "留空表示不限预算，会把「不限」原样发给后端。"
                : `当前口径：${
                    budgetScope === "per_person" ? "每人" : "全部人合计"
                  }。`}
            </p>
          </div>

          <fieldset className="sm:col-span-2">
            <legend className={LABEL_CLASS}>
              偏好（可多选，也可以一个都不选）
            </legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {PREFERENCE_OPTIONS.map((option) => (
                <ChipToggle
                  key={option.key}
                  id={`planner-pref-${option.key}`}
                  name="planner-preferences"
                  label={option.key}
                  emoji={option.emoji}
                  checked={preferences.includes(option.key)}
                  onChange={(next) => togglePreference(option.key, next)}
                />
              ))}
            </div>
          </fieldset>

          <div className="sm:col-span-2">
            <label htmlFor="planner-free-text" className={LABEL_CLASS}>
              补充要求（可选）
            </label>
            <textarea
              id="planner-free-text"
              name="free_text"
              rows={4}
              maxLength={FREE_TEXT_LIMIT}
              value={freeText}
              onChange={(event: ChangeEvent<HTMLTextAreaElement>) =>
                setFreeText(event.currentTarget.value.slice(0, FREE_TEXT_LIMIT))
              }
              aria-describedby="planner-free-text-count"
              placeholder="例如：带爸妈，走不了太多路；一定要吃到早茶；下雨也没关系。"
              className="mt-2 w-full rounded-btn border border-line bg-shell px-3 py-2.5 text-base leading-relaxed text-ink placeholder:text-ink-faint focus:border-teal"
            />
            <p
              id="planner-free-text-count"
              className="tnum mt-1.5 text-xs text-ink-faint"
            >
              {freeText.length}/{FREE_TEXT_LIMIT} 字
            </p>
            <div aria-live="polite" className="mt-1">
              {atFreeTextLimit ? (
                <p className="text-xs font-medium text-coral">
                  已达 {FREE_TEXT_LIMIT} 字上限，超出的内容没有被记录，也不会发送。
                </p>
              ) : null}
            </div>
          </div>
        </div>

        <div className="border-t border-line bg-sand/60 px-5 py-5 sm:px-7">
          <p className="text-sm font-medium text-ink">没想好？点一个示例：</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {EXAMPLES.map((example) => (
              <ChipButton key={example} onClick={() => applyExample(example)}>
                {example}
              </ChipButton>
            ))}
          </div>
          <div aria-live="polite" className="mt-3">
            {exampleNote === null ? null : (
              <p className="text-xs text-teal-dark">{exampleNote}</p>
            )}
          </div>

          <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <Button type="submit" loading={isSubmitting} block className="sm:w-auto">
              {isSubmitting ? "正在提交…" : "开始规划"}
            </Button>
            <p className="text-xs leading-relaxed text-ink-faint">
              提交只会把上面这份需求发给自己的后端；前端不保存任何密钥。
            </p>
          </div>
        </div>

        <div role="status" aria-live="polite">
          {isSubmitting ? (
            <p className="animate-stage border-t border-line px-5 py-4 text-sm text-ink-soft sm:px-7">
              正在把这份需求发给规划服务…
            </p>
          ) : null}
          {submission.kind === "failed" ? (
            <FailurePanel failure={submission.failure} />
          ) : null}
          {submission.kind === "accepted" ? (
            <AcceptedPanel
              requestId={submission.requestId}
              streamUrl={submission.streamUrl}
            />
          ) : null}
        </div>

        {outgoing === null ? null : (
          <div className="border-t border-line px-5 py-5 sm:px-7">
            <p className="text-xs font-medium text-ink-soft">
              即将发给后端的请求内容（POST /api/v1/trips:plan）
            </p>
            <pre
              data-testid="plan-payload"
              className="mt-2 max-w-full overflow-x-auto rounded-btn border border-line bg-sand p-3 text-xs leading-relaxed text-ink"
            >
              <code>{JSON.stringify(outgoing, null, 2)}</code>
            </pre>
          </div>
        )}
      </form>
    </Card>
  );
}

function FailurePanel({ failure }: { failure: Failure }) {
  const title =
    failure.kind === "network"
      ? "未连接到后端服务"
      : failure.kind === "api"
        ? "后端没有接受这次规划请求"
        : "规划请求失败";

  return (
    <div className="border-t border-coral/40 bg-coral-tint/70 px-5 py-4 sm:px-7">
      <p className="text-sm font-semibold text-ink">{title}</p>
      <p className="tnum mt-1.5 text-xs text-ink-soft">
        {failure.status === null ? "无 HTTP 状态" : `HTTP ${failure.status}`}
        {failure.code === null ? "" : ` · 错误码 ${failure.code}`}
        {failure.requestId === null
          ? " · request_id 未返回"
          : ` · request_id ${failure.requestId}`}
      </p>
      <p className="mt-1.5 text-xs leading-relaxed text-ink-soft">
        {failure.message}
      </p>
      {failure.hint === null ? null : (
        <p className="mt-1 text-xs leading-relaxed text-ink-soft">
          后端提示：{failure.hint}
        </p>
      )}
      <p className="mt-2 text-xs leading-relaxed text-ink-soft">
        {engineNote(failure)}
      </p>
    </div>
  );
}

function AcceptedPanel({
  requestId,
  streamUrl,
}: {
  requestId: string;
  streamUrl: string;
}) {
  const [progress, setProgress] = useState<PlanProgressEvent | null>(null);
  const [completed, setCompleted] = useState<PlanCompletedEvent | null>(null);
  const [failed, setFailed] = useState<PlanFailedEvent | null>(null);
  const [streamBroken, setStreamBroken] = useState(false);

  // 订阅进度流。卸载时必须取消：否则组件已卸载还在 setState。
  useEffect(() => {
    return subscribeToPlan(streamUrl, {
      onProgress: (event) => setProgress(event),
      onCompleted: (event) => setCompleted(event),
      onFailed: (event) => setFailed(event),
      onTransportError: () => setStreamBroken(true),
    });
  }, [streamUrl]);

  return (
    <div className="border-t border-teal/30 bg-teal-tint px-5 py-4 sm:px-7">
      <p className="text-sm font-semibold text-ink">后端已接受这次规划请求</p>
      <p className="tnum mt-1.5 text-xs text-ink-soft">
        request_id：{requestId}
      </p>

      {failed !== null ? (
        <div className="mt-2 rounded-[10px] border border-coral/40 bg-coral-tint/70 px-3 py-2.5">
          <p className="text-xs font-medium text-ink">规划没有完成</p>
          <p className="mt-1 text-xs leading-relaxed text-ink-soft">
            {failed.message}
            <span className="mx-1 text-ink-faint">·</span>
            错误码 {failed.code}
          </p>
          {failed.hint === "" ? null : (
            <p className="mt-1 text-xs leading-relaxed text-ink-soft">提示：{failed.hint}</p>
          )}
        </div>
      ) : null}

      {completed !== null ? (
        <div className="mt-2 space-y-2">
          <p className="tnum text-xs leading-relaxed text-ink-soft">
            已生成 {completed.route_count} 套方案
            {completed.cached ? "（命中缓存，未重新计算）" : ""}
          </p>
          <LlmSummary llm={completed.llm} />
          {completed.degraded_modes.length > 0 ? (
            <p className="text-xs leading-relaxed text-ink-soft">
              当前降级模式：{completed.degraded_modes.join("；")}
            </p>
          ) : null}
          <p className="text-xs leading-relaxed text-ink-soft">
            完整路线卡片（地图 · 可修改 · 可分享）属于 M5 —— 这里不先给你一份假行程。
          </p>
        </div>
      ) : null}

      {completed === null && failed === null ? (
        <p className="tnum mt-2 text-xs leading-relaxed text-ink-soft">
          {streamBroken
            ? "进度流已断开（后端可能重启了）。可以重新提交，缓存命中时不会重复计费。"
            : progress === null
              ? `正在等待规划结果…（数据流：${streamUrl}）`
              : `正在规划：${progress.label}（${progress.pct}%）`}
        </p>
      ) : null}
    </div>
  );
}
