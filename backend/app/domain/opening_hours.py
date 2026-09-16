"""把 OSM 的 ``opening_hours`` 文本解析成结构化的营业时间（TASKS.md B6）。

★ 为什么只做"能保证正确"的那一部分 ★
``opening_hours`` 是一门小语言：节假日（``PH``）、季节与月份、第几周、
``sunrise-sunset``、跨零点区间（``22:00-02:00``）、备选语法（``||``）、
"需预约"……全都真实存在，而且**每一种都有各自的语义**。

所以这里的原则是：**看不懂就返回 ``None``（未知），绝不猜。**
宁可让一个地点继续显示"营业时间未知"，也不要给它编一个看起来精确的
"周一至周日 09:00-17:00" —— 一个错的营业时间会让规划把用户送到关着门的景点，
比"未知"糟得多（未知至少会带上一句"出发前请确认"）。

支持的部分（覆盖了绝大多数实际写法）：
- ``24/7``
- 星期：``Mo`` / ``Mo-Fr`` / ``Mo,We,Fr`` / ``Mo-Su``（``-`` 支持跨周，如 ``Sa-Mo``）
- 时间：``HH:MM-HH:MM``，一天多个区间（``09:00-12:00,13:00-17:00``），
  ``24:00`` 结尾（= 当天 24 点，内部记 1440）
- 某天关门：``Mo-Fr 09:00-17:00; Sa-Su off``
- 只写时间不写星期：``09:00-17:00``（等同 ``Mo-Su``）
- 引号内的备注会被剥掉（``"仅限工作日"`` 是注释，不是数据）

三种状态必须分清（它们对应的用户提示完全不同）：
- ``None``：不知道（该地点没有原始数据，或用了不支持的写法）
- 某天 → ``()``：那天**确定闭馆**
- 某天 → 时段元组：那天的开放时段
"""

from __future__ import annotations

import re

from app.domain.models import DAY_MINUTES, OpeningHours, OpeningWindow

__all__ = ["SUPPORTED_EXAMPLE", "parse_opening_hours"]

#: 便于测试与文档引用的一个"标准写法"示例。
SUPPORTED_EXAMPLE = "Mo-Fr 09:00-17:00; Sa 10:00-14:00; Su off"

_DAY_INDEX: dict[str, int] = {
    "mo": 0,
    "tu": 1,
    "we": 2,
    "th": 3,
    "fr": 4,
    "sa": 5,
    "su": 6,
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
_DAY_RE = re.compile(r"^(mo|tu|we|th|fr|sa|su)$")
_QUOTED_RE = re.compile(r'"[^"]*"')

#: 明确表示"关门/不营业"的写法。
_CLOSED_WORDS = frozenset({"off", "closed"})


def _parse_day_spec(spec: str) -> list[int] | None:
    """``Mo-Fr`` / ``Mo,We`` → 星期索引列表；看不懂返回 ``None``。"""
    days: list[int] = []
    for raw_token in spec.split(","):
        token = raw_token.strip().lower()
        if not token:
            return None
        if "-" in token:
            start_text, _, end_text = token.partition("-")
            start, end = start_text.strip(), end_text.strip()
            if start not in _DAY_INDEX or end not in _DAY_INDEX:
                return None
            first, last = _DAY_INDEX[start], _DAY_INDEX[end]
            if first <= last:
                days.extend(range(first, last + 1))
            else:  # 跨周：Sa-Mo = 周六、周日、周一
                days.extend(range(first, 7))
                days.extend(range(0, last + 1))
            continue
        if _DAY_RE.fullmatch(token) is None:
            return None  # PH / 月份 / 第几周……一律不猜
        days.append(_DAY_INDEX[token])
    return days


def _parse_time_spec(spec: str) -> tuple[OpeningWindow, ...] | None:
    """``09:00-17:00`` / ``09:00-12:00,13:00-17:00`` → 开放时段；看不懂返回 ``None``。"""
    windows: list[OpeningWindow] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        match = _TIME_RE.match(part)
        if match is None:
            return None  # sunrise-sunset / 需预约 / “09:00+” 之类
        open_hour, open_minute, close_hour, close_minute = (int(g) for g in match.groups())
        if open_minute > 59 or close_minute > 59 or open_hour > 24 or close_hour > 24:
            return None
        # 24 点只有 ``24:00`` 合法：``24:08`` 是脏数据（真实数据里就有），
        # 放过去会让 close_min 超出一天的长度。
        if (open_hour == 24 and open_minute) or (close_hour == 24 and close_minute):
            return None
        open_min = open_hour * 60 + open_minute
        close_min = close_hour * 60 + close_minute
        if close_min == 0 and open_min > 0:
            # ``12:00-00:00``：结束时刻写 ``00:00`` 的写法在真实数据里就是"到午夜"
            # （OSM 自己的约定是用 ``24:00``，但填写者两种都在用）。
            # 只有在**结束**位置且开始不为 0 时才这样理解；``00:00-08:00`` 仍是"从零点开始"。
            close_min = DAY_MINUTES
        if close_min <= open_min:
            # 跨零点（22:00-02:00）在本模型里表示不了（一天被切成两段会算错闭馆判断）
            return None
        windows.append(OpeningWindow(open_min=open_min, close_min=close_min))
    return tuple(windows) if windows else None


def parse_opening_hours(raw: str | None) -> OpeningHours | None:
    """解析 OSM ``opening_hours``。看不懂 → ``None``（未知），不猜。"""
    if raw is None:
        return None
    # 引号里是备注，不是数据；剥掉之后才做语法判断
    text = _QUOTED_RE.sub(" ", raw).strip()
    if not text:
        return None

    rules = [rule.strip() for rule in text.split(";")]
    if any(not rule for rule in rules):
        return None  # 空规则（``;;``）意味着我们没读懂这段文本

    #: OSM 语义里没提到的星期 = 那一天不营业
    weekly: dict[int, tuple[OpeningWindow, ...]] = {}
    all_day = (OpeningWindow(open_min=0, close_min=DAY_MINUTES),)
    parsed_any = False

    for rule in rules:
        if rule.lower() == "24/7":
            for day in range(7):
                weekly[day] = all_day
            parsed_any = True
            continue

        head, _, rest = rule.partition(" ")
        head = head.strip()
        rest = rest.strip()
        if rest:
            days = _parse_day_spec(head)
            spec = rest
        else:
            # 只写时间（= 每天）或只写 ``off``
            days = list(range(7))
            spec = head
        if days is None:
            return None

        if spec.lower() in _CLOSED_WORDS:
            for day in days:
                weekly[day] = ()
            parsed_any = True
            continue

        try:
            windows = _parse_time_spec(spec)
        except ValueError:
            # 奇形怪状的时段（比如模型约束以外的时间）一律降级为"未知"：
            # 这个函数的返回值会走进一次真实规划，**不许**让外部文本把它炸掉。
            return None
        if windows is None:
            return None
        for day in days:
            # 同一天的后续规则覆盖前一条：``Mo-Fr 09:00-17:00; Fr 09:00-12:00``
            # 里周五以更具体的那条为准（与 OSM 的"后面的规则更优先"一致）。
            weekly[day] = windows
        parsed_any = True

    if not parsed_any:
        return None
    return OpeningHours(by_weekday={day: weekly.get(day, ()) for day in range(7)})
