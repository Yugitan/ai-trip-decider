"""开发设置面板 API 的集成测试（真实 ASGI 应用）。

★ 这个文件守的是"面板不会伤害别人"★
它比"面板好不好用"重要得多：一个能改 .env 的接口，一旦访问控制写错就是后门。
因此这里**只读**真实配置、只发会被拒绝的写请求 ——
写文件的正确性全部由 ``tests/unit/test_dev_config.py`` 在临时目录里验证，
两者的分工是：单测证明"写对了"，集成测试证明"没被写坏"。

覆盖面：
- 读取：可编辑清单 + Secret 打码 + 当前生效快照；
- 白名单：``DATABASE_URL`` 这类键必须被拒（且不留痕迹）；
- 路径：不认识的配置文件（含路径穿越意图）必须被拒；
- 访问控制：配了 ``ADMIN_TOKEN`` 之后，没有正确 header 一律 403。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import clear_config_cache, get_settings

pytestmark = pytest.mark.integration


def test_read_config_lists_editable_items_and_masks_secrets(client: TestClient) -> None:
    response = client.get("/api/v1/dev/config")
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["notice"], "必须有一段说明告诉开发者这不是用户功能"
    keys = {field["key"] for field in data["env"]}
    assert "DEEPSEEK_API_KEY" in keys
    assert "DATABASE_URL" not in keys
    assert "SESSION_SECRET" not in keys

    secret = next(field for field in data["env"] if field["key"] == "DEEPSEEK_API_KEY")
    raw_key = get_settings().deepseek_api_key or ""
    if raw_key:
        assert raw_key not in secret["value"], "明文 Key 绝不能出现在响应里"
        assert "***" in secret["value"]

    names = {item["name"] for item in data["config_files"]}
    assert {"scoring.yaml", "limits.yaml", "ttl.yaml", "pricing.yaml", "seed.yaml"} <= names
    assert data["effective"]["providers"]["llm"]


def test_env_update_with_no_changes_is_a_safe_noop(client: TestClient) -> None:
    """空更新走完整条写路径但不改任何东西 —— 用来证明工具链是通的。"""
    response = client.put("/api/v1/dev/env", json={"updates": {}})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["changed"] == []


def test_env_update_rejects_non_whitelisted_keys(client: TestClient) -> None:
    response = client.put(
        "/api/v1/dev/env", json={"updates": {"DATABASE_URL": "postgresql://elsewhere/db"}}
    )
    # 422 是本项目的约定：INVALID_INPUT 一律映射成 422（与 /trips:plan 一致）
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert "DATABASE_URL" in body["error"]["message"]
    # 真实配置没被动过
    assert "/elsewhere/" not in (get_settings().database_url or "")


def test_config_update_rejects_unknown_file_name(client: TestClient) -> None:
    response = client.put(
        "/api/v1/dev/config/not-a-config.yaml", json={"content": "version: '1.0'\n"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_config_update_rejects_invalid_yaml_without_writing(client: TestClient) -> None:
    """非法内容必须被拒，而且**磁盘上的文件必须与请求前完全一致**。"""
    before = client.get("/api/v1/dev/config").json()["data"]["config_files"]
    current = next(item["content"] for item in before if item["name"] == "ttl.yaml")

    response = client.put(
        "/api/v1/dev/config/ttl.yaml", json={"content": "version: 12345\n"}
    )
    assert response.status_code == 422, response.text
    assert "未被修改" in response.json()["error"]["message"]

    after = client.get("/api/v1/dev/config").json()["data"]["config_files"]
    assert next(item["content"] for item in after if item["name"] == "ttl.yaml") == current


def test_admin_token_is_required_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了 ADMIN_TOKEN 之后：没有 / 错误的 header 一律 403，正确的才放行。"""
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret-token")
    clear_config_cache()
    try:
        assert client.get("/api/v1/dev/config").status_code == 403
        assert (
            client.get("/api/v1/dev/config", headers={"X-Admin-Token": "wrong"}).status_code
            == 403
        )
        ok = client.get("/api/v1/dev/config", headers={"X-Admin-Token": "s3cret-token"})
        assert ok.status_code == 200, ok.text
    finally:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        clear_config_cache()
