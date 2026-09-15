"""配置与人工数据 Schema 的 **fail-fast 分支**单元测试。

为什么单独一个文件：
    ``tests/unit/test_config.py`` 守的是"真实配置文件能正常加载"，
    而这个文件守的是**反面** —— 每一处校验在遇到非法配置时必须**真的报错**。

    这一点比看上去重要：一个写错了的校验器（比如条件写反、异常消息拼错、
    或者干脆漏了 raise）在真实配置下**永远不会暴露**，因为真实配置是合法的。
    等 M2 引入新的评分公式、M3 引入新的 Provider 配置时才会踩到 ——
    那时排查成本高得多。所以这里用"注入非法值 → 断言必被拒绝"的方式逐条钉死。

覆盖范围：``app/core/config.py`` 的 Settings / ScoringConfig / LimitsConfig /
SeedConfig 全部校验分支，以及 ``app/schemas/curated.py`` 的人工数据校验分支。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.core import config as config_module
from app.core.config import (
    IMPLEMENTED_SEARCH_PROVIDERS,
    SEARCH_KEY_FIELDS,
    AdminConfig,
    BudgetFitFormula,
    ConfigError,
    CoordConfig,
    EfficiencyFormula,
    LimitsConfig,
    PlanningLimits,
    PopularityFormula,
    PreferenceDimension,
    PreferenceFormula,
    PricingConfig,
    RelevanceConfig,
    ScoringConfig,
    SeedConfig,
    Settings,
    TagSignal,
    VerificationRules,
    Weights,
    get_scoring_config,
)
from app.core.paths import config_dir
from app.schemas.curated import (
    CuratedPlace,
    CuratedPlaceFile,
    CuratedRoute,
    CuratedRouteFile,
    RouteStop,
)

pytestmark = pytest.mark.unit


# ── 工具 ────────────────────────────────────────────────────────────────────


def raw(name: str) -> dict[str, Any]:
    """读取真实配置文件的原始字典（用它做基线再注入非法值）。"""
    data = yaml.safe_load((config_dir() / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} 的顶层不是映射，测试前提失效"
    return data


def make_settings(**overrides: Any) -> Settings:
    """构造一个不受 `.env` 与**已导出的环境变量**影响的 Settings 实例。

    `_env_file=None` 只挡住 `.env` **文件**，挡不住 shell 里 export 出来的变量 ——
    pydantic-settings 总会读环境变量，且环境变量优先于 `.env`。
    本机把 `DEEPSEEK_API_KEY` 导出到 shell 之后，「配了 OpenAI 的 Key 但 provider 指向
    deepseek，仍应判定为未配置」这条用例立刻变红：它断言的是"没有 Key 的行为"，
    却把环境里的 Key 当成了输入（与 `test_providers._settings()` 修过的 #57 同一个坑）。
    所以这里显式清空全部 Key，让用例的输入完全由它自己决定。
    """
    base: dict[str, Any] = {
        "deepseek_api_key": None,
        "openai_api_key": None,
        "anthropic_api_key": None,
        "tavily_api_key": None,
        "serper_api_key": None,
        "bing_search_api_key": None,
        "amap_web_key": None,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_curated_place(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "陈家祠"}
    payload.update(overrides)
    return payload


def make_curated_route(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "slug": "test-route",
        "name": "测试路线",
        "route_type": "classic",
        "stops": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
    }
    payload.update(overrides)
    return payload


# ════════════════════════════════════════════════════════════════════════════
# 一、Settings：环境变量与降级判定
# ════════════════════════════════════════════════════════════════════════════


def test_blank_string_becomes_none() -> None:
    """`.env` 里 `TAVILY_API_KEY=` 表示"未配置"，不是"配置成了空字符串"。"""
    settings = make_settings(tavily_api_key="", deepseek_api_key="   ")
    assert settings.tavily_api_key is None
    assert settings.deepseek_api_key is None


def test_non_dict_input_passes_through_then_fails_validation() -> None:
    """before 校验器对非 dict 输入必须原样返回，让 pydantic 给出正常的类型错误。

    如果这里抛 AttributeError，用户看到的会是"内部错误"而不是"配置格式不对"。
    """
    with pytest.raises(ValidationError):
        Settings.model_validate(object())


@pytest.mark.parametrize("provider", ["disabled", "null", "none"])
def test_llm_provider_explicitly_disabled(provider: str) -> None:
    assert make_settings(llm_provider=provider).llm_provider_effective == "disabled"


def test_llm_provider_without_key_degrades_to_disabled() -> None:
    """缺 Key 不是错误，是降级 —— 绝不允许因此启动失败。"""
    assert make_settings(llm_provider="deepseek", deepseek_api_key=None).llm_provider_effective == "disabled"


@pytest.mark.parametrize(
    ("provider", "key_field"),
    [("deepseek", "deepseek_api_key"), ("openai", "openai_api_key"), ("anthropic", "anthropic_api_key")],
)
def test_llm_provider_with_key_is_effective(provider: str, key_field: str) -> None:
    settings = make_settings(llm_provider=provider, **{key_field: "sk-test"})
    assert settings.llm_provider_effective == provider


def test_llm_provider_with_key_for_a_different_provider_stays_disabled() -> None:
    """配了 OpenAI 的 Key 但 provider 指向 deepseek，仍应判定为未配置。"""
    settings = make_settings(llm_provider="deepseek", openai_api_key="sk-test")
    assert settings.llm_provider_effective == "disabled"


@pytest.mark.parametrize("provider", ["seed_only", "disabled"])
def test_search_provider_explicitly_offline(provider: str) -> None:
    assert make_settings(search_provider=provider).search_provider_effective == "seed_only"


def test_search_provider_auto_picks_first_available() -> None:
    settings = make_settings(search_provider="auto", tavily_api_key="tvly-test")
    assert settings.search_provider_effective == "tavily"


def test_search_provider_auto_without_keys_falls_back_to_seed_only() -> None:
    assert make_settings(search_provider="auto").search_provider_effective == "seed_only"


def test_search_provider_explicit_but_keyless_degrades() -> None:
    """显式指定 tavily 但没有 Key → 降级到 seed_only，而不是崩溃。"""
    assert make_settings(search_provider="tavily").search_provider_effective == "seed_only"


def test_search_provider_explicit_with_key_is_effective() -> None:
    settings = make_settings(search_provider="tavily", tavily_api_key="tvly-test")
    assert settings.search_provider_effective == "tavily"


def test_search_provider_never_reports_an_unimplemented_provider() -> None:
    """★ 契约：effective 只允许返回**代码里真的实现了**的名字 ★

    `serper` / `bing` 的 Key 在 `.env` 与 /dev 面板里都存在（预留给以后），
    而代码里只有 `tavily` 与 `seed_only` 两个实现。
    以前的行为是：`auto` + Serper Key → 返回 `"serper"`，
    而 `registry._build_search()` 照样返回 `SeedOnlyProvider` ——
    于是 `/health`、dev 面板的生效快照都会说「搜索：serper」，实际却在跑 seed_only。
    这条测试把「配置说谁在跑」与「工厂真跑谁」钉在一起（同类思路见 R14/R12）。
    """
    for name, field in SEARCH_KEY_FIELDS.items():
        if name in IMPLEMENTED_SEARCH_PROVIDERS:
            continue
        assert make_settings(search_provider="auto", **{field: "test-key"}).search_provider_effective == "seed_only"
        assert make_settings(search_provider=name, **{field: "test-key"}).search_provider_effective == "seed_only"


def test_search_provider_prefers_implemented_over_unimplemented() -> None:
    """同时配了 tavily 与 serper 的 Key 时，必须选真的能用的那个。"""
    settings = make_settings(
        search_provider="auto", tavily_api_key="tvly-test", serper_api_key="serper-test"
    )
    assert settings.search_provider_effective == "tavily"


def test_ignored_search_keys_are_reported_not_swallowed() -> None:
    """配了但未实现的 Key 必须能被说出来，否则用户只能对着填了 Key 的输入框猜。"""
    settings = make_settings(serper_api_key="serper-test", bing_search_api_key="bing-test")
    assert settings.ignored_search_keys() == ["serper", "bing"]
    assert any(mode.startswith("search:serper+bing(") for mode in settings.degraded_modes())
    # 已实现的 Provider 不算「被忽略」；没配 Key 时也不该冒出这条降级提示
    assert make_settings(tavily_api_key="tvly-test").ignored_search_keys() == []
    assert not any("已预留" in mode for mode in make_settings().degraded_modes())


@pytest.mark.parametrize("provider", ["haversine", "disabled"])
def test_map_provider_explicitly_offline(provider: str) -> None:
    """显式要求离线估算时，绝不偷偷发网络请求。"""
    assert make_settings(map_provider=provider).map_provider_effective == "haversine"


def test_map_provider_amap_with_key_is_effective() -> None:
    assert make_settings(map_provider="amap", amap_web_key="amap-test").map_provider_effective == "amap"


def test_map_provider_auto_with_amap_key_prefers_amap() -> None:
    assert make_settings(map_provider="auto", amap_web_key="amap-test").map_provider_effective == "amap"


def test_map_provider_amap_without_key_degrades_to_osrm() -> None:
    """显式要 amap 但没 Key → 降级到真实路网（OSRM），而不是崩溃或退回估算。"""
    assert make_settings(map_provider="amap").map_provider_effective == "osrm"


def test_map_provider_default_is_osrm() -> None:
    assert make_settings().map_provider_effective == "osrm"


def test_degraded_modes_reports_weather_disabled() -> None:
    settings = make_settings(weather_provider="disabled")
    assert any(mode.startswith("weather:disabled") for mode in settings.degraded_modes())


@pytest.mark.parametrize("value", ["null", "disabled"])
def test_degraded_modes_reports_all_weather_off_aliases(value: str) -> None:
    assert any(
        mode.startswith("weather:disabled") for mode in make_settings(weather_provider=value).degraded_modes()
    )


def test_degraded_modes_lists_fault_injection() -> None:
    """故障注入开着时必须显式暴露 —— 否则线上会出现无法解释的行为。"""
    settings = make_settings(fault_injection="db_down")
    assert any("fault_injection:db_down" in mode for mode in settings.degraded_modes())


@pytest.mark.parametrize("env", ["production", "prod", "PROD"])
def test_is_production_recognizes_aliases(env: str) -> None:
    assert make_settings(env=env).is_production is True


@pytest.mark.parametrize("env", ["development", "dev", "staging"])
def test_is_production_rejects_non_production(env: str) -> None:
    assert make_settings(env=env).is_production is False


def test_production_safety_check_raises_on_dev_secret() -> None:
    settings = make_settings(env="production", session_secret="dev-only-change-me-in-production")
    with pytest.raises(RuntimeError, match="生产环境配置不安全"):
        settings.assert_safe_for_production()


@pytest.mark.parametrize("token", ["", None])
def test_production_safety_check_raises_on_missing_admin_token(token: str | None) -> None:
    """★ 回归：这里曾经写成 ``admin_token == ""``，而 `_blank_to_none` 已把空串
    归一化成 None —— 于是这条检查**永远不会触发**，后台会在生产环境静默地无保护。
    现在改成 `not self.admin_token`，空串与未设置都会被拦下。
    """
    settings = make_settings(env="production", session_secret="a-real-secret", admin_token=token)
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        settings.assert_safe_for_production()


def test_production_safety_check_raises_on_fault_injection() -> None:
    settings = make_settings(
        env="production", session_secret="a-real-secret", admin_token="tok", fault_injection="db_down"
    )
    with pytest.raises(RuntimeError, match="FAULT_INJECTION"):
        settings.assert_safe_for_production()


def test_development_safety_check_does_not_raise() -> None:
    """开发环境只告警不阻断 —— 否则本地根本起不来。"""
    make_settings(
        env="development", session_secret="dev-only-change-me-in-production"
    ).assert_safe_for_production()


def test_production_safety_check_passes_with_safe_values() -> None:
    make_settings(
        env="production", session_secret="a-real-secret", admin_token="tok", fault_injection=None
    ).assert_safe_for_production()


# ════════════════════════════════════════════════════════════════════════════
# 二、ScoringConfig：权重、公式、archetype
# ════════════════════════════════════════════════════════════════════════════


def test_weights_reject_value_out_of_range() -> None:
    """权重必须落在 [0, 1]：负数或 >1 的权重会让排序结果无法解释。"""
    payload = dict(raw("scoring.yaml")["default_weights"])
    payload["preference"] = 1.5
    with pytest.raises(ValidationError, match=r"必须在 \[0, 1\] 区间内"):
        Weights.model_validate(payload)


def test_weights_reject_negative_value() -> None:
    payload = dict(raw("scoring.yaml")["default_weights"])
    payload["preference"] = -0.1
    with pytest.raises(ValidationError, match=r"必须在 \[0, 1\] 区间内"):
        Weights.model_validate(payload)


def test_weights_reject_unknown_key() -> None:
    payload = dict(raw("scoring.yaml")["default_weights"])
    payload["typo_weight"] = 0.0
    with pytest.raises(ValidationError):
        Weights.model_validate(payload)


def test_weights_accept_small_floating_point_drift() -> None:
    """权重之和允许 1e-6 以内的浮点误差，否则合法的十进制小数会被误杀。"""
    payload = {
        "preference": 0.25,
        "efficiency": 0.15,
        "time_fit": 0.15,
        "popularity": 0.15,
        "budget_fit": 0.10,
        "walking_fit": 0.10,
        "place_relation": 0.10,
    }
    assert Weights.model_validate(payload)


def test_preference_dimension_requires_a_target() -> None:
    """偏好维度必须能落到某个分值字段或某组类别上，否则打分时无从下手。"""
    with pytest.raises(ValidationError, match="必须指定 field 或 categories"):
        PreferenceDimension.model_validate({"label": "美食"})


def test_preference_dimension_accepts_field_target() -> None:
    assert PreferenceDimension.model_validate({"label": "美食", "field": "food"})


def test_preference_dimension_accepts_categories_target() -> None:
    assert PreferenceDimension.model_validate({"label": "美食", "categories": ["food", "cafe"]})


def test_preference_formula_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match=r"mean_weight \+ coverage_weight 必须为 1.0"):
        PreferenceFormula.model_validate(
            {
                "mean_weight": 0.9,
                "coverage_weight": 0.5,
                "coverage_min_dim_score": 0.6,
                "coverage_min_stops": 2,
                "unknown_score_default": 0.5,
            }
        )


def test_efficiency_formula_requires_good_less_than_bad() -> None:
    """good_travel_ratio 必须小于 bad_travel_ratio，否则打分函数的方向就反了。"""
    with pytest.raises(ValidationError, match="good_travel_ratio 必须小于 bad_travel_ratio"):
        EfficiencyFormula.model_validate(
            {
                "good_travel_ratio": 0.8,
                "bad_travel_ratio": 0.4,
                "reversal_penalty": 0.1,
                "reversal_penalty_cap": 0.5,
            }
        )


def test_popularity_formula_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match=r"mean_weight \+ max_weight 必须为 1.0"):
        PopularityFormula.model_validate({"mean_weight": 0.3, "max_weight": 0.3})


@pytest.mark.parametrize("field", ["under_bonus_ratio", "over_zero_point"])
def test_budget_fit_formula_ranges(field: str) -> None:
    payload = dict(raw("scoring.yaml")["formulas"]["budget_fit"])
    payload[field] = 0.0
    with pytest.raises(ValidationError):
        BudgetFitFormula.model_validate(payload)


def test_scoring_config_requires_all_archetypes() -> None:
    payload = deepcopy(raw("scoring.yaml"))
    payload["archetypes"].pop("classic")
    with pytest.raises(ValidationError, match="缺少必需的 archetype"):
        ScoringConfig.model_validate(payload)


def test_scoring_config_rejects_walking_cap_above_hard_limit() -> None:
    """archetype 的步行收紧系数不得超过硬约束上限，否则硬约束形同虚设。

    取值 1.4 是刻意的：它通过字段级的 ``le=1.5`` 校验，只可能被**模型级**的
    交叉校验拦下 —— 这样才真正测到了"配置之间的一致性检查"，而不是字段范围。
    """
    payload = deepcopy(raw("scoring.yaml"))
    hard_limit = payload["formulas"]["walking_fit"]["hard_limit_ratio"]
    payload["archetypes"]["relaxed"]["walking_cap_ratio"] = hard_limit + 0.1
    with pytest.raises(ValidationError, match="超过了硬约束上限"):
        ScoringConfig.model_validate(payload)


def test_scoring_config_rejects_unknown_top_level_key() -> None:
    payload = deepcopy(raw("scoring.yaml"))
    payload["unexpected_section"] = {}
    with pytest.raises(ValidationError):
        ScoringConfig.model_validate(payload)


def test_weights_for_unknown_archetype_returns_defaults() -> None:
    """未知 archetype 不能抛错 —— 上游可能传了历史值，必须优雅退到默认权重。"""
    scoring = get_scoring_config()
    assert scoring.weights_for("no-such-archetype") == scoring.default_weights


def test_weights_for_known_archetype_returns_its_own_weights() -> None:
    scoring = get_scoring_config()
    assert scoring.weights_for("relaxed") == scoring.archetypes["relaxed"].weights


# ════════════════════════════════════════════════════════════════════════════
# 三、LimitsConfig：阈值自洽性
# ════════════════════════════════════════════════════════════════════════════


def test_planning_limits_candidate_min_must_be_below_max() -> None:
    payload = dict(raw("limits.yaml")["planning"])
    payload["candidate_min"], payload["candidate_max"] = 100, 50
    with pytest.raises(ValidationError, match="candidate_min 必须小于 candidate_max"):
        PlanningLimits.model_validate(payload)


def test_planning_limits_transit_warn_must_be_below_prune() -> None:
    payload = dict(raw("limits.yaml")["planning"])
    payload["single_leg_transit_warn_min"] = 90
    payload["single_leg_transit_prune_min"] = 60
    with pytest.raises(ValidationError, match="warn_min 必须小于 prune_min"):
        PlanningLimits.model_validate(payload)


def test_planning_limits_min_output_cannot_exceed_max() -> None:
    payload = dict(raw("limits.yaml")["planning"])
    payload["min_output_routes"] = 10
    payload["max_output_routes"] = 3
    with pytest.raises(ValidationError, match="不能大于 max_output_routes"):
        PlanningLimits.model_validate(payload)


@pytest.mark.parametrize("missing_key", ["start", "end"])
def test_planning_limits_default_window_needs_start_and_end(missing_key: str) -> None:
    payload = dict(raw("limits.yaml")["planning"])
    window = dict(payload["default_window"])
    window.pop(missing_key)
    payload["default_window"] = window
    with pytest.raises(ValidationError, match="default_window 缺少"):
        PlanningLimits.model_validate(payload)


def test_limits_reject_unknown_travel_mode() -> None:
    payload = deepcopy(raw("limits.yaml"))
    payload["relation_graph"]["include_modes"] = ["walk", "teleport"]
    with pytest.raises(ValidationError, match="未知出行方式"):
        LimitsConfig.model_validate(payload)


@pytest.mark.parametrize("pace", ["relaxed", "balanced", "packed"])
def test_limits_require_all_walking_caps(pace: str) -> None:
    payload = deepcopy(raw("limits.yaml"))
    payload["walking_caps_m"].pop(pace)
    with pytest.raises(ValidationError, match="walking_caps_m 缺少节奏定义"):
        LimitsConfig.model_validate(payload)


def test_limits_require_walk_travel_mode() -> None:
    """步行上限的校验依赖 travel_modes.walk，缺了它硬约束就没法算。"""
    payload = deepcopy(raw("limits.yaml"))
    payload["travel_modes"].pop("walk")
    with pytest.raises(ValidationError, match="必须定义 walk"):
        LimitsConfig.model_validate(payload)


def test_limits_reject_relation_mode_without_travel_mode_definition() -> None:
    payload = deepcopy(raw("limits.yaml"))
    payload["relation_graph"]["include_modes"] = ["walk", "ferry"]
    payload["travel_modes"].pop("ferry", None)
    with pytest.raises(ValidationError, match="引用了未定义的出行方式"):
        LimitsConfig.model_validate(payload)


def test_limits_require_at_least_one_feasibility_check() -> None:
    """硬约束检查不得为空 —— 空列表等于"不做可行性校验"，路线可能根本无法执行。"""
    payload = deepcopy(raw("limits.yaml"))
    payload["feasibility"] = {"enabled_checks": []}
    with pytest.raises(ValidationError, match="enabled_checks 不得为空"):
        LimitsConfig.model_validate(payload)


def test_relation_graph_batch_size_is_bounded() -> None:
    payload = deepcopy(raw("limits.yaml"))
    payload["relation_graph"]["batch_size"] = 1
    with pytest.raises(ValidationError):
        LimitsConfig.model_validate(payload)


def test_travel_mode_speed_must_be_positive() -> None:
    payload = deepcopy(raw("limits.yaml"))
    payload["travel_modes"]["walk"]["speed_kmh"] = 0
    with pytest.raises(ValidationError):
        LimitsConfig.model_validate(payload)


# ════════════════════════════════════════════════════════════════════════════
# 四、PricingConfig：未校准单价的诚实报告
# ════════════════════════════════════════════════════════════════════════════


def test_pricing_reports_fully_calibrated_when_no_flags() -> None:
    payload = deepcopy(raw("pricing.yaml"))
    for section in ("llm", "search", "map", "weather"):
        _clear_calibration_flags(payload[section])
    payload["calibrated_at"] = "2026-09-10"
    assert PricingConfig.model_validate(payload).is_fully_calibrated is True


def test_pricing_detects_uncalibrated_inside_nested_dict() -> None:
    payload = deepcopy(raw("pricing.yaml"))
    payload["llm"]["some_model"] = {"price": None, "needs_calibration": True}
    assert PricingConfig.model_validate(payload).is_fully_calibrated is False


def test_pricing_detects_uncalibrated_inside_list() -> None:
    """★ 列表分支：单价表可能写成数组，标志位藏在元素里也必须被找到。

    漏掉这个分支的后果很严重 —— 成本报表会显示"已校准"，
    而实际单价是 null，最终把成本数字当成真实支出。
    """
    payload = deepcopy(raw("pricing.yaml"))
    payload["llm"]["tiers"] = [
        {"name": "fast", "needs_calibration": False},
        {"name": "strong", "needs_calibration": True},
    ]
    assert PricingConfig.model_validate(payload).is_fully_calibrated is False


def test_pricing_list_without_flags_is_calibrated() -> None:
    payload = deepcopy(raw("pricing.yaml"))
    payload["llm"]["tiers"] = [{"name": "fast", "needs_calibration": False}]
    for section in ("llm", "search", "map", "weather"):
        _clear_calibration_flags(payload[section])
    assert PricingConfig.model_validate(payload).is_fully_calibrated is True


def _clear_calibration_flags(node: Any) -> None:
    """把嵌套结构里的 needs_calibration 全部置为 False。"""
    if isinstance(node, dict):
        if "needs_calibration" in node:
            node["needs_calibration"] = False
        for value in node.values():
            _clear_calibration_flags(value)
    elif isinstance(node, list):
        for item in node:
            _clear_calibration_flags(item)


# ════════════════════════════════════════════════════════════════════════════
# 五、SeedConfig：丰富化规则的自洽性
# ════════════════════════════════════════════════════════════════════════════


def test_tag_signal_requires_at_least_one_condition() -> None:
    """没有触发条件的信号会造成无差别加分 —— 这是最容易写错的配置错误。"""
    with pytest.raises(ValidationError, match="没有任何触发条件"):
        TagSignal.model_validate({"id": "broken", "add": {"popularity": 0.1}})


@pytest.mark.parametrize("dimension", ["shopping", "quiet", "typo_dim"])
def test_tag_signal_rejects_unknown_score_dimension(dimension: str) -> None:
    """★ 回归：人工数据里曾出现过 shopping: / quiet: 这类非法维度。"""
    with pytest.raises(ValidationError, match="引用了未知分值维度"):
        TagSignal.model_validate(
            {"id": "broken", "when_tag_key_present": ["wikidata"], "add": {dimension: 0.1}}
        )


def test_tag_signal_accepts_valid_definition() -> None:
    assert TagSignal.model_validate(
        {"id": "ok", "when_tag_key_present": ["wikidata"], "add": {"popularity": 0.1}}
    )


def test_seed_config_rejects_unknown_category_in_require_signal() -> None:
    payload = deepcopy(raw("seed.yaml"))
    payload["relevance"]["require_signal_categories"] = ["nonexistent"]
    with pytest.raises(ValidationError, match="含未知类别"):
        SeedConfig.model_validate(payload)


def test_seed_config_requires_signal_tag_keys() -> None:
    """signal_tag_keys 为空会让 require_signal 丢弃一切 —— 必须 fail-fast。"""
    payload = deepcopy(raw("seed.yaml"))
    payload["relevance"]["signal_tag_keys"] = []
    with pytest.raises(ValidationError, match="signal_tag_keys 不得为空"):
        SeedConfig.model_validate(payload)


@pytest.mark.parametrize("section", ["duration_bases", "indoor_by_category", "tags_by_category"])
def test_seed_config_requires_every_category_defined(section: str) -> None:
    """score_bases 里的每个类别都必须在这三张表里有定义，否则打分时会 KeyError。"""
    payload = deepcopy(raw("seed.yaml"))
    payload[section].pop("museum")
    with pytest.raises(ValidationError, match=f"{section} 缺少类别定义"):
        SeedConfig.model_validate(payload)


def test_seed_config_rejects_unknown_dimension_in_score_bases() -> None:
    payload = deepcopy(raw("seed.yaml"))
    payload["score_bases"]["museum"]["not_a_dimension"] = 0.5
    with pytest.raises(ValidationError, match="含未知维度"):
        SeedConfig.model_validate(payload)


def test_seed_config_rejects_tags_by_signal_pointing_to_missing_signal() -> None:
    payload = deepcopy(raw("seed.yaml"))
    payload["tags_by_signal"]["no_such_signal"] = ["标签"]
    with pytest.raises(ValidationError, match="引用了不存在的信号 id"):
        SeedConfig.model_validate(payload)


def test_seed_config_rejects_category_derived_from_tag_mapping() -> None:
    """tag_to_category 派生出的类别必须是 places.category 的合法值。"""
    payload = deepcopy(raw("seed.yaml"))
    payload["tag_to_category"]["tourism=museum"] = "not_a_category"
    with pytest.raises(ValidationError, match="派生出了未定义的类别"):
        SeedConfig.model_validate(payload)


def test_seed_config_rejects_invalid_fallback_category() -> None:
    payload = deepcopy(raw("seed.yaml"))
    payload["fallback_category"] = "not_a_category"
    with pytest.raises(ValidationError, match="派生出了未定义的类别"):
        SeedConfig.model_validate(payload)


def test_seed_config_rejects_invalid_prefix_fallback_category() -> None:
    payload = deepcopy(raw("seed.yaml"))
    payload["tag_prefix_fallbacks"]["place"] = "not_a_category"
    with pytest.raises(ValidationError, match="派生出了未定义的类别"):
        SeedConfig.model_validate(payload)


def test_seed_config_rejects_invalid_category_override_target() -> None:
    payload = deepcopy(raw("seed.yaml"))
    payload["category_overrides"][0]["set_category"] = "not_a_category"
    with pytest.raises(ValidationError, match="设置了未定义的类别"):
        SeedConfig.model_validate(payload)


def test_seed_config_non_stop_categories_must_match_code_constant() -> None:
    """★ 配置说"地铁站可以当站点"、代码说不行 —— 这种分歧必须在校验期暴露。"""
    payload = deepcopy(raw("seed.yaml"))
    payload["filters"]["non_stop_categories"] = []
    with pytest.raises(ValidationError, match="不一致"):
        SeedConfig.model_validate(payload)


def test_admin_config_requires_city_and_districts() -> None:
    payload = dict(raw("seed.yaml")["admin"])
    payload["city_names"] = []
    with pytest.raises(ValidationError, match="不得为空"):
        AdminConfig.model_validate(payload)


@pytest.mark.parametrize("prefix", ["中山", "广州"])
def test_admin_config_forbids_dangerous_name_prefixes(prefix: str) -> None:
    """★ 裸「中山」会误伤中山纪念堂/中山大学旧址；「广州」显然是本城。"""
    payload = dict(raw("seed.yaml")["admin"])
    payload["neighbor_name_prefixes"] = [prefix]
    with pytest.raises(ValidationError, match="不允许包含"):
        AdminConfig.model_validate(payload)


def test_coord_config_requires_bbox_keys() -> None:
    payload = dict(raw("seed.yaml")["coord"])
    payload["bbox"] = {"min_lat": 22.5, "max_lat": 23.95}
    with pytest.raises(ValidationError, match=r"coord\.bbox 缺少字段"):
        CoordConfig.model_validate(payload)


def test_coord_config_requires_city_center_lat_lng() -> None:
    payload = dict(raw("seed.yaml")["coord"])
    payload["city_center"] = {"lat": 23.1291}
    with pytest.raises(ValidationError, match="city_center 需要 lat 与 lng"):
        CoordConfig.model_validate(payload)


def test_verification_rules_reject_unknown_status() -> None:
    payload = dict(raw("seed.yaml")["verification"])
    payload["default"] = "probably"
    with pytest.raises(ValidationError, match="不在"):
        VerificationRules.model_validate(payload)


def test_relevance_rejects_zero_duplicate_merge_radius() -> None:
    payload = dict(raw("seed.yaml")["relevance"])
    payload["duplicate_merge_radius_m"] = 0
    with pytest.raises(ValidationError):
        RelevanceConfig.model_validate(payload)


# ════════════════════════════════════════════════════════════════════════════
# 六、配置加载器：文件缺失与格式错误
# ════════════════════════════════════════════════════════════════════════════


def test_missing_config_file_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path)
    with pytest.raises(ConfigError, match="缺少配置文件"):
        config_module._load_yaml("scoring.yaml")


def test_config_file_with_non_dict_top_level_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """顶层是列表的 YAML 必须给出明确错误，而不是在下游抛 AttributeError。"""
    (tmp_path / "scoring.yaml").write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path)
    with pytest.raises(ConfigError, match="顶层必须是映射"):
        config_module._load_yaml("scoring.yaml")


def test_corrupt_yaml_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "scoring.yaml").write_text("a: [oops\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path)
    with pytest.raises(ConfigError, match="解析失败"):
        config_module._load_yaml("scoring.yaml")


def test_invalid_config_content_is_wrapped_in_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """校验失败必须包成 ConfigError（带文件名），而不是漏出裸的 ValidationError。"""
    (tmp_path / "scoring.yaml").write_text("version: '1'\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path)
    with pytest.raises(ConfigError, match="校验失败"):
        config_module._load_model("scoring.yaml", ScoringConfig)


def test_clear_config_cache_empties_all_loaders() -> None:
    """缓存不清会让"改了 YAML 但行为没变"这种问题极难排查。"""
    config_module.get_scoring_config()
    config_module.get_limits_config()
    config_module.get_ttl_config()
    config_module.get_pricing_config()
    config_module.get_seed_config()
    config_module.get_settings()
    config_module.clear_config_cache()
    for loader in (
        config_module.get_scoring_config,
        config_module.get_limits_config,
        config_module.get_ttl_config,
        config_module.get_pricing_config,
        config_module.get_seed_config,
        config_module.get_settings,
    ):
        assert loader.cache_info().currsize == 0


# ════════════════════════════════════════════════════════════════════════════
# 七、Curated Schema：人工数据的非法输入
# ════════════════════════════════════════════════════════════════════════════


def test_curated_place_rejects_invalid_category() -> None:
    with pytest.raises(ValidationError, match=r"类别 .* 不合法"):
        CuratedPlace.model_validate(make_curated_place(category="karaoke"))


def test_curated_place_rejects_unknown_score_dimension() -> None:
    """★ 回归：人工数据里曾出现过 shopping: / quiet: 这类非法维度。"""
    with pytest.raises(ValidationError, match="scores 含非法维度"):
        CuratedPlace.model_validate(make_curated_place(scores={"shopping": 0.8}))


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_curated_place_rejects_score_out_of_range(value: float) -> None:
    with pytest.raises(ValidationError, match="必须在 \\[0, 1\\] 内"):
        CuratedPlace.model_validate(make_curated_place(scores={"food": value}))


def test_curated_place_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        CuratedPlace.model_validate({"name": ""})


def test_curated_place_rejects_duration_out_of_range() -> None:
    with pytest.raises(ValidationError):
        CuratedPlace.model_validate(make_curated_place(recommended_duration_min=5))


def test_curated_place_rejects_unknown_field() -> None:
    """extra='forbid'：拼错的字段名必须报错，不能被静默忽略。"""
    with pytest.raises(ValidationError):
        CuratedPlace.model_validate(make_curated_place(price=10))


def test_curated_place_accepts_valid_entry() -> None:
    place = CuratedPlace.model_validate(
        make_curated_place(aliases=["小蛮腰"], category="attraction", scores={"photo": 0.9})
    )
    assert place.name == "陈家祠"


def test_curated_place_file_rejects_duplicate_names() -> None:
    with pytest.raises(ValidationError, match="重复名称"):
        CuratedPlaceFile.model_validate(
            {
                "version": "1",
                "city": "guangzhou",
                "places": [{"name": "陈家祠"}, {"name": "陈家祠"}],
            }
        )


def test_curated_place_file_accepts_unique_names() -> None:
    file = CuratedPlaceFile.model_validate(
        {"version": "1", "city": "guangzhou", "places": [{"name": "陈家祠"}, {"name": "广州塔"}]}
    )
    assert len(file.places) == 2


def test_route_stop_rejects_invalid_transport() -> None:
    with pytest.raises(ValidationError, match=r"交通方式 .* 不合法"):
        RouteStop.model_validate({"name": "广州塔", "transport_to_next": "rocket"})


def test_route_stop_accepts_valid_transport() -> None:
    for transport in ("walk", "metro", "bus", "taxi", "bike", "ferry"):
        assert RouteStop.model_validate({"name": "广州塔", "transport_to_next": transport})


def test_curated_route_rejects_invalid_route_type() -> None:
    with pytest.raises(ValidationError, match=r"route_type=.* 不合法"):
        CuratedRoute.model_validate(make_curated_route(route_type="karaoke"))


def test_curated_route_rejects_invalid_archetype_hint() -> None:
    with pytest.raises(ValidationError, match=r"archetype_hint=.* 不合法"):
        CuratedRoute.model_validate(make_curated_route(archetype_hint="extreme"))


def test_curated_route_rejects_invalid_pace() -> None:
    with pytest.raises(ValidationError, match=r"pace=.* 不合法"):
        CuratedRoute.model_validate(make_curated_route(pace="sprint"))


def test_curated_route_rejects_invalid_difficulty() -> None:
    with pytest.raises(ValidationError, match=r"difficulty=.* 不合法"):
        CuratedRoute.model_validate(make_curated_route(difficulty="impossible"))


@pytest.mark.parametrize("stop_count", [1, 2])
def test_curated_route_rejects_fewer_than_three_stops(stop_count: int) -> None:
    """★ 回归：实测有 12 条"路线"只有 2 个站点 —— 少于 3 站不算路线。"""
    stops = [{"name": f"站点{i}"} for i in range(stop_count)]
    with pytest.raises(ValidationError, match="少于 3 站不算路线"):
        CuratedRoute.model_validate(make_curated_route(stops=stops))


def test_curated_route_rejects_duplicate_stops() -> None:
    with pytest.raises(ValidationError, match="站点有重复"):
        CuratedRoute.model_validate(
            make_curated_route(stops=[{"name": "广州塔"}, {"name": "广州塔"}, {"name": "陈家祠"}])
        )


@pytest.mark.parametrize("slug", ["ab", "Test-Route", "route_with_underscore", "路线"])
def test_curated_route_rejects_invalid_slug(slug: str) -> None:
    """slug 会出现在 URL 与分享链接里，必须限制在小写字母数字与连字符。"""
    with pytest.raises(ValidationError):
        CuratedRoute.model_validate(make_curated_route(slug=slug))


def test_curated_route_accepts_valid_slug() -> None:
    assert CuratedRoute.model_validate(make_curated_route(slug="guangzhou-classic-1"))


def test_curated_route_file_rejects_duplicate_slugs() -> None:
    with pytest.raises(ValidationError, match="slug 重复"):
        CuratedRouteFile.model_validate(
            {
                "version": "1",
                "city": "guangzhou",
                "routes": [
                    make_curated_route(slug="same-slug"),
                    make_curated_route(slug="same-slug", name="另一条路线"),
                ],
            }
        )


def test_curated_route_file_accepts_unique_slugs() -> None:
    file = CuratedRouteFile.model_validate(
        {
            "version": "1",
            "city": "guangzhou",
            "routes": [
                make_curated_route(slug="route-a"),
                make_curated_route(slug="route-b", name="另一条路线"),
            ],
        }
    )
    assert len(file.routes) == 2
