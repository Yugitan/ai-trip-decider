"""HTTP 层的**错误处理与故障路径**集成测试。

为什么单独一个文件：
    正常路径的契约已经由 ``test_health_api.py`` 与 ``test_catalog_api.py`` 覆盖。
    这里专门守**出问题的时候**的行为 —— 而这类代码在真实环境里几乎不会被触发，
    一旦写错（比如忘了包 envelope、泄露了堆栈、错误码判反），
    往往要到线上出故障时才被发现。

    覆盖三类：
    1. **中间件与安全响应头**：CORS 必须在外层，否则前端读到的是"跨域被拦"
       而不是我们精心写的错误提示；未捕获异常不得泄露堆栈。
    2. **404/405 的路径语义错误码**：未知路径报"行程不存在"会把排查方向带偏。
    3. **故障注入**：数据库不可用、迁移未跑时，/health 必须**如实降级**而不是假装健康。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db

pytestmark = pytest.mark.integration

CITY = "guangzhou"


# ════════════════════════════════════════════════════════════════════════════
# 工具：可控的假数据库会话
# ════════════════════════════════════════════════════════════════════════════


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value

    def all(self) -> list[Any]:
        return []


class _FakeSession:
    """按 SQL 内容决定成功或失败的假会话，用于触发 health 的故障分支。"""

    def __init__(self, *, connect_ok: bool, schema_ok: bool) -> None:
        self._connect_ok = connect_ok
        self._schema_ok = schema_ok

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _FakeResult:
        sql = str(statement)
        if sql.strip().upper().startswith("SELECT 1"):
            if not self._connect_ok:
                raise OSError("connection refused (simulated)")
            return _FakeResult(1)
        if not self._schema_ok:
            raise RuntimeError('relation "places" does not exist (simulated)')
        return _FakeResult(7)


def _client_with_fake_db(*, connect_ok: bool, schema_ok: bool) -> TestClient:
    """构造一个把数据库依赖替换成假会话的应用。"""
    from app.main import create_app

    app: FastAPI = create_app()
    session = _FakeSession(connect_ok=connect_ok, schema_ok=schema_ok)

    async def _fake_db() -> AsyncIterator[_FakeSession]:
        yield session

    app.dependency_overrides[get_db] = _fake_db
    return TestClient(app)


# ════════════════════════════════════════════════════════════════════════════
# 一、中间件：请求上下文、CORS 与安全响应头
# ════════════════════════════════════════════════════════════════════════════


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.headers["X-Request-Id"], "缺少 X-Request-Id，用户反馈时无法定位"


def test_request_id_header_is_echoed(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-Id": "trace-abc-123"})
    assert response.headers["X-Request-Id"] == "trace-abc-123"


def test_elapsed_header_is_present_and_numeric(client: TestClient) -> None:
    value = client.get("/api/v1/health").headers["X-Elapsed-Ms"]
    assert value.isdigit()


def test_cors_headers_are_present_on_success(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Origin": "http://localhost:3000"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_headers_are_present_on_error_responses(client: TestClient) -> None:
    """★ CORS 必须在**外层**，否则前端拿到的是浏览器的"跨域被拦"，
    而不是我们精心写的 INVALID_INPUT 提示 —— 用户只会看到一个空白页。

    这条断言实际钉住的是中间件的注册顺序：如果哪天有人在
    ``create_app`` 里把 CORS 挪到自定义中间件之前，这条测试会失败。
    """
    response = client.get(
        f"/api/v1/cities/{CITY}/places?category=not-a-category",
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 422
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_cors_headers_are_present_on_unhandled_errors() -> None:
    """未捕获异常走到最外层 ServerErrorMiddleware 时，CORS 头同样不能丢。"""
    from app.main import create_app

    app = create_app()

    @app.get("/api/v1/_boom_for_cors")
    async def _boom() -> None:
        raise RuntimeError("simulated boom")

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/api/v1/_boom_for_cors", headers={"Origin": "http://localhost:3000"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_security_headers_are_present_on_error_responses(client: TestClient) -> None:
    """安全响应头必须对所有响应生效，包括错误响应。"""
    for path in ("/api/v1/nope", "/api/v1/places/not-a-uuid"):
        headers = client.get(path).headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


def test_csp_header_is_only_added_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """CSP 只在生产环境注入（开发环境需要支持 Next.js 的热更新与内联脚本）。"""
    from app import main as main_module
    from app.core.config import Settings

    dev_app = main_module.create_app()
    with TestClient(dev_app) as dev_client:
        assert "Content-Security-Policy" not in dev_client.get("/api/v1/health").headers

    prod_settings = Settings(
        _env_file=None,
        env="production",
        session_secret="a-real-secret",
        admin_token="tok",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: prod_settings)
    prod_app = main_module.create_app()
    with TestClient(prod_app) as prod_client:
        headers = prod_client.get("/api/v1/health").headers
        assert "Content-Security-Policy" in headers
        assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_docs_are_disabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境不暴露 /docs：它会把全部端点与参数结构对外公开。"""
    from app import main as main_module
    from app.core.config import Settings

    prod_settings = Settings(
        _env_file=None,
        env="production",
        session_secret="a-real-secret",
        admin_token="tok",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: prod_settings)
    with TestClient(main_module.create_app()) as c:
        assert c.get("/docs").status_code == 404
        # openapi.json 保留：它是前端类型生成的来源，且不含业务数据
        assert c.get("/openapi.json").status_code == 200


