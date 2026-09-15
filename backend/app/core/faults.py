"""故障注入开关（PRD §23.5、§26 验收第 17 项）。

★ 为什么故障注入要写进产品代码，而不是只写在测试里 ★
"外部服务挂了会怎样" 是这个产品最需要被验证的一类行为（降级是这个项目的主线），
而用 mock 替换整个 Provider 时，测到的是**测试自己搭的假世界**：
假 Provider 不会经过装配工厂、不会经过检索链的成本熔断、
更不会触发"降级清单要如实写进响应"这条纪律。
``FAULT_INJECTION=<名字>`` 让真实的装配路径原样跑起来，只在**接缝**上扎一刀。

三条纪律：

1. **只注入在接缝上**：Provider 边界（L6/L7/L8）、请求级会话入口、限流器、
   全局日成本、站点快照查询。``app/domain/`` 下的算法一行都不改 ——
   它们必须是纯函数，一旦为了"可注入"引入分支，纯度守卫测试就该红了。
2. **未知的故障名不是"什么都没发生"**：那等于你以为在测故障、其实什么都没测，
   比不测更危险 —— 因此启动时 ``validate_fault_name`` 直接失败（fail-fast）。
3. **生产禁止设置**：由 ``Settings.assert_safe_for_production`` 拦截（已有测试）。

注入是**一次性**的：它跟随 ``Settings``（进程级），而不是某个请求 ——
本文件刻意不做"只影响第 N 次调用"这种花活，那种语义会让测试结果依赖执行顺序。
"""

from __future__ import annotations

from app.core.config import Settings, get_settings

__all__ = [
    "DB_SLOW_DELAY_S",
    "FAULT_NAMES",
    "active_fault",
    "fault_active",
    "validate_fault_name",
]

#: PRD §23.5 列出的全部故障名。名字与文档逐字对应，方便对照验收表。
FAULT_NAMES: frozenset[str] = frozenset(
    {
        "llm_timeout",
        "llm_invalid_json",
        "llm_500",
        "search_500",
        "search_empty",
        "search_timeout",
        "map_500",
        "map_timeout",
        "db_down",
        "db_slow",
        "place_missing",
        "route_empty",
        "rate_limit",
        "cost_breaker",
    }
)

#: ``db_slow`` 模拟的慢查询时长（PRD 写的是 3s）。常量而非配置：
#: 它只是一个测试用的等待，做成可配置只会多一个能配错的地方。
DB_SLOW_DELAY_S = 3.0


def active_fault(settings: Settings | None = None) -> str | None:
    """当前生效的故障名（未设置或空串都返回 ``None``）。"""
    raw = (settings or get_settings()).fault_injection
    if raw is None:
        return None
    name = raw.strip()
    return name or None


def fault_active(name: str, *, settings: Settings | None = None) -> bool:
    """该故障是否生效。**不校验名字合法**（那是启动时 ``validate_fault_name`` 的事）。"""
    return active_fault(settings) == name


def validate_fault_name(name: str | None) -> None:
    """启动自检：设置了一个不认识的故障名就直接失败。

    失败而不是忽略，理由见模块 docstring 第 2 条：静默忽略会让"我测过故障降级了"
    变成一句无法证伪的话。
    """
    if name is None or not name.strip():
        return
    cleaned = name.strip()
    if cleaned not in FAULT_NAMES:
        raise ValueError(
            f"未知的 FAULT_INJECTION：{cleaned!r}。可选值：{', '.join(sorted(FAULT_NAMES))}"
        )
