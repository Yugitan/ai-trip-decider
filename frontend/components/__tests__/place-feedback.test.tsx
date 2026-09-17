/**
 * 「报告错误」入口的测试（PRD FR-11.5 AC-11.5）。
 *
 * 守三件事：
 * 1. `place_id` 由卡片带过去 —— 用户不需要自己说清\"是哪个地点\"；
 * 2. 类别必填、留言可选（强制留言会让人直接离开）；
 * 3. 回执用**后端给的**那句话，前端不另写一个承诺。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FEEDBACK_CATEGORY_LABELS, PlaceFeedback } from "@/components/place-feedback";
import { ApiError, type FeedbackInput } from "@/lib/api";

const submitMock = vi.fn<
  (input: FeedbackInput) => Promise<{ data: { id: string; status: string; note: string }; meta: never }>
>();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, submitFeedback: (input: FeedbackInput) => submitMock(input) };
});

const NOTE = "已记录。数据问题会在人工复核时核对来源后修正，不会自动覆盖知识库。";

beforeEach(() => {
  submitMock.mockReset();
});

describe("PlaceFeedback", () => {
  it("默认只显示一个入口按钮（浏览页的主线不是报错）", () => {
    render(<PlaceFeedback placeId="p1" placeName="广州塔" />);
    expect(screen.getByTestId("place-feedback-open-p1")).toBeInTheDocument();
    expect(screen.queryByTestId("place-feedback-submit-p1")).not.toBeInTheDocument();
  });

  it("打开后带出地点名与全部类别，并把 place_id 一起提交", async () => {
    submitMock.mockResolvedValue({ data: { id: "f1", status: "open", note: NOTE }, meta: undefined as never });
    render(<PlaceFeedback placeId="p1" placeName="广州塔" />);

    await userEvent.click(screen.getByTestId("place-feedback-open-p1"));
    expect(screen.getByText(/要报告「广州塔」的哪一类问题/)).toBeInTheDocument();

    const select = screen.getByTestId("place-feedback-category-p1");
    for (const label of Object.values(FEEDBACK_CATEGORY_LABELS)) {
      expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
    }
    await userEvent.selectOptions(select, "wrong_price");
    await userEvent.type(screen.getByTestId("place-feedback-message-p1"), " 门票是 150 不是 15 ");
    await userEvent.click(screen.getByTestId("place-feedback-submit-p1"));

    expect(submitMock).toHaveBeenCalledWith({
      category: "wrong_price",
      message: "门票是 150 不是 15",
      place_id: "p1",
    });
    // 回执是后端那句话；前端不另写一句承诺
    expect(await screen.findByTestId("place-feedback-sent-p1")).toHaveTextContent(NOTE);
  });

  it("不写留言也能提交（类别就够定位问题了），且 message 传 null 而不是空串", async () => {
    submitMock.mockResolvedValue({ data: { id: "f2", status: "open", note: NOTE }, meta: undefined as never });
    render(<PlaceFeedback placeId="p1" placeName="广州塔" />);

    await userEvent.click(screen.getByTestId("place-feedback-open-p1"));
    await userEvent.click(screen.getByTestId("place-feedback-submit-p1"));

    expect(submitMock).toHaveBeenCalledWith({ category: "wrong_hours", message: null, place_id: "p1" });
  });

  it("后端拒绝时显示后端的人话与提示（不是一句\"提交失败\"）", async () => {
    submitMock.mockRejectedValue(
      new ApiError(
        { code: "RATE_LIMITED", message: "上报太频繁了，请稍后再试", hint: "这一小时内最多 10 次。", context: {} },
        429,
        null,
      ),
    );
    render(<PlaceFeedback placeId="p1" placeName="广州塔" />);

    await userEvent.click(screen.getByTestId("place-feedback-open-p1"));
    await userEvent.click(screen.getByTestId("place-feedback-submit-p1"));

    const failed = await screen.findByTestId("place-feedback-failed-p1");
    expect(failed.textContent).toContain("上报太频繁了，请稍后再试");
    expect(failed.textContent).toContain("这一小时内最多 10 次。");
    // 失败后表单仍在，用户可以改一改再试
    expect(screen.getByTestId("place-feedback-submit-p1")).toBeInTheDocument();
  });

  it("非 HTTP 失败（断网）时说的是\"没能送出去\"，并指向可执行的下一步", async () => {
    submitMock.mockRejectedValue(new Error("boom"));
    render(<PlaceFeedback placeId="p1" placeName="广州塔" />);

    await userEvent.click(screen.getByTestId("place-feedback-open-p1"));
    await userEvent.click(screen.getByTestId("place-feedback-submit-p1"));

    const failed = await screen.findByTestId("place-feedback-failed-p1");
    expect(failed.textContent).toContain("没能把这条反馈送出去");
    expect(failed.textContent).toContain("确认后端已启动");
  });

  it("取消就收起表单，且不会顺手提交一次", async () => {
    render(<PlaceFeedback placeId="p1" placeName="广州塔" />);
    await userEvent.click(screen.getByTestId("place-feedback-open-p1"));
    await userEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(screen.queryByTestId("place-feedback-submit-p1")).not.toBeInTheDocument();
    expect(submitMock).not.toHaveBeenCalled();
  });

  it("类别标签与后端枚举一一对应（漏一个就会有一个选项永远选不到）", () => {
    expect(Object.keys(FEEDBACK_CATEGORY_LABELS).sort()).toEqual([
      "bad_route",
      "closed",
      "other",
      "wrong_coord",
      "wrong_hours",
      "wrong_price",
    ]);
  });
});