def test_slow_request_is_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """慢请求的判定分支：耗时 > 3000ms 时要记录告警并如实写入响应头。

    这里替换的是 ``app.main`` 模块里的 ``time`` 名字，而不是 stdlib 的
    ``time.perf_counter`` —— 后者是全局共享的，pytest/anyio 内部也会调用它，
    会让"第几次调用"变得不可预测。
    """

    class _FakeTime:
        def __init__(self) -> None:
            self.calls = 0

        def perf_counter(self) -> float:
            self.calls += 1
            return 0.0 if self.calls == 1 else 5.0

    from app import main as main_module

    monkeypatch.setattr(main_module, "time", _FakeTime())
    with TestClient(main_module.create_app()) as c:
        response = c.get("/api/v1/health")
    assert response.headers["X-Elapsed-Ms"] == "5000"


# ════════════════════════════════════════════════════════════════════════════
# 二、未捕获异常：统一 envelope + 不泄露堆栈
# ════════════════════════════════════════════════════════════════════════════


def test_unhandled_exception_returns_envelope_without_leaking_details() -> None:
    """★ 任何 5xx 都不得向用户暴露堆栈（PRD §6 FR-13.2）。

    同时验证：中间件记下了异常日志、ServerErrorMiddleware 给出了统一 envelope。
    """
    from app.main import create_app

    app = create_app()

    @app.get("/api/v1/_boom")
    async def _boom() -> None:
        raise RuntimeError("simulated-secret-detail")

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/api/v1/_boom", headers={"X-Request-Id": "boom-trace"})

    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INTERNAL"
    assert body["error"]["hint"], "错误必须给出可执行建议，而不是一句技术术语"

    text = response.text
    for leak in ("simulated-secret-detail", "Traceback", "RuntimeError", 'File "'):
        assert leak not in text, f"响应泄露了内部实现细节：{leak}"


def test_unhandled_exception_still_returns_request_id() -> None:
    """异常路径也要带上 request_id —— 否则用户报障时我们无从查起。"""
    from app.main import create_app

    app = create_app()

    @app.get("/api/v1/_boom_rid")
    async def _boom() -> None:
        raise ValueError("boom")

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/api/v1/_boom_rid", headers={"X-Request-Id": "rid-keep-me"})

    assert response.headers["X-Request-Id"] == "rid-keep-me"
    assert response.json()["meta"]["request_id"] == "rid-keep-me"


# ════════════════════════════════════════════════════════════════════════════
# 三、404 / 405 的路径语义错误码
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ("/api/v1/public/share-abc", "SHARE_NOT_FOUND"),
        ("/api/v1/trips/abc123", "TRIP_NOT_FOUND"),
        ("/api/v1/trip/abc123", "TRIP_NOT_FOUND"),
        ("/api/v1/places/abc/extra", "PLACE_NOT_FOUND"),
        ("/api/v1/definitely-unknown", "NOT_FOUND"),
    ],
)
def test_404_error_code_matches_path_semantics(client: TestClient, path: str, expected_code: str) -> None:
    """★ 未知路径一律报"行程不存在"会把排查方向带偏，前端也会显示错误的提示文案。"""
    response = client.get(path)
    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == expected_code, f"{path} 的错误码不对：{body['error']['code']}"
    assert body["error"]["context"]["path"] == path


