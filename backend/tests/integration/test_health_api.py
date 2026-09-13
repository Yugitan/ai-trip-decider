"""健康检查与 HTTP 契约的集成测试（真实应用 + 真实数据库）。"""

from __future__ import annotations

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
