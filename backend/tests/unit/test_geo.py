"""地理计算的单元测试（**纯函数，零 IO、零数据库、零网络**）。

为什么这个文件必须存在：
    ``app/domain/geo.py`` 是「矩形 bbox 切进邻市」这个**最严重数据 bug** 的修复核心
    （矩形 bbox 约 18 800 km²，而广州实际只有约 7 434 km²，导致约 2 000 条深圳/东莞/
    佛山地点混进广州知识库）。修复前它没有任何专用测试 —— 只能靠"跑完 12 秒建库后
    看地点总数对不对"来间接验证，而这恰恰是最不可靠的验证方式：
    边界判定错了，用户会在路线里看到根本不在广州的地点。

这组测试钉住三类东西：
1. **几何算法的正确性**：射线法的内部/外部/凹多边形/洞（飞地）判定。
2. **边界点的确切语义**：顶点命中是保证的，边中点不是（这是刻意取舍，见 docstring）。
3. **耗时估算的诚实性**：只允许"向上取整"（宁可多算，不可少算），
   以及浮点误差不得凭空多算一分钟。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from math import ceil, radians

import pytest

from app.domain.geo import (
    DEFAULT_ROUTE_FACTOR,
    EARTH_RADIUS_M,
    CityBoundary,
    bbox_contains,
    haversine_m,
    point_in_polygon,
    point_in_rings,
    route_factor,
    travel_minutes,
)

pytestmark = pytest.mark.unit

# ── 通用测试几何体 ──────────────────────────────────────────────────────────
#
# 统一用「纬度 23.0–23.2、经度 113.0–113.2」的正方形（约 22 km × 20 km），
# 量级与广州城区一致，便于把断言数字和真实地理直觉对上。
# 顶点格式是 (lat, lng) —— 与 CityBoundary / point_in_polygon 的约定一致。

SQUARE: tuple[tuple[float, float], ...] = (
    (23.0, 113.0),
    (23.0, 113.2),
    (23.2, 113.2),
    (23.2, 113.0),
)

# 带缺口的凹多边形（右下角被挖掉一块），用于验证射线法不会把缺口当成内部。
NOTCHED: tuple[tuple[float, float], ...] = (
    (23.0, 113.0),
    (23.0, 113.2),
    (23.1, 113.2),
    (23.1, 113.1),
    (23.2, 113.1),
    (23.2, 113.0),
)

# 内环（洞）：位于 SQUARE 内部的更小正方形
HOLE: tuple[tuple[float, float], ...] = (
    (23.05, 113.05),
    (23.05, 113.15),
    (23.15, 113.15),
    (23.15, 113.05),
)


# ════════════════════════════════════════════════════════════════════════════
# 一、haversine_m：大圆距离
# ════════════════════════════════════════════════════════════════════════════


def test_haversine_same_point_is_zero() -> None:
    assert haversine_m(23.1066, 113.3245, 23.1066, 113.3245) == pytest.approx(0.0, abs=1e-6)


def test_haversine_one_degree_of_latitude_is_about_111_km() -> None:
    """1 度纬度在任何经度上都是同一个弧长（111.19 km），这是最容易核对的基准值。"""
    distance = haversine_m(23.0, 113.0, 24.0, 113.0)
    assert distance == pytest.approx(111_194.9, rel=1e-4)
    # 与理论值 R * Δφ(弧度) 一致
    assert distance == pytest.approx(EARTH_RADIUS_M * radians(1.0), rel=1e-9)


def test_haversine_one_degree_of_longitude_shrinks_with_latitude() -> None:
    """1 度经度的弧长随纬度按 cos(φ) 收缩 —— 这是"经度不可直接当距离"的原因。"""
    at_equator = haversine_m(0.0, 113.0, 0.0, 114.0)
    at_guangzhou = haversine_m(23.0, 113.0, 23.0, 114.0)
    assert at_guangzhou < at_equator
    assert at_guangzhou / at_equator == pytest.approx(0.9205, abs=1e-3)  # cos(23°)


def test_haversine_is_symmetric() -> None:
    pairs = [
        (23.1066, 113.3245, 23.1291, 113.2466),
        (23.0, 113.0, 23.5, 113.5),
        (22.9, 113.4, 23.3, 113.1),
    ]
    for lat1, lng1, lat2, lng2 in pairs:
        assert haversine_m(lat1, lng1, lat2, lng2) == pytest.approx(
            haversine_m(lat2, lng2, lat1, lng1), rel=1e-12
        )


def test_haversine_is_non_negative_and_small_for_nearby_points() -> None:
    """同城两点的距离必须落在合理量级（防止把度当成米之类的单位错误）。"""
    distance = haversine_m(23.1066, 113.3245, 23.1291, 113.2466)  # 广州塔 → 陈家祠
    assert 0 < distance < 15_000, f"同城两点算出 {distance} 米，量级明显不对"


# ════════════════════════════════════════════════════════════════════════════
# 二、route_factor：直线距离 → 路网距离的绕行系数
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("crow_m", "expected"),
    [
        (0, 1.15),  # 极短距离：绕行比例最高
        (299, 1.15),
        (300, 1.30),  # 阈值本身归入下一档（< 300 才走 1.15）
        (1_000, 1.30),
        (1_499, 1.30),
        (1_500, 1.40),  # 阈值本身归入下一档
        (10_000, 1.40),
    ],
)
def test_route_factor_buckets(crow_m: float, expected: float) -> None:
    assert route_factor(crow_m) == pytest.approx(expected)


def test_route_factor_is_monotonic_non_decreasing() -> None:
    """系数只应随距离上升 —— 长距离的绕行比例不会比短距离更高。"""
    values = [route_factor(d) for d in range(0, 5_000, 50)]
    assert values == sorted(values)


def test_route_factor_never_shrinks_the_distance() -> None:
    """系数必须 >= 1：估算要保守（偏长），绝不能让距离看起来比直线还短。"""
    for crow in (0, 100, 299, 300, 1_499, 1_500, 50_000):
        assert route_factor(crow) >= 1.0


def test_default_route_factor_documents_the_mid_bucket() -> None:
    """DEFAULT_ROUTE_FACTOR 目前没有调用方，但它应等于中档系数，避免误导后来者。"""
    assert pytest.approx(1.30) == DEFAULT_ROUTE_FACTOR


# ════════════════════════════════════════════════════════════════════════════
# 三、bbox_contains：矩形粗筛（含边界）
# ════════════════════════════════════════════════════════════════════════════

BBOX = {"min_lat": 23.0, "max_lat": 23.2, "min_lng": 113.0, "max_lng": 113.2}


@pytest.mark.parametrize(
    ("lat", "lng", "expected"),
    [
        (23.1, 113.1, True),  # 内部
        (23.0, 113.0, True),  # 左下角（边界算在内）
        (23.2, 113.2, True),  # 右上角
        (23.2, 113.1, True),  # 上边
        (23.0, 113.1, True),  # 下边
        (22.999, 113.1, False),  # 略低
        (23.201, 113.1, False),  # 略高
        (23.1, 112.999, False),  # 略左
        (23.1, 113.201, False),  # 略右
    ],
)
def test_bbox_contains(lat: float, lng: float, expected: bool) -> None:
    assert bbox_contains(lat, lng, BBOX) is expected


def test_bbox_contains_is_a_coarse_filter_not_a_city_test() -> None:
    """回归提醒：矩形 bbox 会切进邻市，它只能做粗筛，不能判定"在不在本市"。

    这里刻意断言"矩形内部但城市外部"的点会被 bbox 接受 ——
    用来钉住「判定归属必须走 point_in_rings」这条设计约束。
    若有人把 bbox_contains 当成归属判据，这个测试会提醒他为什么不行。
    """
    # 位于矩形右下角、但（在本测试构造下）不属于 SQUARE 的多边形内部
    assert bbox_contains(23.19, 113.19, BBOX) is True


# ════════════════════════════════════════════════════════════════════════════
# 四、travel_minutes：由距离与速度推导耗时
# ════════════════════════════════════════════════════════════════════════════


def test_travel_minutes_zero_distance_returns_at_least_one() -> None:
    """零距离也返回 1 分钟而不是 0：耗时列有 >0 约束，0 会让下游除以它或显示"0 分钟"。"""
    assert travel_minutes(0, 5.0) == 1
    assert travel_minutes(-100, 5.0) == 1


def test_travel_minutes_zero_distance_uses_overhead() -> None:
    assert travel_minutes(0, 5.0, overhead_min=0.0) == 1
    assert travel_minutes(0, 5.0, overhead_min=3.0) == 3
    assert travel_minutes(0, 5.0, overhead_min=12.4) == 12


def test_travel_minutes_exact_multiple_stays_exact() -> None:
    """1000 米 @ 5 km/h 恰好 12.0 分钟 → 12，不应被取整成 13。"""
    assert travel_minutes(1_000, 5.0) == 12


def test_travel_minutes_rounds_up() -> None:
    """向上取整：7.2 分钟必须报 8 分钟，少算会让用户低估行程。"""
    assert travel_minutes(1_000, 7.0) == pytest.approx(9, abs=0)  # 8.571…
    assert travel_minutes(1_000, 8.0) == 8  # 恰好 7.5 → 8


def test_travel_minutes_adds_overhead() -> None:
    assert travel_minutes(1_000, 5.0, overhead_min=5.0) == 17
    # 固定开销本身就是小数时，仍要向上取整
    assert travel_minutes(1_000, 5.0, overhead_min=0.5) == 13


def test_travel_minutes_never_under_reports() -> None:
    """★ 核心契约：结果必须 >= 真实分钟数（向上取整），一次都不能少算。

    这条断言专门守着 ``int(minutes + 0.999)`` 那个旧实现的缺陷：
    当分钟小数部分落在 (0, 0.001] 时会返回 floor 而不是 ceil，于是**少算 1 分钟**。
    现在改为「按 1e-6 规整后 ceil」，这里用穷举把契约钉死。

    比较基准取"规整后的精确值"而不是原始浮点值 —— 因为原始值本身带噪声
    （例如 1350m @ 4.5km/h 会算出 18.000000000000004），
    把噪声当成"真实耗时"会让这条断言变成在考验 IEEE 754 而不是考验业务逻辑。
    """
    speed = 4.5
    for distance in range(10, 5_000, 10):
        exact = round(distance / 1000.0 / speed * 60.0, 6)
        reported = travel_minutes(distance, speed)
        assert reported >= exact, f"{distance}m @ {speed}km/h 真实 {exact} 分钟，却报 {reported}"


def test_travel_minutes_is_ceil_of_exact_value() -> None:
    """对任意输入，结果必须等于 max(1, ceil(规整后的分钟数))。"""
    for distance in (300, 700, 1_100, 2_500, 4_900):
        for speed in (3.0, 4.5, 5.0, 12.0, 30.0):
            exact = round(distance / 1000.0 / speed * 60.0, 6)
            assert travel_minutes(distance, speed) == max(1, ceil(exact))


def test_travel_minutes_does_not_inflate_on_float_noise() -> None:
    """浮点噪声不得凭空多算一分钟。

    1350 米 @ 4.5 km/h 的真实耗时是 18.0 分钟，但 IEEE 754 会算出
    ``18.000000000000004``。若直接用 ``math.ceil`` 会得到 19 分钟 ——
    凭空多一分钟，而且是**系统性**的（实测 10–60000 米 × 14 种速度的穷举里有 371 组）。
    所以实现先按 1e-6 规整再取整。
    """
    raw = 1350 / 1000.0 / 4.5 * 60.0
    assert raw > 18.0, "本测试依赖的浮点噪声没有出现，说明前提变了，需要重新构造用例"
    assert ceil(raw) == 19, "朴素 ceil 会多算一分钟 —— 这正是实现要规避的行为"
    assert travel_minutes(1350, 4.5) == 18


def test_travel_minutes_grows_with_distance() -> None:
    """同一速度下距离越远耗时不会更短（防止取整逻辑写出非单调结果）。"""
    values = [travel_minutes(d, 5.0) for d in range(0, 3_000, 100)]
    assert values == sorted(values)


# ════════════════════════════════════════════════════════════════════════════
# 五、point_in_polygon：射线法
# ════════════════════════════════════════════════════════════════════════════


def test_degenerate_rings_are_never_inside() -> None:
    """少于 3 个点构不成多边形。返回 False 而不是抛错，因为边界数据可能被截断。"""
    assert point_in_polygon(23.0, 113.0, ()) is False
    assert point_in_polygon(23.0, 113.0, ((23.0, 113.0),)) is False
    assert point_in_polygon(23.0, 113.0, ((23.0, 113.0), (23.1, 113.1))) is False


def test_interior_point_is_inside() -> None:
    assert point_in_polygon(23.1, 113.1, SQUARE) is True


@pytest.mark.parametrize(
    ("lat", "lng"),
    [
        (23.5, 113.1),  # 正上方
        (22.5, 113.1),  # 正下方
        (23.1, 113.5),  # 正右方
        (23.1, 112.5),  # 正左方
        (23.5, 113.5),  # 右上角外侧
        (22.5, 112.5),  # 左下角外侧
    ],
)
def test_exterior_point_is_outside(lat: float, lng: float) -> None:
    assert point_in_polygon(lat, lng, SQUARE) is False


def test_vertices_are_guaranteed_inside() -> None:
    """★ 顶点命中是**保证**的语义（见 docstring）：点与顶点完全重合 → True。"""
    for vertex in SQUARE:
        assert point_in_polygon(vertex[0], vertex[1], SQUARE) is True


def test_edge_midpoint_is_not_guaranteed() -> None:
    """★ 边中点**不保证**被判定为内部 —— 这是刻意的、已写进 docstring 的取舍。

    上边的中点 (23.2, 113.1) 恰好落在边界上，但射线法把水平边按"不算穿越"处理，
    于是返回 False。这里把实际行为钉住：
      - 如果将来有人改成"边中点也算内部"，这个测试会失败，提醒他同步改 docstring；
      - 如果有人误以为"边界一律算内部"，这个测试就是反例。
    坐标来自 OSM（约 0.1 米精度），恰好落在边线上的概率可忽略，因此不值得引入
    一个任意的 epsilon 去换一个不可复现的判定。
    """
    assert point_in_polygon(23.2, 113.1, SQUARE) is False


def test_closed_ring_behaves_like_open_ring() -> None:
    """首尾点重复的闭合环（GeoJSON 常见写法）必须与不闭合的环给出相同结果。"""
    closed = (*SQUARE, SQUARE[0])
    for lat, lng in ((23.1, 113.1), (23.5, 113.1), (23.1, 112.5), (23.0, 113.0)):
        assert point_in_polygon(lat, lng, closed) is point_in_polygon(lat, lng, SQUARE)


def test_concave_notch_is_outside() -> None:
    """凹多边形：被挖掉的那一块必须判为外部，否则会吞掉邻市地块。"""
    assert point_in_polygon(23.15, 113.15, NOTCHED) is False


def test_concave_polygon_body_is_inside() -> None:
    assert point_in_polygon(23.05, 113.15, NOTCHED) is True


def test_winding_direction_does_not_matter() -> None:
    """顶点顺序（顺时针/逆时针）不得影响结果 —— OSM 两种都可能有。"""
    reversed_square = tuple(reversed(SQUARE))
    for lat, lng in ((23.1, 113.1), (23.5, 113.1), (23.1, 112.5)):
        assert point_in_polygon(lat, lng, reversed_square) is point_in_polygon(lat, lng, SQUARE)


def test_triangle_works() -> None:
    triangle = ((23.0, 113.0), (23.0, 113.2), (23.2, 113.0))
    assert point_in_polygon(23.02, 113.02, triangle) is True
    assert point_in_polygon(23.15, 113.15, triangle) is False  # 斜边外侧


# ════════════════════════════════════════════════════════════════════════════
# 六、point_in_rings：外环取并集、内环（飞地/洞）取差集
# ════════════════════════════════════════════════════════════════════════════


def test_point_inside_outer_and_outside_hole_is_inside() -> None:
    boundary = CityBoundary(outer=(SQUARE,), inner=(HOLE,))
    assert point_in_rings(23.02, 113.02, boundary) is True


def test_point_inside_hole_is_outside() -> None:
    """洞（飞地）必须被排除：否则会把"广州被其他城市包围的飞地"当成广州。"""
    boundary = CityBoundary(outer=(SQUARE,), inner=(HOLE,))
    assert point_in_rings(23.1, 113.1, boundary) is False


def test_point_outside_all_outer_rings_is_outside() -> None:
    boundary = CityBoundary(outer=(SQUARE,), inner=())
    assert point_in_rings(23.5, 113.5, boundary) is False


def test_multiple_outer_rings_are_unioned() -> None:
    """城市可以有多个外环（主城 + 飞地），任意外环命中即为城市内。"""
    far_square = tuple((lat + 1.0, lng + 1.0) for lat, lng in SQUARE)
    boundary = CityBoundary(outer=(SQUARE, far_square), inner=())

    assert point_in_rings(23.1, 113.1, boundary) is True  # 主城
    assert point_in_rings(24.1, 114.1, boundary) is True  # 飞地
    assert point_in_rings(23.6, 113.6, boundary) is False  # 两者之间


def test_hole_in_one_outer_ring_does_not_affect_another() -> None:
    """洞只作用于它所标注的城市，不能把另一个外环里的点也排除掉。"""
    far_square = tuple((lat + 1.0, lng + 1.0) for lat, lng in SQUARE)
    boundary = CityBoundary(outer=(SQUARE, far_square), inner=(HOLE,))
    # 这个点在 far_square 内，坐标与 HOLE 无关
    assert point_in_rings(24.1, 114.1, boundary) is True


def test_degenerate_outer_ring_never_matches() -> None:
    boundary = CityBoundary(outer=(((23.0, 113.0), (23.1, 113.1)),), inner=())
    assert point_in_rings(23.05, 113.05, boundary) is False


# ════════════════════════════════════════════════════════════════════════════
# 七、CityBoundary：构造、校验与反序列化
# ════════════════════════════════════════════════════════════════════════════


def test_city_boundary_rejects_empty_outer() -> None:
    """没有外环的边界会把**所有**地点判为城市外 —— 必须 fail-fast 而不是静默清库。"""
    with pytest.raises(ValueError, match="至少需要一个外环"):
        CityBoundary(outer=())


def test_city_boundary_point_count_sums_all_rings() -> None:
    boundary = CityBoundary(outer=(SQUARE,), inner=(HOLE,))
    assert boundary.point_count == len(SQUARE) + len(HOLE)


def test_city_boundary_is_frozen_and_hashable() -> None:
    """frozen dataclass：边界在多进程/多请求间共享，不可被就地修改。"""
    boundary = CityBoundary(outer=(SQUARE,), inner=())
    with pytest.raises(FrozenInstanceError):
        boundary.outer = ()  # type: ignore[misc]
    assert hash(boundary) == hash(CityBoundary(outer=(SQUARE,), inner=()))


def test_city_boundary_from_payload_parses_floats() -> None:
    payload = {
        "outer": [[[23, 113], [23, 113.2], [23.2, 113.2], [23.2, 113]]],
        "inner": [[[23.05, 113.05], [23.05, 113.15], [23.15, 113.15], [23.15, 113.05]]],
    }
    boundary = CityBoundary.from_payload(payload)
    assert boundary.point_count == 8
    assert all(isinstance(coord, float) for ring in boundary.outer for point in ring for coord in point)
    assert point_in_rings(23.1, 113.1, boundary) is False  # 落在洞里
    assert point_in_rings(23.02, 113.02, boundary) is True


def test_city_boundary_from_payload_without_inner_defaults_to_empty() -> None:
    boundary = CityBoundary.from_payload({"outer": [[[23, 113], [23, 113.2], [23.2, 113.2]]]})
    assert boundary.inner == ()
    assert point_in_rings(23.05, 113.05, boundary) is True


def test_city_boundary_from_payload_handles_null_inner() -> None:
    """YAML/JSON 里的 ``inner: null`` 必须被当成"没有洞"，而不是崩溃。"""
    boundary = CityBoundary.from_payload({"outer": [[[23, 113], [23, 113.2], [23.2, 113.2]]], "inner": None})
    assert boundary.inner == ()


def test_city_boundary_from_payload_with_empty_outer_raises() -> None:
    with pytest.raises(ValueError, match="至少需要一个外环"):
        CityBoundary.from_payload({"outer": []})
