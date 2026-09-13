"""配置加载与校验的单元测试。

重点验证 **fail-fast**：配置错误必须直接抛错，而不是静默用默认值继续跑。
这是 PRD §11.1「权重必须可以配置，不要把所有逻辑写死在 Prompt 里」的守门测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core import config as config_module
from app.core.config import (
    ConfigError,
    Settings,
    get_limits_config,
    get_pricing_config,
    get_scoring_config,
    get_settings,
    get_ttl_config,
)

pytestmark = pytest.mark.unit


# ── 真实配置文件必须可用 ────────────────────────────────────────────────────


def test_scoring_config_loads_and_is_normalized() -> None:
    scoring = get_scoring_config()
    assert scoring.version
    # 默认权重与每个 archetype 都恰好和为 1.0
    assert abs(sum(scoring.default_weights.model_dump().values()) - 1.0) < 1e-9
    for name, archetype in scoring.archetypes.items():
        total = sum(archetype.weights.model_dump().values())
        assert abs(total - 1.0) < 1e-9, f"{name} 权重之和为 {total}"


def test_required_archetypes_present() -> None:
    scoring = get_scoring_config()
    assert set(scoring.archetypes) >= {"relaxed", "classic", "themed"}


def test_preference_dimensions_have_a_target() -> None:
    """每个偏好维度必须能落到某个分值字段或某组类别上，否则打分时无从下手。"""
    for key, dim in get_scoring_config().preference_dimensions.items():
        assert dim.field is not None or dim.categories, f"{key} 既无 field 也无 categories"


def test_limits_and_ttl_load() -> None:
    limits = get_limits_config()
    assert limits.planning.candidate_min < limits.planning.candidate_max
    assert limits.feasibility["enabled_checks"], "硬约束检查不得为空"
    for pace in ("relaxed", "balanced", "packed"):
        assert limits.walking_caps_m[pace] > 0
    ttl = get_ttl_config()
    assert ttl.ttl_for("place_opening_hours") == 48
    assert ttl.ttl_for("weather") == 3
    assert ttl.ttl_for("place_identity") == 2160


def test_pricing_config_reports_uncalibrated_honestly() -> None:
    """未校准的单价必须被如实报告，禁止把 null 当成 0 来美化成本。"""
    pricing = get_pricing_config()
    # DeepSeek 已于 2026-09-13 校准，但高德/Serper/Bing 仍是 null ——
    # 只要文件里还有任何一处 needs_calibration，整体就必须如实报 False。
    assert pricing.is_fully_calibrated is False, "高德等单价仍未校准，应为 False"
    # Tavily 的单价已从官方文档读取并填入
    assert pricing.search["tavily"]["credit_price_usd"] == pytest.approx(0.008)
    assert pricing.search["tavily"]["costs"]["search_basic"] == 1
    assert pricing.search["tavily"]["needs_calibration"] is False


def test_no_bare_null_keys_in_yaml() -> None:
    """回归：YAML 里裸写的 ``null:`` 会被解析成 None 键，导致配置校验莫名失败。"""
    from app.core.paths import config_dir

    for path in sorted(config_dir().glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert None not in _all_keys(raw), f"{path.name} 存在被解析成 None 的 YAML 键"


def _all_keys(node: Any) -> set[Any]:
    keys: set[Any] = set()
    if isinstance(node, dict):
        keys |= set(node.keys())
        for value in node.values():
            keys |= _all_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _all_keys(item)
    return keys


# ── fail-fast：权重之和必须为 1.0 ───────────────────────────────────────────


@pytest.fixture
def patch_scoring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """把 config_dir 指向临时目录，用于注入非法配置。"""
    real = config_module._load_yaml("scoring.yaml")

    def _write(payload: dict[str, Any]) -> None:
        (tmp_path / "scoring.yaml").write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path)

    return real, _write


def test_weights_not_summing_to_one_fails_fast(patch_scoring: Any) -> None:
    real, write = patch_scoring
    broken = dict(real)
    broken["default_weights"] = {**real["default_weights"], "preference": 0.99}
    write(broken)
    with pytest.raises(ConfigError) as exc:
        get_scoring_config()
    assert "权重之和必须为 1.0" in str(exc.value)


def test_archetype_weight_sum_is_checked(patch_scoring: Any) -> None:
    real, write = patch_scoring
    broken = dict(real)
    broken["archetypes"] = {
        **real["archetypes"],
        "relaxed": {**real["archetypes"]["relaxed"], "weights": {**real["archetypes"]["relaxed"]["weights"], "walking_fit": 0.9}},
    }
    write(broken)
    with pytest.raises(ConfigError):
        get_scoring_config()


def test_missing_archetype_is_rejected(patch_scoring: Any) -> None:
    real, write = patch_scoring
    broken = dict(real)
    broken["archetypes"] = {k: v for k, v in real["archetypes"].items() if k != "classic"}
    write(broken)
    with pytest.raises(ConfigError) as exc:
        get_scoring_config()
    assert "archetype" in str(exc.value)


def test_unknown_weight_key_is_rejected(patch_scoring: Any) -> None:
    """多写一个未知权重键必须报错（extra='forbid'），防止拼写错误被静默忽略。"""
    real, write = patch_scoring
    broken = dict(real)
    broken["default_weights"] = {**real["default_weights"], "typo_wight": 0.0, "preference": 0.30}
    write(broken)
    with pytest.raises(ConfigError):
        get_scoring_config()


def test_walking_cap_ratio_cannot_exceed_hard_limit(patch_scoring: Any) -> None:
    """archetype 的步行收紧系数不得超过硬约束上限，否则硬约束形同虚设。"""
    real, write = patch_scoring
    broken = dict(real)
    broken["archetypes"] = {
        **real["archetypes"],
        "relaxed": {**real["archetypes"]["relaxed"], "walking_cap_ratio": 1.4},
    }
    write(broken)
    with pytest.raises(ConfigError) as exc:
        get_scoring_config()
    assert "硬约束上限" in str(exc.value)


def test_missing_config_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path)
    with pytest.raises(ConfigError) as exc:
        get_scoring_config()
    assert "缺少配置文件" in str(exc.value)


def test_corrupt_yaml_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "scoring.yaml").write_text("a: [oops\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path)
    with pytest.raises(ConfigError) as exc:
        get_scoring_config()
    assert "解析失败" in str(exc.value)


# ── Settings：缺 Key 必须是"降级"，不是"崩溃" ──────────────────────────────


def test_settings_without_keys_reports_degraded_modes() -> None:
    """无 Key 环境必须能正常读到配置，并把降级能力如实列出来（可读、非空字符串）。"""
    settings = get_settings()
    assert settings.llm_provider_effective in ("deepseek", "openai", "anthropic", "disabled")
    assert settings.search_provider_effective in ("seed_only", "tavily", "serper", "bing")
    assert settings.map_provider_effective in ("amap", "osrm", "haversine")
    modes = settings.degraded_modes()
    assert isinstance(modes, list)
    assert all(isinstance(m, str) and m for m in modes)
    if settings.llm_provider_effective == "disabled":
        assert any(m.startswith("llm:disabled") for m in modes)
    if settings.search_provider_effective == "seed_only":
        assert any(m.startswith("search:seed_only") for m in modes)


@pytest.mark.parametrize(
    ("provider", "key_field", "expected"),
    [
        ("deepseek", "deepseek_api_key", "disabled"),
        ("openai", "openai_api_key", "disabled"),
    ],
)
def test_llm_provider_falls_back_when_key_absent(provider: str, key_field: str, expected: str) -> None:
    settings = get_settings().model_copy(update={"llm_provider": provider, key_field: None})
    assert settings.llm_provider_effective == expected


def test_search_provider_auto_without_keys_is_seed_only() -> None:
    settings = get_settings().model_copy(
        update={"search_provider": "auto", "tavily_api_key": None, "serper_api_key": None, "bing_search_api_key": None}
    )
    assert settings.search_provider_effective == "seed_only"


def test_explicit_search_provider_without_key_degrades_instead_of_crashing() -> None:
    """显式指定 tavily 但没 Key：应降级到 seed_only，而不是让服务起不来。"""
    settings = get_settings().model_copy(update={"search_provider": "tavily", "tavily_api_key": None})
    assert settings.search_provider_effective == "seed_only"


def test_amap_without_key_falls_back_to_osrm() -> None:
    settings = get_settings().model_copy(update={"map_provider": "amap", "amap_web_key": None})
    assert settings.map_provider_effective == "osrm"


def test_amap_with_key_is_used() -> None:
    settings = get_settings().model_copy(update={"map_provider": "amap", "amap_web_key": "fake-key"})
    assert settings.map_provider_effective == "amap"


def test_blank_env_values_become_none() -> None:
    """`.env` 里写 `TAVILY_API_KEY=` 应视为"未配置"，而不是空字符串 Key。"""
    normalized = Settings.model_validate(
        {"tavily_api_key": "   ", "deepseek_api_key": "", "search_provider": "auto", "map_provider": "auto"}
    )
    assert normalized.tavily_api_key is None
    assert normalized.deepseek_api_key is None
    assert normalized.search_provider_effective == "seed_only"
    assert normalized.llm_provider_effective == "disabled"


def test_production_selfcheck_blocks_dev_secrets() -> None:
    settings = get_settings().model_copy(
        update={"env": "production", "session_secret": "dev-only-change-me-in-production"}
    )
    with pytest.raises(RuntimeError, match="生产环境配置不安全"):
        settings.assert_safe_for_production()


def test_fault_injection_visible_in_degraded_modes() -> None:
    settings = get_settings().model_copy(update={"fault_injection": "llm_timeout"})
    assert any("fault_injection" in mode for mode in settings.degraded_modes())
