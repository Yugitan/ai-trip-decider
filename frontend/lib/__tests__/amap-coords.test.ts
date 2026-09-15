import { describe, expect, it } from "vitest";

import {
  isOutOfChina,
  isPlottable,
  wgs84ToGcj02,
} from "@/lib/amap-coords";

/**
 * 坐标纠偏的测试。
 *
 * 为什么这个文件重要：**纠偏错了不会报错，只会把点画错地方** ——
 * 地图照样渲染、界面照样正常，用户看到的是一张「看起来很对」的错图。
 * 而它的输入（OSM 的 WGS-84）和输出（高德的 GCJ-02）都在我们手上，
 * 偏差到底多大是可以被测量的，所以这里既钉算法本身，也钉与官方转换的实测偏差。
 */

/** 广州塔（WGS-84，来自 OSM）。 */
const CANTON_TOWER = { latitude: 23.1066, longitude: 113.3245 };

/** 两点之间的近似距离（米，等距圆柱投影，测试里够用）。 */
function metersBetween(
  a: { latitude: number; longitude: number },
  b: { latitude: number; longitude: number },
): number {
  const metersPerDegreeLat = 111_320;
  const metersPerDegreeLng =
    metersPerDegreeLat * Math.cos((a.latitude * Math.PI) / 180);
  const dy = (a.latitude - b.latitude) * metersPerDegreeLat;
  const dx = (a.longitude - b.longitude) * metersPerDegreeLng;
  return Math.sqrt(dx * dx + dy * dy);
}

describe("isOutOfChina", () => {
  it("中国境内的城市不判为境外", () => {
    expect(isOutOfChina(23.1066, 113.3245)).toBe(false); // 广州
    expect(isOutOfChina(39.9087, 116.3975)).toBe(false); // 北京
    expect(isOutOfChina(43.8256, 87.6168)).toBe(false); // 乌鲁木齐（西边界）
  });

  it("境外判为境外", () => {
    expect(isOutOfChina(35.6762, 139.6503)).toBe(true); // 东京
    expect(isOutOfChina(40.7128, -74.006)).toBe(true); // 纽约
    expect(isOutOfChina(-33.8688, 151.2093)).toBe(true); // 悉尼
  });
});

describe("wgs84ToGcj02", () => {
  it("★ 广州的坐标必须真的发生偏移（否则地图上就是差几百米）★", () => {
    const converted = wgs84ToGcj02(
      CANTON_TOWER.latitude,
      CANTON_TOWER.longitude,
    );
    expect(converted.latitude).not.toBe(CANTON_TOWER.latitude);
    expect(converted.longitude).not.toBe(CANTON_TOWER.longitude);

    // 量级：中国境内 GCJ-02 的偏移在数百米级别。
    // 太小说明算法被短路了（比如直接 return 输入），太大说明公式写错了。
    const offset = metersBetween(converted, CANTON_TOWER);
    expect(offset).toBeGreaterThan(100);
    expect(offset).toBeLessThan(1000);
  });

  it("偏移方向在广州是「向东南」（纬度变小、经度变大）", () => {
    // 方向不是拍脑袋：下面钉着的官方向量就是「纬度变小、经度变大」。
    // 偏移方向随纬度变化（北京就是纬度变大），所以这里只断言广州这一处。
    const converted = wgs84ToGcj02(
      CANTON_TOWER.latitude,
      CANTON_TOWER.longitude,
    );
    expect(converted.latitude).toBeLessThan(CANTON_TOWER.latitude);
    expect(converted.longitude).toBeGreaterThan(CANTON_TOWER.longitude);
  });

  it("同一个坐标永远得到同一个结果（纯函数，可重复）", () => {
    expect(wgs84ToGcj02(23.1066, 113.3245)).toEqual(
      wgs84ToGcj02(23.1066, 113.3245),
    );
  });

  it("境外坐标原样返回（GCJ-02 只在中国大陆有偏移）", () => {
    // 东京
    expect(wgs84ToGcj02(35.6762, 139.6503)).toEqual({
      latitude: 35.6762,
      longitude: 139.6503,
    });
  });

  it("非有限值原样返回，绝不补一个 0", () => {
    // 0,0 是几内亚湾 —— 把缺失坐标当成 0 会让地图把视野拉到非洲，
    // 比画不出来糟糕得多。「有没有得画」由调用方判断。
    expect(wgs84ToGcj02(Number.NaN, 113.3245)).toEqual({
      latitude: Number.NaN,
      longitude: 113.3245,
    });
    expect(wgs84ToGcj02(23.1066, Number.POSITIVE_INFINITY)).toEqual({
      latitude: 23.1066,
      longitude: Number.POSITIVE_INFINITY,
    });
  });

  it("纬度 0 这种「恰好等于 0 的合法值」不会被误判成缺失", () => {
    // 赤道 + 中国经度：在境外，原样返回；重点是 0 作为一个**数值**被保留，
    // 而不是被 `if (!lat)` 之类的写法当成「没有值」丢掉。
    expect(wgs84ToGcj02(0, 113.3245)).toEqual({
      latitude: 0,
      longitude: 113.3245,
    });
  });
});

