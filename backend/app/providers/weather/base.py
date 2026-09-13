"""天气 Provider 契约（PRD §15.5、config/ttl.yaml 的 refresh_policy）。

天气只影响**偏好权重**（雨天加室内、高温加休息），不参与硬约束判定 ——
所以天气 API 挂掉时直接跳过适配即可（降级矩阵）。分类阈值全部来自配置，
不写死在代码里。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.models import WeatherCondition
from app.providers.base import LatLng, ProviderHealth

__all__ = ["WeatherProvider", "WeatherSnapshot", "WeatherThresholds", "classify_weather"]


@dataclass(frozen=True, slots=True)
class WeatherThresholds:
    """触发偏好调整的天气阈值（来自 config/ttl.yaml 的 refresh_policy）。"""

    rain_probability: float = 0.60
    high_temp_c: float = 33.0
    low_temp_c: float = 8.0


@dataclass(frozen=True, slots=True)
class WeatherSnapshot:
    date: str
    condition: WeatherCondition
    rain_probability: float
    temp_max_c: float
    temp_min_c: float
    source: str = "open_meteo"


@runtime_checkable
class WeatherProvider(Protocol):
    name: str

    async def forecast(self, location: LatLng, *, days: int = 7) -> list[WeatherSnapshot]: ...

    def health(self) -> ProviderHealth: ...


def classify_weather(
    *,
    rain_probability: float,
    temp_max_c: float,
    thresholds: WeatherThresholds | None = None,
) -> WeatherCondition:
    """把数值天气归到 ``clear | rain | heat`` 三态。

    优先级：**雨天优先于高温**。理由是两者的应对动作不同且互斥 ——
    既可以下雨又高温时，用户更需要"带伞 + 室内"而不是"少走动"，
    而且室内景点通常也避暑，一套调整能覆盖两种诉求。
    """
    limits = thresholds or WeatherThresholds()
    if rain_probability >= limits.rain_probability:
        return "rain"
    if temp_max_c >= limits.high_temp_c:
        return "heat"
    return "clear"


def weather_from_daily(
    daily: Mapping[str, object],
    *,
    source: str = "open_meteo",
    thresholds: WeatherThresholds | None = None,
) -> list[WeatherSnapshot]:
    """把 open-meteo 的 daily 数组翻译成 ``WeatherSnapshot`` 列表。

    入参类型故意是 ``Mapping[str, object]`` 而不是 ``dict[str, Sequence]``：
    它来自 ``response.json()``，即**不可信输入**。把"应该是数组"写进类型签名
    只会让调用方以为已经校验过了。

    数组长度不一致时以 ``time`` 为准，缺失项跳过 —— 宁可少给一天，
    也不要错位（把明天的降雨概率配到后天的日期上）。

    ★ 四个字段都必须是真数组 ★
    open-meteo 若因参数错误返回 ``"time": "2026-09-12"``（字符串而非数组），
    ``enumerate`` 会逐字符迭代：日期变成 ``"2"`` / ``"0"`` 并配上**真实的**温度，
    产出的是"看起来像数据"的垃圾。这类静默错位比报错危险得多，所以这里先拒绝非数组。
    """
    dates = _series(daily.get("time"))
    rain = _series(daily.get("precipitation_probability_max"))
    highs = _series(daily.get("temperature_2m_max"))
    lows = _series(daily.get("temperature_2m_min"))
    snapshots: list[WeatherSnapshot] = []
    for index, day in enumerate(dates):
        if index >= len(highs) or index >= len(lows):
            continue
        high = _as_float(highs[index])
        low = _as_float(lows[index])
        if high is None or low is None:
            continue
        probability = _as_float(rain[index]) if index < len(rain) else None
        # open-meteo 给的是百分数（0–100），统一成 0–1
        ratio = 0.0 if probability is None else probability / 100.0
        snapshots.append(
            WeatherSnapshot(
                date=str(day),
                condition=classify_weather(
                    rain_probability=ratio, temp_max_c=high, thresholds=thresholds
                ),
                rain_probability=ratio,
                temp_max_c=high,
                temp_min_c=low,
                source=source,
            )
        )
    return snapshots


def _series(value: object) -> Sequence[object]:
    """真数组才接受；``str``/``bytes`` 也是 ``Sequence``，但逐字符迭代会静默错位。"""
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        return ()
    return value


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
