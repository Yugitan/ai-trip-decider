import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RouteMap } from "@/components/route-map";
import { wgs84ToGcj02 } from "@/lib/amap-coords";
import type { AmapLoadResult } from "@/lib/amap";

/**
 * 地图组件的测试。
 *
 * 三条要认真守住的东西：
 * 1. **画的是纠偏后的坐标**：直接把 OSM 的 WGS-84 丢给高德，每个点会偏几百米 ——
 *    图还是能画出来，只是错的。所以断言的是 `LngLat` 收到的值等于纠偏结果；
 * 2. **画不出来时说的是真话**：缺 Key / 加载失败 / 坐标不全，三种原因各有一句话；
 * 3. **卸载要销毁地图**：WebGL 上下文不释放，来回改几次路线页面就会卡死。
 */

// Vitest 3 的 `vi.fn` 只接受**一个**函数类型参数（不是 <参数元组, 返回值> 两个）。
const loadAmapMock = vi.fn<() => Promise<AmapLoadResult>>();
const amapJsKeyMock = vi.fn<() => string | null>();

vi.mock("@/lib/amap", async () => {
  // 只替换真正碰网络/全局的那两个函数；类型导出在运行时不存在，无需提供。
  return {
    amapJsKey: () => amapJsKeyMock(),
    loadAmap: () => loadAmapMock(),
  };
});

/**
 * 取数组第 `index` 项，缺失时直接失败。
 *
 * tsconfig 开了 `noUncheckedIndexedAccess`：下标是 `T | undefined`。
 * 与其用 `!` 把类型按下去，不如让"没有这一项"变成一个说人话的失败 ——
 * 那也正是这些断言想知道的。
 */
function at<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) {
    throw new Error(`期望第 ${index} 项存在，实际只有 ${items.length} 项`);
  }
  return item;
}

/** 记录下所有被创建的地图与覆盖物，供断言使用。 */
function makeFakeAmap() {
  const created = {
    lngLats: [] as [number, number][],
    markers: [] as Record<string, unknown>[],
    polylines: [] as Record<string, unknown>[],
    added: [] as unknown[],
    fitViewCalls: 0,
    destroyed: 0,
    containerAtCreate: null as HTMLElement | null,
  };

  class FakeLngLat {
    constructor(
      public longitude: number,
      public latitude: number,
    ) {
      created.lngLats.push([longitude, latitude]);
    }
    getLng() {
      return this.longitude;
    }
    getLat() {
      return this.latitude;
    }
  }
  class FakeMarker {
    constructor(public options: Record<string, unknown>) {
      created.markers.push(options);
    }
    setMap() {}
  }
  class FakePolyline {
    constructor(public options: Record<string, unknown>) {
      created.polylines.push(options);
    }
    setMap() {}
  }
  class FakeMap {
    constructor(container: HTMLElement | string) {
      created.containerAtCreate =
        typeof container === "string" ? null : container;
    }
    add(overlays: unknown[]) {
      created.added.push(...overlays);
    }
    setFitView() {
      created.fitViewCalls += 1;
    }
    destroy() {
      created.destroyed += 1;
    }
  }

  return {
    created,
    namespace: {
      Map: FakeMap,
      Marker: FakeMarker,
      Polyline: FakePolyline,
      LngLat: FakeLngLat,
    },
  };
}

const STOPS = [
  { seq: 1, name: "广州塔", latitude: 23.1066, longitude: 113.3245 },
  { seq: 2, name: "海心沙", latitude: 23.1135, longitude: 113.3208 },
  { seq: 3, name: "陈家祠", latitude: 23.1291, longitude: 113.2466 },
];

beforeEach(() => {
  loadAmapMock.mockReset();
  amapJsKeyMock.mockReset();
  amapJsKeyMock.mockReturnValue("test-key");
});

describe("RouteMap 的画不出来", () => {
  it("没配 Key：说明白是缺配置，而不是留一个灰盒子", () => {
    amapJsKeyMock.mockReturnValue(null);
    render(<RouteMap points={STOPS} label="A" />);
    expect(screen.getByText(/未配置地图 Key/)).toBeInTheDocument();
    expect(screen.getByText(/NEXT_PUBLIC_AMAP_JS_KEY/)).toBeInTheDocument();
    expect(screen.queryByTestId("route-map-A")).not.toBeInTheDocument();
    expect(loadAmapMock).not.toHaveBeenCalled();
  });

  it("坐标不全：说的是「画不出来」，并明说站点照常显示、不编位置", () => {
    render(
      <RouteMap
        points={[
          { seq: 1, name: "坐标缺失的点", latitude: Number.NaN, longitude: 113 },
        ]}
        label="A"
      />,
    );
    expect(screen.getByText(/缺少可用坐标/)).toBeInTheDocument();
    expect(screen.getByText(/不会替它们编一个位置/)).toBeInTheDocument();
    expect(loadAmapMock).not.toHaveBeenCalled();
  });

  it("部分坐标缺失时仍然画图（能画的画出来，不去猜缺的那些）", async () => {
    const { created, namespace } = makeFakeAmap();
    loadAmapMock.mockResolvedValue({ ok: true, amap: namespace as never });
    render(
      <RouteMap
        points={[
          ...STOPS,
          { seq: 4, name: "无坐标", latitude: Number.NaN, longitude: Number.NaN },
        ]}
        label="A"
      />,
    );
    await waitFor(() => expect(created.markers).toHaveLength(3));
  });

  it("加载失败：把原因说出来，并补一句「站点列表不受影响」", async () => {
    loadAmapMock.mockResolvedValue({
      ok: false,
      reason: "地图脚本加载失败（webapi.amap.com 不可达，或被浏览器/扩展拦截）",
    });
    render(<RouteMap points={STOPS} label="A" />);
    expect(await screen.findByText(/webapi\.amap\.com 不可达/)).toBeInTheDocument();
    expect(screen.getByText(/站点列表不受影响/)).toBeInTheDocument();
  });

  it("初始化抛异常（容器没尺寸 / WebGL 不可用）也要变成一句话", async () => {
    loadAmapMock.mockResolvedValue({
      ok: true,
      amap: {
        Map: class {
          constructor() {
            throw new Error("WebGL not supported");
          }
        },
      } as never,
    });
    render(<RouteMap points={STOPS} label="A" />);
    expect(
      await screen.findByText(/地图初始化失败（WebGL not supported）/),
    ).toBeInTheDocument();
  });
});

