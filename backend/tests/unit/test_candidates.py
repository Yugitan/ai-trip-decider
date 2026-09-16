"""候选生成里「一站待多久」的单元测试。

守的是一条**用户一眼就能看出来的**诚实性：选了「轻松」却排出一份和「紧凑」
完全一样的行程（站点相同、每站停留相同，只有步行上限不同），那 pace 就是个
没生效的装饰。停留时长必须真的随节奏变，且变化是**可审计**的（系数在
``config/limits.yaml``，不是散在代码里的 1.3）。
"""

from __future__ import annotations

import pytest

from app.core.config import get_limits_config
from app.domain.candidates import stay_duration_min
from app.domain.models import Place

pytestmark = pytest.mark.unit


def _place(category: str = "food", duration: int | None = None) -> Place:
    return Place(
        id=f"{category}-1",
        name="测试地点",
        category=category,
        lat=23.13,
        lng=113.26,
        recommended_duration_min=duration,
    )


def test_balanced_is_the_identity() -> None:
    """balanced 必须原样返回地点自带时长 —— 否则等于把库里的数据悄悄改了一遍。"""
    assert stay_duration_min(_place(duration=90), "balanced") == 90
    assert stay_duration_min(_place(duration=45), "balanced") == 45


def test_pace_actually_changes_the_stay() -> None:
    """relaxed 待得比 balanced 久、packed 更短，且顺序严格。"""
    place = _place(duration=90)
    relaxed = stay_duration_min(place, "relaxed")
    balanced = stay_duration_min(place, "balanced")
    packed = stay_duration_min(place, "packed")
    assert relaxed > balanced > packed, (relaxed, balanced, packed)


def test_stay_is_rounded_to_five_minutes() -> None:
    """90 × 1.3 = 117 —— 「117 分钟」是假精度，读者会以为它是量出来的。"""
    for pace in ("relaxed", "balanced", "packed"):
        assert stay_duration_min(_place(duration=90), pace) % 5 == 0


def test_min_stop_duration_is_enforced() -> None:
    """transport_hub 基线只有 10 分钟、再乘 0.85 —— 不能低于配置的下限。

    低于下限的停留意味着"为它排了一个站，却不值得为它停下来"，
    这种站点应该在候选阶段被剪掉，而不是排进路线。
    """
    limits = get_limits_config()
    floor = limits.planning.min_stop_duration_min
    assert floor > 10, "配置下限若 ≤ transport_hub 基线，这条测试就失去意义了"
    assert stay_duration_min(_place("transport_hub"), "packed", limits=limits) == floor


def test_falls_back_to_category_baseline() -> None:
    """地点没有 recommended_duration_min 时用类别基线，且**同样**受节奏影响。"""
    limits = get_limits_config()
    baseline = limits.planning.stay_scale_by_pace["relaxed"]
    assert baseline != 1.0, "relaxed 系数若为 1，这条测试就查不出「基线绕过节奏」的 bug"

    from app.core.config import get_seed_config

    base = get_seed_config().duration_bases["museum"]

    def _baseline_place() -> Place:
        # 单独造一个 Place：Place 是 frozen 的，改字段要另建
        return Place(id="museum-1", name="博物馆", category="museum", lat=23.1, lng=113.2)

    assert stay_duration_min(_baseline_place(), "balanced", limits=limits) == base
    assert stay_duration_min(_baseline_place(), "relaxed", limits=limits) > base


def test_missing_pace_key_degrades_to_unscaled() -> None:
    """旧配置里没有 stay_scale_by_pace 时退回 1.0，而不是崩掉或按 0 缩放。"""
    limits = get_limits_config()
    stripped = limits.model_copy(
        update={"planning": limits.planning.model_copy(update={"stay_scale_by_pace": {}})}
    )
    assert stay_duration_min(_place(duration=60), "relaxed", limits=stripped) == 60
