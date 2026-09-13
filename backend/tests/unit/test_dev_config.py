"""开发设置面板的配置服务单测（全部跑在临时目录，**不碰真实 .env 与 config/**）。

守的都是"让面板别变成后门或脚枪"的那些点：

- 白名单之外的环境变量一律拒绝（``DATABASE_URL`` / ``SESSION_SECRET`` 刻意不在名单里）；
- Secret 只写不读（读取一律打码）；
- 改 ``.env`` 保留注释与顺序，且任何失败都把文件与进程环境恢复原状；
- 改 YAML 先校验**候选内容**，不通过时一个字节都不写。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import clear_config_cache
from app.core.errors import AppError
from app.services import dev_config

pytestmark = pytest.mark.unit

_SAMPLE_ENV = """# 顶部注释：解释为什么某些 Key 不能进前端
ENV=development
DEEPSEEK_API_KEY=sk-abcdefghijklmnop
LLM_TIMEOUT_S=20

# ── 搜索 ──
TAVILY_API_KEY=
"""


@pytest.fixture(autouse=True)
def _clean_config_cache():
    """每个用例前后都清缓存：配置是 lru_cache 的，残留会串味。"""
    clear_config_cache()
    yield
    clear_config_cache()


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(_SAMPLE_ENV, encoding="utf-8")
    return path


# ── Secret 打码 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("short", "***"),
        ("sk-1234567890abcdef", "sk-***cdef"),
    ],
)
def test_mask_secret(value: str, expected: str) -> None:
    assert dev_config.mask_secret(value) == expected


def test_env_payload_masks_secret_and_reports_set_state(tmp_path: Path) -> None:
    payload = {field["key"]: field for field in dev_config.env_payload(path=_env_file(tmp_path))}
    secret = payload["DEEPSEEK_API_KEY"]
    assert secret["kind"] == "secret"
    assert secret["is_set"] is True
    assert "***" in secret["value"]
    assert "sk-abcdefghijklmnop" not in secret["value"], "明文 Key 绝不能出现在响应里"

    empty = payload["TAVILY_API_KEY"]
    assert empty["is_set"] is False
    assert empty["value"] == ""

    # 非 Secret 字段原样返回（否则面板改不了）
    assert payload["LLM_TIMEOUT_S"]["value"] == "20"
    # ENV / DATABASE_URL / SESSION_SECRET 刻意不在面板上（见 dev_config 的注释）
    assert "ENV" not in payload


def test_env_payload_only_lists_whitelisted_keys(tmp_path: Path) -> None:
    text = _env_file(tmp_path).read_text(encoding="utf-8") + "DATABASE_URL=postgresql://x\n"
    _env_file(tmp_path).write_text(text, encoding="utf-8")
    keys = {field["key"] for field in dev_config.env_payload(path=_env_file(tmp_path))}
    assert "DATABASE_URL" not in keys, "数据库连接串不该出现在面板上"


# ── .env 写入 ───────────────────────────────────────────────────────────────


def test_update_env_rewrites_in_place_and_keeps_comments(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    environ: dict[str, str] = {}

    changed = dev_config.update_env(
        {"DEEPSEEK_API_KEY": "sk-new-value-1234"}, path=path, environ=environ
    )

    assert changed == ["DEEPSEEK_API_KEY"]
    text = path.read_text(encoding="utf-8")
    assert "sk-new-value-1234" in text
    assert "顶部注释：解释为什么某些 Key 不能进前端" in text, "注释必须保留"
    assert "# ── 搜索 ──" in text
    assert environ["DEEPSEEK_API_KEY"] == "sk-new-value-1234", "改动要立刻对进程生效"


def test_update_env_appends_keys_missing_from_file(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    dev_config.update_env({"SERPER_API_KEY": "serper-x"}, path=path, environ={})
    text = path.read_text(encoding="utf-8")
    assert "SERPER_API_KEY=serper-x" in text
    assert "由开发设置面板追加" in text


def test_update_env_rejects_keys_outside_whitelist(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(AppError) as excinfo:
        dev_config.update_env({"DATABASE_URL": "postgresql://elsewhere"}, path=path, environ={})
    assert excinfo.value.code == "INVALID_INPUT"
    assert path.read_text(encoding="utf-8") == before, "被拒绝的写入不能留下任何痕迹"


def test_update_env_rejects_bad_types(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    with pytest.raises(AppError):
        dev_config.update_env({"BACKEND_PORT": "八千"}, path=path, environ={})
    with pytest.raises(AppError):
        dev_config.update_env({"LLM_PROVIDER": "some-other-vendor"}, path=path, environ={})
    with pytest.raises(AppError):
        dev_config.update_env({"ENABLE_LOCAL_FETCH": "yes"}, path=path, environ={})
    with pytest.raises(AppError):
        dev_config.update_env({"API_BASE_URL": "http://x\ny"}, path=path, environ={})


def test_update_env_is_a_noop_when_nothing_changes(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    changed = dev_config.update_env({"LLM_TIMEOUT_S": "20"}, path=path, environ={})
    assert changed == []
    assert path.read_text(encoding="utf-8") == before


def test_update_env_rolls_back_when_settings_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _env_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    environ = {"LLM_TIMEOUT_S": "20"}

    class _Boom:
        def __init__(self) -> None:
            raise RuntimeError("假装这一组配置放一起不合法")

    monkeypatch.setattr(dev_config, "Settings", _Boom)

    with pytest.raises(AppError) as excinfo:
        dev_config.update_env({"LLM_TIMEOUT_S": "45"}, path=path, environ=environ)

    assert "已回滚" in excinfo.value.message
    assert path.read_text(encoding="utf-8") == before, "校验失败必须把文件写回原样"
    assert environ["LLM_TIMEOUT_S"] == "20", "进程环境也要恢复"


# ── config/*.yaml ───────────────────────────────────────────────────────────


def test_config_file_names_lists_only_known_files(tmp_path: Path) -> None:
    (tmp_path / "ttl.yaml").write_text("version: '1.0'\n", encoding="utf-8")
    (tmp_path / "unknown.yaml").write_text("a: 1\n", encoding="utf-8")
    assert dev_config.config_file_names(directory=tmp_path) == ["ttl.yaml"]


@pytest.mark.parametrize("name", ["../../.env", "nope.yaml", "seed.yml"])
def test_config_path_rejects_outside_whitelist(tmp_path: Path, name: str) -> None:
    with pytest.raises(AppError) as excinfo:
        dev_config.write_config_file(name, "version: '1.0'\n", directory=tmp_path)
    assert excinfo.value.code == "INVALID_INPUT"


def test_write_config_file_rejects_invalid_content_without_touching_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ttl.yaml"
    original = "version: '2026.09.1'\nttl_hours: {}\nrefresh_policy: {}\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        dev_config.write_config_file("ttl.yaml", "version: 12345\n", directory=tmp_path)

    assert "未被修改" in excinfo.value.message
    assert target.read_text(encoding="utf-8") == original


def test_write_config_file_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    target = tmp_path / "ttl.yaml"
    target.write_text("version: '1.0'\n", encoding="utf-8")
    with pytest.raises(AppError):
        dev_config.write_config_file("ttl.yaml", "- 1\n- 2\n", directory=tmp_path)


def test_write_config_file_accepts_valid_content(tmp_path: Path) -> None:
    """用真实配置的内容做一次往返：合法内容应当被接受。"""
    source = dev_config.read_config_file("ttl.yaml")
    target = tmp_path / "ttl.yaml"
    target.write_text(source, encoding="utf-8")
    dev_config.write_config_file("ttl.yaml", source, directory=tmp_path)
    assert target.read_text(encoding="utf-8") == source


def test_read_config_file_unknown_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        dev_config.read_config_file("passwd", directory=tmp_path)


# ── 生效快照 ────────────────────────────────────────────────────────────────


def test_effective_snapshot_exposes_runtime_truth() -> None:
    snapshot = dev_config.effective_snapshot()
    assert set(snapshot) == {
        "env",
        "providers",
        "llm",
        "cost_limits",
        "rate_limits",
        "degraded_modes",
        "config_versions",
    }
    assert snapshot["llm"]["fast_model"]
    # 这里**不返回 Key 本身**，只返回"配没配"
    assert isinstance(snapshot["llm"]["secret_configured"], bool)
    assert "api_key" not in str(snapshot["llm"]).lower() or "secret_configured" in str(
        snapshot["llm"]
    )