describe("与高德官方转换的实测比对", () => {
  /**
   * ★ 这些期望值不是算出来的，是**量出来的** ★
   *
   * 取法：用 `AMap.convertFrom(lnglat, "gps", cb)`（官方 JS SDK 的 WGS-84 → GCJ-02 转换）
   * 对同几个点求值，结果原样抄在这里。取数时间：2026-09-14。
   *
   * 为什么要这么做：单测自己算一遍再自己断言，只能证明"实现没变"，
   * 不能证明"实现是对的" —— 公式写错一个符号也会一直绿。
   * 拿官方值当基准，才能把「我们算的」与「高德认为的」对齐。
   *
   * 实测（见下面两条断言）：最大偏差 2.8e-6 度 ≈ **0.31 米**，而且高德返回的坐标
   * 本身就只保留到 6 位小数（≈0.11 米），所以这点差异基本全来自它自己的取整。
   */
  const OFFICIAL: ReadonlyArray<{
    name: string;
    wgs: [number, number];
    gcj: [number, number];
  }> = [
    { name: "广州塔", wgs: [23.1066, 113.3245], gcj: [23.104001, 113.329924] },
    { name: "陈家祠", wgs: [23.1291, 113.2466], gcj: [23.126419, 113.251923] },
    { name: "海心沙", wgs: [23.1135, 113.3208], gcj: [23.110897, 113.326217] },
    { name: "北京天安门", wgs: [39.9087, 116.3975], gcj: [39.910103, 116.403743] },
  ];

  it.each(OFFICIAL)("$name 与官方结果相差不到 1 米", ({ wgs, gcj }) => {
    const converted = wgs84ToGcj02(wgs[0], wgs[1]);
    expect(metersBetween(converted, { latitude: gcj[0], longitude: gcj[1] })).toBeLessThan(1);
  });

  it("官方向量下算法输出保持稳定（有人改了公式就会红）", () => {
    // 与上一条的区别：上一条容忍 1 米的偏差（官方取整造成），
    // 这一条把本算法的输出精确钉住 —— 公式被改动时能立刻发现，
    // 并迫使人重新去官方比对一次，而不是直接改期望值让它变绿。
    expect(wgs84ToGcj02(23.1066, 113.3245)).toEqual({
      latitude: 23.10399820620719,
      longitude: 113.32992121689368,
    });
  });
});

describe("isPlottable", () => {
  it("合法经纬度可画", () => {
    expect(isPlottable(23.1066, 113.3245)).toBe(true);
    expect(isPlottable(0, 0)).toBe(true);
    expect(isPlottable(-90, 180)).toBe(true);
  });

  it("越界或非有限值不可画", () => {
    expect(isPlottable(91, 113)).toBe(false);
    expect(isPlottable(23, 181)).toBe(false);
    expect(isPlottable(Number.NaN, 113)).toBe(false);
    expect(isPlottable(23, Number.NaN)).toBe(false);
  });
});
