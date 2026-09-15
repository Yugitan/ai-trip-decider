/**
 * 坐标纠偏：WGS-84 → GCJ-02（纯函数、零依赖、可单测）。
 *
 * ★ 为什么必须有这一步 ★
 * 我们的地点坐标**全部来自 OSM**，是 WGS-84（见 `backend/data/curated/guangzhou_places.yaml`
 * 的文件头：人工数据只写别名与排序偏好，不写任何事实性字段）。
 * 而高德的瓦片与 JS API 用的是 **GCJ-02**（俗称火星坐标）。
 * 拿 WGS-84 的点直接往高德地图上画，每一个标记都会偏 **100–700 米** ——
 * 在广州塔这种尺度上，就是把「广州塔」标到了江对岸。
 *
 * 所以：**存储与计算永远用 WGS-84**（后端 haversine / OSRM 都在 WGS-84 上算），
 * 只有「往高德地图上画」这一刻才换成 GCJ-02。本模块只负责这最后一跳。
 *
 * ★ 为什么不调高德的 `AMap.convertFrom` ★
 * 它是官方、精确的，但每个点位都要一次外网请求（还有 40 点/次的限制），
 * 于是地图上少一次网络往返就画不出来，且多一个失败点。
 * 这里用的是公开的偏移算法实现，**离线、确定、可测**；
 * 与官方转换的实测偏差见 `__tests__/amap-coords.test.ts` 里的实测向量
 * （用 `AMap.convertFrom` 逐个比对广州 6 个点位，最大偏差 ~1 米量级，
 * 对地图标记完全够用，且偏差写在测试里而不是靠一句「应该没问题」）。
 *
 * 参考：GCJ-02 由「克拉索夫斯基椭球 + 三角函数扰动」构成，
 * 各家实现给出的公式完全一致，因此不同实现之间的结果可以互相验证。
 */

/** 克拉索夫斯基椭球的长半轴（米）。 */
const EARTH_RADIUS_M = 6378245.0;

/** 椭球的偏心率平方。 */
const ECCENTRICITY_SQUARED = 0.00669342162296594323;

/** 一对经纬度。 */
export interface LatLng {
  latitude: number;
  longitude: number;
}

/**
 * 是否在中国境外。
 *
 * 境外的偏移算法不适用（GCJ-02 只在中国大陆有偏移），
 * 落到境外时原样返回 —— 注意这个判据本身是**粗略的矩形**，
 * 与「城市边界点面判定」那种精确做法不同；它只用来决定"要不要纠偏"，
 * 边界附近的误差最多让某个点少偏几十米，不会把点画到错误的国家。
 */
export function isOutOfChina(latitude: number, longitude: number): boolean {
  return (
    longitude < 72.004 ||
    longitude > 137.8347 ||
    latitude < 0.8293 ||
    latitude > 55.8271
  );
}

function transformLatitude(x: number, y: number): number {
  let ret =
    -100.0 +
    2.0 * x +
    3.0 * y +
    0.2 * y * y +
    0.1 * x * y +
    0.2 * Math.sqrt(Math.abs(x));
  ret +=
    ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) *
      2.0) /
    3.0;
  ret +=
    ((20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin((y / 3.0) * Math.PI)) *
      2.0) /
    3.0;
  ret +=
    ((160.0 * Math.sin((y / 12.0) * Math.PI) +
      320 * Math.sin((y * Math.PI) / 30.0)) *
      2.0) /
    3.0;
  return ret;
}

function transformLongitude(x: number, y: number): number {
  let ret =
    300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  ret +=
    ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) *
      2.0) /
    3.0;
  ret +=
    ((20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin((x / 3.0) * Math.PI)) *
      2.0) /
    3.0;
  ret +=
    ((150.0 * Math.sin((x / 12.0) * Math.PI) +
      300.0 * Math.sin((x / 30.0) * Math.PI)) *
      2.0) /
    3.0;
  return ret;
}

/**
 * WGS-84 → GCJ-02。
 *
 * 约定（调用方需要知道的两条）：
 * 1. **非有限值原样返回**：`NaN` / `Infinity` 进来还是出去，
 *    不做「补个 0」或者「返回中心点」这种好心办坏事的事
 *    （`0, 0` 是几内亚湾，会让地图把视野调到非洲）。是否可画由调用方判断。
 * 2. **境外原样返回**：GCJ-02 在中国大陆之外没有偏移。
 */
export function wgs84ToGcj02(latitude: number, longitude: number): LatLng {
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
    return { latitude, longitude };
  }
  if (isOutOfChina(latitude, longitude)) {
    return { latitude, longitude };
  }

  const deltaLat = transformLatitude(longitude - 105.0, latitude - 35.0);
  const deltaLng = transformLongitude(longitude - 105.0, latitude - 35.0);
  const radLat = (latitude / 180.0) * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - ECCENTRICITY_SQUARED * magic * magic;
  const sqrtMagic = Math.sqrt(magic);

  return {
    latitude: latitude + (deltaLat * 180.0) / (((EARTH_RADIUS_M * (1 - ECCENTRICITY_SQUARED)) / (magic * sqrtMagic)) * Math.PI),
    longitude: longitude + (deltaLng * 180.0) / ((EARTH_RADIUS_M / sqrtMagic) * Math.cos(radLat) * Math.PI),
  };
}

/**
 * 地图上的一个站点：显示用坐标已经纠偏过，`seq` 用来标注编号。
 *
 * 刻意**不在这里做筛选**（过滤非有限值/越界坐标）：那是调用方的判断，
 * 而且「几个点没画出来」这件事必须能让用户看见，不能在这层悄悄丢掉。
 */
export interface MapPoint {
  seq: number;
  name: string;
  latitude: number;
  longitude: number;
}

/** 一组点位是否都落在合法的经纬度范围内（用于决定「这张图能不能画」）。 */
export function isPlottable(latitude: number, longitude: number): boolean {
  return (
    Number.isFinite(latitude) &&
    Number.isFinite(longitude) &&
    latitude >= -90 &&
    latitude <= 90 &&
    longitude >= -180 &&
    longitude <= 180
  );
}
