"""OSM ``opening_hours`` 解析（`app/domain/opening_hours.py`，TASKS.md B6）。

这个解析器的风险不在"算错"，而在**猜**：``opening_hours`` 是一门小语言，
节假日、季节、跨零点、``sunrise-sunset`` 都有各自的语义。一个猜错的开放时间
比"未知"糟得多 —— 未知会带上一句"出发前请确认"，猜错会把用户送到关着门的景点。

所以测试分成两半，两半同等重要：
1. **支持的写法必须解对**（含 ``off`` 那天要能与"未知"区分开）；
2. **不支持的写法必须返回 None**，尤其不能"大致能用就先用着"。
"""

from __future__ import annotations

import pytest

from app.domain.models import DAY_MINUTES, OpeningHours
from app.domain.opening_hours import SUPPORTED_EXAMPLE, parse_opening_hours

pytestmark = pytest.mark.unit


def _windows(hours: OpeningHours, day: int) -> tuple[tuple[int, int], ...] | None:
    windows = hours.windows_at(day)
    if windows is None:
        return None
    return tuple((window.open_min, window.close_min) for window in windows)


def test_weekday_range_and_closed_days() -> None:
    """``Mo-Fr 09:00-17:00; Sa 10:00-14:00; Su off``：工作日/周六/周日三种状态。"""
    hours = parse_opening_hours(SUPPORTED_EXAMPLE)
    assert hours is not None

    assert _windows(hours, 0) == ((9 * 60, 17 * 60),)  # 周一
    assert _windows(hours, 4) == ((9 * 60, 17 * 60),)  # 周五
    assert _windows(hours, 5) == ((10 * 60, 14 * 60),)  # 周六
    assert _windows(hours, 6) == ()  # 周日：确定闭馆（≠ 未知）
    assert hours.is_unknown is False


def test_days_never_mentioned_are_closed_not_unknown() -> None:
    """只写了一天的规则：其余六天是**确定闭馆**，不是"不知道"。"""
    hours = parse_opening_hours("We 14:00-18:00")
    assert hours is not None
    assert _windows(hours, 2) == ((14 * 60, 18 * 60),)
    assert _windows(hours, 0) == ()
    assert _windows(hours, 6) == ()


def test_day_lists_and_wrap_around_ranges() -> None:
    """``Mo,We,Fr`` 与跨周的 ``Sa-Mo`` 都要支持（周末连着周一开门的店很常见）。"""
    listed = parse_opening_hours("Mo,We,Fr 08:00-12:00")
    assert listed is not None
    assert [_windows(listed, day) for day in range(7)] == [
        ((480, 720),),
        (),
        ((480, 720),),
        (),
        ((480, 720),),
        (),
        (),
    ]

    wrapped = parse_opening_hours("Sa-Mo 10:00-20:00")
    assert wrapped is not None
    assert _windows(wrapped, 5) == ((600, 1200),)  # 周六
    assert _windows(wrapped, 6) == ((600, 1200),)  # 周日
    assert _windows(wrapped, 0) == ((600, 1200),)  # 周一
    assert _windows(wrapped, 1) == ()  # 周二


def test_multiple_windows_per_day_and_times_only_rule() -> None:
    """中午休息（两个区间）与"不写星期 = 每天"两种常见写法。"""
    split_day = parse_opening_hours("Mo-Su 09:00-12:00,13:00-17:00")
    assert split_day is not None
    assert _windows(split_day, 3) == ((9 * 60, 12 * 60), (13 * 60, 17 * 60))

    no_days = parse_opening_hours("09:00-17:00")
    assert no_days is not None
    assert all(_windows(no_days, day) == ((540, 1020),) for day in range(7))


def test_24_7_and_24_00_end() -> None:
    """``24/7``（全天）与 ``24:00`` 结尾（内部记 1440）都不许被当成"跨零点"。"""
    always = parse_opening_hours("24/7")
    assert always is not None
    assert _windows(always, 0) == ((0, DAY_MINUTES),)

    till_midnight = parse_opening_hours("Mo-Su 10:00-24:00")
    assert till_midnight is not None
    assert _windows(till_midnight, 2) == ((600, DAY_MINUTES),)

    # 结束位置写 00:00 的写法（真实数据里有）同样理解为午夜；
    # 但开头的 00:00 仍是"从零点开始"，不靠猜来定方向。
    zero_end = parse_opening_hours("Mo-Sa,Su 12:00-00:00")
    assert zero_end is not None
    assert _windows(zero_end, 0) == ((720, DAY_MINUTES),)
    zero_start = parse_opening_hours("Mo-Fr 00:00-08:00")
    assert zero_start is not None
    assert _windows(zero_start, 0) == ((0, 480),)


def test_later_rule_wins_for_the_same_day() -> None:
    """同一天的更具体规则覆盖前面那条（``Mo-Fr ...; Fr ...`` 里周五取后者）。"""
    hours = parse_opening_hours("Mo-Fr 09:00-17:00; Fr 09:00-12:00")
    assert hours is not None
    assert _windows(hours, 4) == ((540, 720),)


def test_quoted_notes_are_comments_not_data() -> None:
    """引号里是备注：剥掉之后照常解析，而不是让整条规则作废。"""
    hours = parse_opening_hours('Mo-Su 09:00-18:00 "节假日闭馆"')
    assert hours is not None
    assert _windows(hours, 6) == ((540, 1080),)


@pytest.mark.parametrize(
    "raw",
    [
        "sunrise-sunset",
        "Mo-Su 10:00-02:00",  # 跨零点：本模型表示不了
        "05:59-00:09",  # 跨零点（凌晨关门的店）
        "06:05-24:20",  # 脏数据：24 点只有 24:00 合法
        "Mo-Fr 09:00-17:00; PH off",  # 节假日
        "Jun-Sep 09:00-18:00",  # 月份
        "week 1-53 Mo 09:00-17:00",  # 第几周
        "Mo 09:00-17:00 || Tu 10:00-16:00",  # 备选语法
        "Mo-Su 09:00-12:00;;",  # 空规则 = 没读懂
        "by appointment",
        '""',
        "Mo-Su",
        "Mo-Su 09:00",
    ],
)
def test_unsupported_syntax_is_unknown_not_guessed(raw: str) -> None:
    """看不懂就返回 None —— 猜一个"大致对"的时间比未知更危险。"""
    assert parse_opening_hours(raw) is None


def test_missing_data_is_unknown() -> None:
    """没有原始数据、或只有空白：未知（而不是"每天闭馆"）。"""
    assert parse_opening_hours(None) is None
    assert parse_opening_hours("   ") is None
    parsed = parse_opening_hours(SUPPORTED_EXAMPLE)
    assert parsed is not None and parsed.is_unknown is False