describe("RouteMap 画得出来", () => {
  it("★ 交给高德的是纠偏后的坐标，不是原始的 WGS-84 ★", async () => {
    const { created, namespace } = makeFakeAmap();
    loadAmapMock.mockResolvedValue({ ok: true, amap: namespace as never });
    render(<RouteMap points={STOPS} label="A" />);

    await waitFor(() => expect(created.lngLats).toHaveLength(3));

    STOPS.forEach((stop, index) => {
      const expected = wgs84ToGcj02(stop.latitude, stop.longitude);
      const [longitude, latitude] = at(created.lngLats, index);
      expect(longitude).toBeCloseTo(expected.longitude, 10);
      expect(latitude).toBeCloseTo(expected.latitude, 10);
      // 而且必须**不等于**原始坐标：这是"漏掉纠偏"这个回归的直接证据
      expect(longitude).not.toBe(stop.longitude);
      expect(latitude).not.toBe(stop.latitude);
    });
  });

  it("每个点一个带序号的标记，并按顺序连成一条虚线", async () => {
    const { created, namespace } = makeFakeAmap();
    loadAmapMock.mockResolvedValue({ ok: true, amap: namespace as never });
    render(<RouteMap points={STOPS} label="A" />);

    await waitFor(() => expect(created.markers).toHaveLength(3));
    expect(
      created.markers.map(
        (marker) => (marker.label as { content: string }).content,
      ),
    ).toEqual(["1", "2", "3"]);
    expect(created.markers.map((marker) => marker.title)).toEqual([
      "广州塔",
      "海心沙",
      "陈家祠",
    ]);

    expect(created.polylines).toHaveLength(1);
    const polyline = at(created.polylines, 0);
    expect((polyline.path as unknown[]).length).toBe(3);
    // 线是虚线，且图注必须说清它不是实际路线（不是靠颜色暗示）
    expect(polyline.strokeStyle).toBe("dashed");
    expect(
      screen.getByText(/虚线只表示站点的先后顺序，不是实际行车路线/),
    ).toBeInTheDocument();
  });

  it("只有一个站点时不画线（一条线要两个点，画一条零长度的线没有意义）", async () => {
    const { created, namespace } = makeFakeAmap();
    loadAmapMock.mockResolvedValue({ ok: true, amap: namespace as never });
    render(<RouteMap points={STOPS.slice(0, 1)} label="A" />);
    await waitFor(() => expect(created.markers).toHaveLength(1));
    expect(created.polylines).toHaveLength(0);
  });

  it("把视野适配到所有覆盖物上，并且容器是真的挂进了 DOM", async () => {
    const { created, namespace } = makeFakeAmap();
    loadAmapMock.mockResolvedValue({ ok: true, amap: namespace as never });
    render(<RouteMap points={STOPS} label="A" />);
    await waitFor(() => expect(created.added.length).toBeGreaterThan(0));
    expect(created.fitViewCalls).toBe(1);
    expect(created.containerAtCreate).toBe(
      screen.getByTestId("route-map-A"),
    );
  });

  it("★ 卸载时必须销毁地图（否则来回改路线会耗尽 WebGL 上下文）★", async () => {
    const { created, namespace } = makeFakeAmap();
    loadAmapMock.mockResolvedValue({ ok: true, amap: namespace as never });
    const { unmount } = render(<RouteMap points={STOPS} label="A" />);
    await waitFor(() => expect(created.markers).toHaveLength(3));
    unmount();
    expect(created.destroyed).toBe(1);
  });

  it("等 SDK 期间显示「加载中」，不假装已经画好", async () => {
    const { namespace } = makeFakeAmap();
    let resolve: (value: AmapLoadResult) => void = () => {};
    loadAmapMock.mockReturnValue(
      new Promise<AmapLoadResult>((r) => {
        resolve = r;
      }),
    );
    render(<RouteMap points={STOPS} label="A" />);
    expect(screen.getByText("地图加载中…")).toBeInTheDocument();
    resolve({ ok: true, amap: namespace as never });
    await waitFor(() =>
      expect(screen.queryByText("地图加载中…")).not.toBeInTheDocument(),
    );
  });
});
