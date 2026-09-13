/**
 * LLM 使用情况展示组件的测试。
 *
 * 这里守的是**诚实性**：没配 Key 要说"规则引擎兜底、功能不受影响"，
 * 命中缓存要说"未重复计费"，单价未校准必须标出来 —— 不允许把
 * "看起来用了 AI" 当成卖点糊过去。
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { LlmStatus } from "@/lib/api";
import { LlmSummary, describeTaskSource, taskSummary } from "@/components/llm-summary";

function status(overrides: Partial<LlmStatus> = {}): LlmStatus {
  return {
    enabled: true,
    used: true,
    provider: "deepseek",
    model: "deepseek-flash",
    prompt_version: "2026.09.2",
    calls: 2,
    cache_hits: 0,
    tokens_in: 771,
    tokens_out: 193,
    cost_cny: "0.004",
    cost_calibrated: true,
    tasks: { intent_patch: "llm", route_narrative: "llm" },
    fallback_reasons: [],
    ...overrides,
  };
}

describe("LlmSummary", () => {
  it("没有 meta.llm 时如实说明，而不是留空", () => {
    render(<LlmSummary llm={null} />);
    expect(screen.getByText(/没有带上模型使用信息/)).toBeInTheDocument();
  });

  it("未配置 Key 时说清是规则引擎兜底且功能不受影响", () => {
    render(
      <LlmSummary
        llm={status({
          enabled: false,
          used: false,
          provider: "disabled",
          model: null,
          calls: 0,
          tokens_in: 0,
          tokens_out: 0,
          tasks: { intent_patch: "rule", route_narrative: "rule" },
        })}
      />,
    );
    expect(screen.getByText("本次用规则引擎完成")).toBeInTheDocument();
    expect(screen.getByText(/未配置 LLM API Key/)).toBeInTheDocument();
    expect(screen.getByText(/功能不受影响/)).toBeInTheDocument();
    // 没有真实调用就不该出现成本行
    expect(screen.queryByText(/成本约/)).not.toBeInTheDocument();
  });

  it("展示了模型、调用次数、token 与已校准的成本", () => {
    render(<LlmSummary llm={status()} />);
    expect(screen.getByText(/deepseek-flash/)).toBeInTheDocument();
    expect(screen.getByText(/2 次/)).toBeInTheDocument();
    expect(screen.getByText(/771 入 \/ 193 出/)).toBeInTheDocument();
    expect(screen.getByText(/单价已校准/)).toBeInTheDocument();
  });

  it("未校准的金额必须标注，不能看起来像真实支出", () => {
    render(<LlmSummary llm={status({ cost_calibrated: false, cost_cny: "0" })} />);
    expect(screen.getByText(/单价未校准/)).toBeInTheDocument();
  });

  it("命中缓存时说明未重复计费", () => {
    render(
      <LlmSummary
        llm={status({
          calls: 0,
          cache_hits: 1,
          tokens_in: 0,
          tokens_out: 0,
          tasks: { intent_patch: "llm", result: "cache" },
        })}
      />,
    );
    expect(screen.getByText(/缓存命中 1 次（未重复计费）/)).toBeInTheDocument();
  });

  it("降级原因逐条列出", () => {
    render(
      <LlmSummary
        llm={status({
          used: false,
          calls: 0,
          fallback_reasons: ["PROVIDER_TIMEOUT（已降级到规则引擎/模板文案）"],
        })}
      />,
    );
    expect(screen.getByText(/已降级：PROVIDER_TIMEOUT/)).toBeInTheDocument();
  });
});

describe("任务归属文案", () => {
  it("把机器码翻译成人话", () => {
    expect(describeTaskSource("llm")).toBe("模型");
    expect(describeTaskSource("cache")).toBe("模型（命中缓存）");
    expect(describeTaskSource("rule")).toBe("规则引擎");
    expect(describeTaskSource("weird")).toBe("weird");
  });

  it("过滤掉内部 result 键，未调用时明说", () => {
    expect(taskSummary({ intent_patch: "llm", route_narrative: "rule" })).toBe(
      "意图补全：模型 · 方案文案：规则引擎",
    );
    expect(taskSummary({ result: "cache" })).toBe("本次未调用");
    expect(taskSummary({})).toBe("本次未调用");
  });
});
