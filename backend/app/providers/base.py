"""Provider 通用契约：健康状态与经纬度。

``ProviderHealth`` 是"诚实性"在 Provider 层的落点：**不可用必须被如实报告**，
而不是抛一个异常让上层猜。``/health`` 与 UI 的降级提示都读它。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LatLng", "ProviderHealth"]


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """一个 Provider 的健康状态。``available=False`` 表示处于降级模式。"""

    name: str
    available: bool
    detail: str = ""
    degraded: bool = False

    @property
    def is_usable(self) -> bool:
        return self.available


@dataclass(frozen=True, slots=True)
class LatLng:
    """一个坐标点。多数 Provider 只接受 (lat, lng)，个别要求 (lng, lat) 字符串。"""

    lat: float
    lng: float

    def as_lnglat(self, digits: int = 6) -> str:
        """``"113.264385,23.129112"`` —— 高德/OSRM 都要求经度在前。

        高德文档明确"经纬度小数点不超过 6 位"，所以这里默认保留 6 位，
        避免因为多余精度被服务端拒绝或截断出意料之外的位置。
        """
        return f"{self.lng:.{digits}f},{self.lat:.{digits}f}"
