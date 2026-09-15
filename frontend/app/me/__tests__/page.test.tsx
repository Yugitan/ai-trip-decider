/**
 * `/me` 我的行程页的测试。
 *
 * 这一页的价值在两处，且都不是"排版"：
 * 1. **空状态必须是友好的**（PRD E2E-15）：没有记录时说清楚"这里为什么是空的"并给一条
 *    能走出去的路（去规划），而不是一个错误提示或一片白；
 * 2. **本地记录的正确读取与清空**：记录是 localStorage 里的一段用户可编辑的 JSON，
 *    坏数据要退化成空状态，清空按钮既要清掉存储也要立刻反映到界面上。
 *
 * 用真实的 jsdom localStorage（而不是 mock 掉 `lib/recent-trips`）：
 * 这一页与存储之间的接线（键名、序列化格式）正是最容易接错的地方，
 * mock 掉之后"键写岔了"这种 bug 就永远不会被发现。
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import MyTripsPage from "@/app/me/page";
import { RECENT_TRIPS_KEY } from "@/lib/recent-trips";

/**
 * ★ 为什么要自己装一份 localStorage ★
 * 本机 Node（26）在没有 `--localstorage-file` 时把 localStorage 禁掉，
 * 而 vitest 的 jsdom 环境不覆盖 Node 已有的同名全局 —— 测试进程里它就是 undefined
 * （见 `lib/__tests__/recent-trips.test.ts` 的同一条备注）。这里补一份符合 Storage 契约的
 * 内存实现，让「页面 ↔ localStorage」这条接线真实地走一遍。
 *
 * 仍然保留真实存储、而不是 mock 掉 `lib/recent-trips`：这一页与存储之间的键名与
 * 序列化格式正是最容易接错的地方，mock 掉之后「键写岔了」这种 bug 就永远不会被发现。
 */
function installLocalStorage(): Storage {
  const data = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return data.size;
    },
    clear: () => void data.clear(),
    getItem: (key: string) => data.get(key) ?? null,
    key: (index: number) => Array.from(data.keys())[index] ?? null,
    removeItem: (key: string) => void data.delete(key),
    setItem: (key: string, value: string) => void data.set(key, String(value)),
  };
  vi.stubGlobal("localStorage", storage);
  return storage;
}

let storage: Storage;

/** 把一条记录直接写进本地存储（模拟"之前建过一份行程"）。 */
function seedRecord(records: unknown) {
  storage.setItem(RECENT_TRIPS_KEY, JSON.stringify(records));
}

function record(id: string, title: string, savedAt = "2026-09-15T22:10:00") {
  return { id, title, savedAt };
}

beforeEach(() => {
  storage = installLocalStorage();
});

describe("/me 我的行程", () => {
  it("★ 没有记录时给友好空状态 + 一条能走出去的路（而不是报错）", async () => {
    render(<MyTripsPage />);

    const empty = await screen.findByTestId("recent-trips-empty");
    expect(within(empty).getByRole("heading", { name: "这里还是空的" })).toBeInTheDocument();
    expect(within(empty).getByRole("link", { name: "去规划一次" })).toHaveAttribute(
      "href",
      "/#planner",
    );
    // 「非报错」这一条要真的钉住：空状态里不该出现任何故障措辞
    expect(screen.queryByText(/出错|失败|不可用/)).toBeNull();
    expect(screen.queryByTestId("recent-trips-list")).toBeNull();
  });

  it("★ 坏数据退化成空状态，而不是把页面打崩", async () => {
    storage.setItem(RECENT_TRIPS_KEY, "{不是 JSON");
    render(<MyTripsPage />);

    expect(await screen.findByTestId("recent-trips-empty")).toBeInTheDocument();
  });

  it("有条记录时逐条列出，并链接到行程自己的地址", async () => {
    seedRecord([
      record("11111111-2222-3333-4444-555555555555", "沙面与永庆坊", "2026-09-15T22:10:00"),
      record("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "白云山半日", "2026-09-14T09:05:00"),
    ]);
    render(<MyTripsPage />);

    const list = await screen.findByTestId("recent-trips-list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);

    const first = screen.getByRole("link", { name: /沙面与永庆坊/ });
    expect(first).toHaveAttribute("href", "/trip/11111111-2222-3333-4444-555555555555");
    // 时间按本地时区显示到分钟
    expect(screen.getByText("2026-09-15 22:10")).toBeInTheDocument();
    expect(screen.getByText("2026-09-14 09:05")).toBeInTheDocument();
    // 有记录时不该再显示空状态
    expect(screen.queryByTestId("recent-trips-empty")).toBeNull();
  });

  it("坏条目被丢掉，好条目照常显示", async () => {
    seedRecord([record("good-id", "好的记录"), { id: "缺标题" }, null]);
    render(<MyTripsPage />);

    const list = await screen.findByTestId("recent-trips-list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByText("好的记录")).toBeInTheDocument();
  });

  it("★ 清空按钮：既清掉本地存储，也立刻把界面切到空状态", async () => {
    seedRecord([record("11111111-2222-3333-4444-555555555555", "沙面与永庆坊")]);
    render(<MyTripsPage />);

    const clear = await screen.findByTestId("recent-trips-clear");
    fireEvent.click(clear);

    expect(await screen.findByTestId("recent-trips-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("recent-trips-list")).toBeNull();
    await waitFor(() => expect(storage.getItem(RECENT_TRIPS_KEY)).toBeNull());
  });

  it("★ 头部高亮「我的行程」—— aria-current 指的是你真正所在的那一页", async () => {
    render(<MyTripsPage />);

    const nav = screen.getByRole("navigation", { name: "主导航" });
    expect(within(nav).getByRole("link", { name: "我的行程" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).getByRole("link", { name: "首页" })).not.toHaveAttribute("aria-current");
  });

  it("★ 把「记录 ≠ 副本」说明白：记录只在本地，行程仍按会话授权", async () => {
    render(<MyTripsPage />);

    expect(screen.getByText(/不经过服务器/)).toBeInTheDocument();
    expect(screen.getByText(/行程内容仍在后端、按游客会话隔离/)).toBeInTheDocument();
    expect(screen.getByText(/这些链接可能打不开/)).toBeInTheDocument();
  });
});