def test_404_envelope_has_all_required_keys(client: TestClient) -> None:
    body = client.get("/api/v1/nope").json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert set(body["error"]) == {"code", "message", "hint", "context"}


def test_method_not_allowed_uses_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/health")
    assert response.status_code == 405
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_INPUT"


def test_framework_5xx_is_normalized_to_internal_code() -> None:
    """框架抛出的 5xx（如依赖方声明的 503）不能把原始状态码当成错误码透出去 ——
    前端只认我们定义的错误码枚举，收到 "503" 会走不到任何分支。
    """
    from fastapi import HTTPException

    from app.main import create_app

    app = create_app()

    @app.get("/api/v1/_http_503")
    async def _boom() -> None:
        raise HTTPException(status_code=503, detail="upstream unavailable")

    with TestClient(app) as c:
        response = c.get("/api/v1/_http_503")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "INTERNAL"
    assert body["error"]["context"]["status"] == 503


def test_framework_4xx_keeps_the_message_but_normalizes_the_code() -> None:
    """4xx 的 detail 是开发者的说明文字，可以透出；但错误码必须是枚举值。"""
    from fastapi import HTTPException

    from app.main import create_app

    app = create_app()

    @app.get("/api/v1/_http_403")
    async def _forbidden() -> None:
        raise HTTPException(status_code=403, detail="not allowed here")

    with TestClient(app) as c:
        response = c.get("/api/v1/_http_403")

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert body["error"]["message"] == "not allowed here"


def test_unknown_city_error_includes_context(client: TestClient) -> None:
    """错误上下文要带上出错的 slug，便于定位是哪个城市没数据。"""
    body = client.get("/api/v1/cities/shenzhen/places").json()
    assert body["error"]["code"] == "UNSUPPORTED_CITY"
    assert body["error"]["context"]["slug"] == "shenzhen"
    assert "guangzhou" in body["error"]["hint"]


def test_validation_error_reports_fields_without_echoing_body(client: TestClient) -> None:
    """★ 校验失败只暴露字段级摘要，不回显完整输入体（可能含敏感内容）。"""
    response = client.get(f"/api/v1/cities/{CITY}/places?limit=0&q={'x' * 100}")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    fields = body["error"]["context"]["fields"]
    assert fields, "校验失败必须指出是哪个字段的问题"
    assert all({"field", "problem", "type"} <= set(item) for item in fields)
    assert "x" * 100 not in response.text, "响应回显了原始输入"


# ════════════════════════════════════════════════════════════════════════════
# 四、health 的故障路径（数据库不可用 / 迁移未跑）
# ════════════════════════════════════════════════════════════════════════════


def test_health_reports_degraded_when_database_unreachable() -> None:
    """★ 数据库连不上时必须**如实报告**，绝不能假装健康。"""
    with _client_with_fake_db(connect_ok=False, schema_ok=False) as c:
        response = c.get("/api/v1/health")
    assert response.status_code == 200, "/health 永远返回 200，用 status 字段表达健康度"
    data = response.json()["data"]
    assert data["status"] == "degraded"
    assert data["database"]["ok"] is False
    assert data["database"]["latency_ms"] is None
    assert data["database"]["error"], "必须给出异常类型名，而不是一句'出错了'"


def test_health_reports_migration_pending_when_tables_missing() -> None:
    """连接正常但表不存在 → schema_state 必须是 migration_pending。"""
    with _client_with_fake_db(connect_ok=True, schema_ok=False) as c:
        data = c.get("/api/v1/health").json()["data"]
    assert data["database"]["ok"] is True
    assert data["database"]["schema_ready"] is False
    assert data["schema_state"] == "migration_pending"
    assert data["status"] == "degraded"


def test_ready_returns_503_when_degraded() -> None:
    """★ /health/ready 供编排系统判断能否接流量，依赖不可用时必须返回 503。"""
    with _client_with_fake_db(connect_ok=False, schema_ok=False) as c:
        response = c.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["data"]["status"] == "degraded"


