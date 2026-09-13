"""高德（Amap）地图 Provider。

与 OSRM 的关键差别：高德对**每种出行方式返回真实的距离与耗时**
（步行规划走的是步行路网，不是车速），因此有 Key 时方案质量明显更好。

接口（官方文档 https://lbs.amap.com/api/webservice/guide/api/direction）：
- 步行 ``/v3/direction/walking``，驾车 ``/v3/direction/driving``，骑行 ``/v3/direction/bicycling``；
- 参数 ``origin`` / ``destination`` 为 ``"经度,纬度"``（小数不超过 6 位）；
- 响应 ``route.paths[0].distance``（米）、``route.paths[0].duration``（秒），字段是**字符串**。

★ 未在真实 Key 下验证 ★
开发环境没有高德 Key，因此本实现只经过契约测试（respx 录制形状的响应）。
任何结构不符都会抛 ``ProviderError``，由上层降级到 OSRM —— 不会把错误数据当真实数据用。
公交/地铁/轮渡不在本 Provider 覆盖范围内（返回 ``None`` 让调用方降级），
因为那需要额外的城市编码与换乘线路数据。
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from app.core.errors import ErrorCode, ProviderError
from app.domain.models import TransportMode
from app.providers.base import LatLng, ProviderHealth
from app.providers.map.base import MapLeg

__all__ = ["AmapMapProvider"]

_ENDPOINT = "https://restapi.amap.com/v3/direction"
_PATH = {"walk": "walking", "bike": "bicycling", "taxi": "driving"}


class AmapMapProvider:
    name = "amap"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _ENDPOINT,
        timeout_s: float = 8.0,
    ) -> None:
        if not api_key:
            raise ValueError("AmapMapProvider 需要非空 api_key（无 Key 请降级到 OSRM/haversine）")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=True, detail="已配置高德 Key（真实路网）")

    async def leg(self, origin: LatLng, destination: LatLng, *, mode: TransportMode) -> MapLeg | None:
        path = _PATH.get(mode)
        if path is None:
            # 公交/地铁/轮渡需要城市编码与线路数据，本 Provider 不提供 → 让调用方降级
            return None

        params = {
            "key": self._api_key,
            "origin": origin.as_lnglat(),
            "destination": destination.as_lnglat(),
            "output": "JSON",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.get(f"{self._base_url}/{path}", params=params)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, "direction", kind=ErrorCode.PROVIDER_TIMEOUT, message="高德请求超时"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name,
                "direction",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"高德请求失败：{type(exc).__name__}",
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                self.name,
                "direction",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"高德返回 HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name,
                "direction",
                kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
                message="高德返回了非 JSON 响应",
            ) from exc
        return _to_leg(body, mode=mode)

    def estimate_cost(self, op: str, units: int = 1) -> Decimal:
        # 单价未从官方价目表校准（pricing.yaml map.amap 全是 null）→ 由成本层标记未校准。
        return Decimal("0")


def _to_leg(body: object, *, mode: TransportMode) -> MapLeg:
    if not isinstance(body, dict):
        raise ProviderError(
            "amap", "direction", kind=ErrorCode.PROVIDER_INVALID_RESPONSE, message="高德响应不是对象"
        )
    # 高德的 status 是字符串 "1"/"0"
    if str(body.get("status", "0")) != "1":
        raise ProviderError(
            "amap",
            "direction",
            kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
            message=f"高德返回失败：{body.get('info', '未知错误')}",
        )
    route = body.get("route")
    paths = route.get("paths") if isinstance(route, dict) else None
    if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
        raise ProviderError(
            "amap", "direction", kind=ErrorCode.PROVIDER_INVALID_RESPONSE, message="高德响应缺少 route.paths"
        )
    first = paths[0]
    distance = _as_int(first.get("distance"))
    duration_s = _as_int(first.get("duration"))
    if distance is None:
        raise ProviderError(
            "amap", "direction", kind=ErrorCode.PROVIDER_INVALID_RESPONSE, message="高德响应缺少距离"
        )
    return MapLeg(
        distance_m=distance,
        # 秒 → 分钟，向上取整（宁可多算 1 分钟，也不要让用户误以为来得及）
        duration_min=None if duration_s is None else max(1, -(-duration_s // 60)),
        distance_source="amap",
        duration_source="amap" if duration_s is not None else None,
        mode=mode,
    )


def _as_int(value: object) -> int | None:
    """高德把数字编码成字符串（``"2600"``），这里容忍两种形式。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None
