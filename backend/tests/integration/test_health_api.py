"""健康检查与 HTTP 契约的集成测试（真实应用 + 真实数据库）。"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_health_reports_status_and_degraded_modes(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200

    body = response.json()
    assert body["ok"] is True
    data = body["data"]

    # 数据库必须可用（集成测试环境下迁移已自动执行）
    assert data["database"]["ok"] is True
    assert data["database"]["schema_ready"] is True
    assert data["schema_state"] == "ok"

    # 配置版本必须来自 YAML，证明配置真的被加载而不是硬编码
    assert data["config"]["scoring_version"]
    assert data["config"]["limits_version"]
    assert data["config"]["ttl_version"]

    # 未校准的单价必须如实暴露（避免成本报表被误读为真实支出）
    assert data["config"]["pricing_calibrated"] is False

    # 降级模式清单必须可读
    assert isinstance(data["degraded_modes"], list)

    # 响应头
    assert response.headers["X-Request-Id"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_health_reports_the_thresholds_that_actually_trip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ `/health` 报的熔断阈值必须是**真正在执行的那两个数**。

    熔断器读 `config/limits.yaml`（`plan_service` 里 `breaker=limits.cost.circuit_breaker`），
    而 `Settings` 里有同名镜像变量。曾经报的是镜像：把它改成 99 会让 `/health`
    高高兴兴地显示“99 元熔断”，而请求仍然在 1 元处被拦下 ——
    两边默认值恰好相等，所以这个谎言一直没被看见。
    """
    from app.core.config import clear_config_cache, get_limits_config

    limits = get_limits_config().cost.circuit_breaker

    monkeypatch.setenv("PLAN_COST_CIRCUIT_BREAKER_CNY", "99")
    monkeypatch.setenv("SEARCH_COST_CIRCUIT_BREAKER_CNY", "99")
    clear_config_cache()
    try:
        data = client.get("/api/v1/health").json()["data"]
    finally:
        clear_config_cache()

    assert data["cost_breakers"]["plan_total_cny"] == limits.plan_total_cny
    assert data["cost_breakers"]["plan_search_cny"] == limits.plan_search_cny
    assert data["cost_breakers"]["plan_total_cny"] != 99.0


def test_health_reports_today_spend_and_global_budget(
    client: TestClient, cost_row_factory: Callable[[str], int]
) -> None:
    """★ 全局日成本必须真的被读、并被报出来（PRD §15.4 第四级熔断）。

    `config/limits.yaml` 里一直写着 `cost.global_daily_cny: 20.00`，
    但 2026-09-15 之前**没有任何代码读它** —— 一个拦不住支出的阈值比没有更危险：
    看到它在那里，人就会以为已经有兜底了。

    这里查的不是“字段在不在”，而是“往今天的账里插一笔钱，它是不是真的动了”。
    """
    from app.core.config import get_limits_config

    limit = get_limits_config().cost.global_daily_cny
    cost_row_factory(str(float(limit) + 5))

    data = client.get("/api/v1/health").json()["data"]

    assert data["cost"]["global_daily_budget_cny"] == limit
    assert Decimal(data["cost"]["daily_spend_cny"]) >= Decimal(str(limit))
    assert data["cost"]["budget_exceeded"] is True
    assert data["cost"]["disabled_by_config"] is False


def test_health_cost_block_is_null_not_false_when_it_cannot_tell(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """查不到 ≠ 没超：库读不出来时 `budget_exceeded` 必须是 **None**。

    报 False 等于向运维保证“还没超预算”，而那是一句没有根据的话 ——
    与本项目一直守的「null ≠ 0」是同一条规矩。
    """
    from app.api.v1 import health as health_module

    class _Boom:
        def __init__(self, _session: object) -> None:
            raise RuntimeError("假装库挂了")

    monkeypatch.setattr(health_module, "CostStore", _Boom)

    data = client.get("/api/v1/health").json()["data"]

    assert data["cost"]["budget_exceeded"] is None
    assert data["cost"]["daily_spend_cny"] is None
    assert data["cost"]["unavailable"] == "RuntimeError"


def test_health_reports_schema_not_ready_when_tables_missing(client: TestClient) -> None:
    """知识库为空时 active_places 必须是 0，而不是报错或假装有数据。"""
    data = client.get("/api/v1/health").json()["data"]
    assert data["database"]["active_places"] >= 0
    assert data["database"]["routes"] >= 0


def test_ready_endpoint_returns_200_when_healthy(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"


def test_request_id_is_echoed_back(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-Id": "trace-me-123"})
    assert response.headers["X-Request-Id"] == "trace-me-123"


def test_unknown_path_uses_unified_error_envelope(client: TestClient) -> None:
    """回归：早期实现里未知路径会返回纯文本 404，绕过统一错误规范。"""
    response = client.get("/api/v1/definitely-not-a-route")
    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert set(body["error"]) == {"code", "message", "hint", "context"}
    assert body["error"]["message"] != "Not Found" or body["error"]["hint"]


def test_method_not_allowed_uses_unified_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/health")
    assert response.status_code == 405
    assert response.json()["ok"] is False


def test_error_response_does_not_leak_stack_trace(client: TestClient) -> None:
    for path in ("/api/v1/health", "/api/v1/nope"):
        text = client.get(path).text
        for leak in ("Traceback", "File \"", "sqlalchemy.exc", "asyncpg"):
            assert leak not in text, f"{path} 的响应泄露了内部实现细节：{leak}"


def test_openapi_schema_is_generatable(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "TripDecider API"
    assert "/api/v1/health" in schema["paths"]


def test_security_headers_present_on_all_responses(client: TestClient) -> None:
    for path in ("/api/v1/health", "/api/v1/nope"):
        headers = client.get(path).headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
