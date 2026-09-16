import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PlannerForm, parseExample } from "@/components/planner-form";

// 只替换网络函数，保留真实的 ApiError / NetworkError 类，
// 这样组件里的 instanceof / name 判断走的是同一条路径。
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    planTrip: vi.fn(),
    getHealth: vi.fn(),
  };
});

describe("PlannerForm 默认值", () => {
  it("预填 广州 / 1 天 / 2 人 / 轻松 / 300 每人，可以 0 输入直接提交", () => {
    render(<PlannerForm />);

    expect(screen.getByLabelText("目的地")).toHaveValue("广州");
    expect(screen.getByRole("radio", { name: "1 天" })).toBeChecked();
    expect(screen.getByLabelText("人数")).toHaveValue("2");
    expect(screen.getByRole("radio", { name: "轻松" })).toBeChecked();
    expect(screen.getByLabelText("预算")).toHaveValue("300");
    expect(screen.getByRole("radio", { name: "每人" })).toBeChecked();
    expect(screen.getByLabelText(/补充要求/)).toHaveValue("");
    expect(screen.getByText("0/500 字")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始规划" })).toBeEnabled();
  });

  it("未开放的广州以外城市是禁用选项", () => {
    render(<PlannerForm />);

    const select = screen.getByLabelText("目的地");
    const shenzhen = screen.getByRole("option", { name: "深圳（即将开放）" });
    const chengdu = screen.getByRole("option", { name: "成都（即将开放）" });

    expect(select).toContainElement(shenzhen);
    expect(shenzhen).toBeDisabled();
    expect(chengdu).toBeDisabled();
    expect(screen.getByRole("option", { name: "广州" })).toBeEnabled();
  });
});

describe("PlannerForm 补充要求字数上限", () => {
  it("超过 500 字符会被截断，并给出可见提示", () => {
    render(<PlannerForm />);
    const textarea = screen.getByLabelText(/补充要求/);

    expect(textarea).toHaveAttribute("maxLength", "500");

    // 粘贴 600 字：maxLength 对程序化赋值不生效，靠 onChange 截断兜底
    fireEvent.change(textarea, { target: { value: "广".repeat(600) } });

    expect(textarea).toHaveValue("广".repeat(500));
    expect((textarea as HTMLTextAreaElement).value).toHaveLength(500);
    expect(screen.getByText("500/500 字")).toBeInTheDocument();
    expect(screen.getByText(/已达 500 字上限/)).toBeInTheDocument();
  });

  it("未达上限时不显示提示", () => {
    render(<PlannerForm />);
    const textarea = screen.getByLabelText(/补充要求/);

    fireEvent.change(textarea, { target: { value: "想吃早茶" } });

    expect(textarea).toHaveValue("想吃早茶");
    expect(screen.getByText("4/500 字")).toBeInTheDocument();
    expect(screen.queryByText(/已达 500 字上限/)).not.toBeInTheDocument();
  });
});

describe("PlannerForm 示例一键填入", () => {
  it("点击示例后填好天数、偏好、节奏（并说明填了什么）", async () => {
    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(
      screen.getByRole("button", {
        name: "广州 2 天，情侣，喜欢美食和拍照，不想太累",
      }),
    );

    expect(screen.getByRole("radio", { name: "2 天" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "轻松" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /美食/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /拍照/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /情侣/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /购物/ })).not.toBeChecked();
    expect(screen.getByText(/已按示例填入/)).toBeInTheDocument();
  });

  it("示例里的预算也会被解析出来（每人 200）", async () => {
    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("button", { name: /预算 200\/人/ }));

    expect(screen.getByLabelText("预算")).toHaveValue("200");
    expect(screen.getByRole("radio", { name: "每人" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "1 天" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /CityWalk/ })).toBeChecked();
  });

  it("parseExample 只做能确定的映射，识别不到就保持原值", () => {
    expect(parseExample("广州 2 天，情侣，喜欢美食和拍照，不想太累")).toEqual({
      days: 2,
      daySpan: null,
      people: null,
      preferences: ["美食", "拍照", "情侣"],
      pace: "relaxed",
      budget: null,
    });

    expect(parseExample("随便看看")).toEqual({
      days: null,
      daySpan: null,
      people: null,
      preferences: [],
      pace: null,
      budget: null,
    });

    // 「玩几天」与「每天玩多久」是两个维度，一句话里可以同时出现
    expect(parseExample("广州 2 天，就玩个半天")).toMatchObject({
      days: 2,
      daySpan: "half_day",
    });
    expect(parseExample("想待一整天")).toMatchObject({ daySpan: "full_day" });
    // 「一天玩尽可能多」两者都命中，取更满的那个才符合原意
    expect(parseExample("一天尽可能多逛几个地方")).toMatchObject({
      daySpan: "whole_window",
    });

    expect(parseExample("广州 3 天，亲子，博物馆和自然，节奏适中")).toEqual({
      days: 3,
      daySpan: null,
      people: null,
      preferences: ["亲子", "自然", "博物馆"],
      pace: "balanced",
      budget: null,
    });
  });
});

describe("PlannerForm 可访问性", () => {
  it("偏好用原生 checkbox、节奏用原生 radio，键盘可操作", () => {
    render(<PlannerForm />);

    expect(screen.getByRole("checkbox", { name: /博物馆/ })).toHaveAttribute(
      "type",
      "checkbox",
    );
    expect(screen.getByRole("radio", { name: "紧凑" })).toHaveAttribute(
      "type",
      "radio",
    );
    expect(screen.getByRole("textbox", { name: "人数" })).toHaveAttribute(
      "inputmode",
      "numeric",
    );
    expect(screen.getByRole("textbox", { name: "预算" })).toHaveAttribute(
      "inputmode",
      "numeric",
    );
  });
});