def test_ready_returns_503_when_migration_pending() -> None:
    with _client_with_fake_db(connect_ok=True, schema_ok=False) as c:
        response = c.get("/api/v1/health/ready")
    assert response.status_code == 503


def test_health_payload_exposes_versions_and_breakers(client: TestClient) -> None:
    """配置版本与熔断阈值必须可查 —— 否则"线上到底跑的是哪份配置"无法回答。"""
    data = client.get("/api/v1/health").json()["data"]
    assert data["config"]["scoring_version"]
    assert data["config"]["limits_version"]
    assert data["config"]["ttl_version"]
    assert data["config"]["pricing_version"]
    assert data["cost_breakers"]["plan_total_cny"] > 0
    assert data["providers"]["map"] in ("amap", "osrm", "haversine")


# ════════════════════════════════════════════════════════════════════════════
# 五、目录 API 的边界场景
# ════════════════════════════════════════════════════════════════════════════


def test_empty_search_result_returns_empty_page(client: TestClient) -> None:
    """搜不到东西时必须返回空列表 + total=0，而不是报错或返回全部。

    这条同时覆盖了 ``_sources_of`` / ``_aliases_of`` 的**空输入守卫** ——
    没有命中的地点时不应该去数据库发一次 ``WHERE place_id IN ()`` 的无效查询。
    """
    data = client.get(f"/api/v1/cities/{CITY}/places?q=zzz-no-such-place-zzz").json()["data"]
    assert data["items"] == []
    assert data["page"]["total"] == 0
    assert data["page"]["has_more"] is False


def test_district_filter_returns_only_that_district(client: TestClient) -> None:
    """行政区过滤：地点详情页与"按区浏览"依赖它。"""
    data = client.get(f"/api/v1/cities/{CITY}/places?district=荔湾区&limit=20").json()["data"]
    if data["page"]["total"] == 0:
        pytest.skip("当前知识库里没有 addr:district=荔湾区的活跃地点")
    assert all(item["district"] == "荔湾区" for item in data["items"])


def test_filters_are_echoed_back_in_page_meta(client: TestClient) -> None:
    """回显生效的筛选条件，便于调用方确认参数真的被应用了。"""
    data = client.get(f"/api/v1/cities/{CITY}/places?category=cafe&q=a").json()["data"]
    assert data["filters"] == {"q": "a", "category": "cafe", "district": None}


def test_offset_beyond_total_returns_empty_page(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/places?offset=100000").json()["data"]
    assert data["items"] == []
    assert data["page"]["has_more"] is False
    assert data["page"]["total"] > 0, "total 应反映真实总数，而不是被 offset 影响"


def test_search_is_case_insensitive_for_english_names(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/places?q=CANTON").json()["data"]
    lower = client.get(f"/api/v1/cities/{CITY}/places?q=canton").json()["data"]
    assert data["page"]["total"] == lower["page"]["total"]


def test_place_detail_works_for_a_place_without_aliases(client: TestClient) -> None:
    """没有别名与额外来源的地点也必须能正常取详情（空列表而非 null）。"""
    items = client.get(f"/api/v1/cities/{CITY}/places?limit=50").json()["data"]["items"]
    assert items
    for item in items:
        detail = client.get(f"/api/v1/places/{item['id']}").json()["data"]
        assert isinstance(detail["aliases"], list)
        assert isinstance(detail["sources"], list)
        assert isinstance(detail["tags"], list)
        assert isinstance(detail["best_time"], list)


def test_route_list_without_stops_is_lighter_but_same_count(client: TestClient) -> None:
    with_stops = client.get(f"/api/v1/cities/{CITY}/routes?with_stops=true").json()["data"]
    without = client.get(f"/api/v1/cities/{CITY}/routes?with_stops=false").json()["data"]
    assert with_stops["total"] == without["total"]
    assert all(route["stops"] == [] for route in without["items"])
    assert all(route["stop_count"] >= 0 for route in without["items"])


def test_route_type_filter_unknown_value_returns_empty(client: TestClient) -> None:
    """未知 route_type 返回空列表而不是报错 —— 它是开放取值，不做白名单。"""
    data = client.get(f"/api/v1/cities/{CITY}/routes?route_type=nonexistent").json()["data"]
    assert data["items"] == []
    assert data["total"] == 0
