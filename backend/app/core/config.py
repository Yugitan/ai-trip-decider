"""应用配置：环境变量（Settings）+ YAML 配置（ScoringConfig 等）。

设计原则（PRD §11.1、§21.1）：
- **权重与阈值不写在代码里，也不写在 prompt 里**，全部来自 ``config/*.yaml``。
- 配置错误必须 **fail-fast**：评分权重之和不为 1.0 直接启动失败，禁止静默兜底。
- 缺少第三方 Key **不是**错误，而是"降级模式"：由 :meth:`Settings.degraded_modes` 如实报告。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import config_dir, env_file
from app.domain.categories import NON_STOP_CATEGORIES, PLACE_CATEGORIES

# ════════════════════════════════════════════════════════════════════════════
# 一、环境变量
# ════════════════════════════════════════════════════════════════════════════

ProviderName = Literal["deepseek", "openai", "anthropic", "disabled"]

# 搜索 Provider 的两份名单，刻意分开写：
#
# - `SEARCH_KEY_FIELDS`：**配置里存在**的 Provider（`.env` 有它的 Key 输入框、dev 面板也列它）；
# - `IMPLEMENTED_SEARCH_PROVIDERS`：**代码里实现了**的 Provider。
#
# 为什么必须分开：`search_provider_effective` 决定"界面与 /health 里说谁在跑"，
# 而 `providers/registry._build_search()` 决定"实际跑谁"。
# 只要允许 effective 返回一个没实现的实现名，就会出现"在 /dev 面板贴一个 Serper Key、
# 生效快照显示 serper、实际却在跑 seed_only"这种只有用户能发现的谎报。
# 因此：**没实现的名字一律不许出现在 effective 里**，且要能说出"你的 Key 被忽略了"。
SEARCH_KEY_FIELDS: Final[dict[str, str]] = {
    "tavily": "tavily_api_key",
    "serper": "serper_api_key",
    "bing": "bing_search_api_key",
}
IMPLEMENTED_SEARCH_PROVIDERS: Final[frozenset[str]] = frozenset({"tavily"})


class Settings(BaseSettings):
    """环境变量配置。空的 API Key 视为"未配置"，对应能力进入降级模式。"""

    model_config = SettingsConfigDict(
        env_file=env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 运行 ──
    env: str = "development"
    backend_port: int = 8000
    frontend_url: str = "http://localhost:3000"
    api_base_url: str = "http://localhost:8000"
    database_url: str = "postgresql+asyncpg://localhost:5432/tripdecider_dev"
    test_database_url: str | None = None
    session_secret: str = "dev-only-change-me-in-production"  # noqa: S105 - 开发默认值，生产由 assert_safe_for_production 拦截

    # ── LLM ──
    llm_provider: str = "deepseek"
    deepseek_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    llm_base_url: str = "https://api.deepseek.com"
    # 与 config/pricing.yaml 的 llm.deepseek 分档保持一致（2026-09-13 校准）：
    # 官方价目页现在只有 deepseek-flash 与 deepseek-v4-pro；
    # 旧的 deepseek-chat / deepseek-reasoner 已不在价目表上。
    llm_tier_fast_model: str = "deepseek-flash"
    llm_tier_strong_model: str = "deepseek-v4-pro"
    llm_timeout_s: float = 20.0
    llm_max_retries: int = 1

    # ── 搜索 ──
    search_provider: str = "auto"
    tavily_api_key: str | None = None
    serper_api_key: str | None = None
    bing_search_api_key: str | None = None
    enable_local_fetch: bool = False
    search_max_queries_per_plan: int = 8

    # ── 地图 ──
    map_provider: str = "auto"
    amap_web_key: str | None = None
    osrm_base_url: str = "https://router.project-osrm.org"
    nominatim_user_agent: str = "TripDecider/1.0 (dev@example.com)"
    map_max_calls_per_plan: int = 40

    # ── 天气 ──
    weather_provider: str = "open_meteo"

    # ── 成本与限流 ──
    plan_cost_circuit_breaker_cny: float = 1.0
    search_cost_circuit_breaker_cny: float = 0.3
    rate_limit_cold_plans_per_day: int = 5
    global_daily_budget_cny: float = 20.0

    # ── 后台 ──
    admin_token: str | None = None

    # ── 测试故障注入（生产必须为空）──
    fault_injection: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data: Any) -> Any:
        """把空字符串归一化为 None：`.env` 里 `TAVILY_API_KEY=` 表示"未配置"。"""
        if isinstance(data, dict):
            return {k: (None if isinstance(v, str) and v.strip() == "" else v) for k, v in data.items()}
        return data

    # ── 派生：实际生效的 Provider（考虑 Key 是否存在）──

    @property
    def llm_provider_effective(self) -> ProviderName:
        if self.llm_provider in ("disabled", "null", "none"):
            return "disabled"
        has_key = {
            "deepseek": self.deepseek_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(self.llm_provider)
        return self.llm_provider if has_key else "disabled"  # type: ignore[return-value]

    @property
    def search_provider_effective(self) -> str:
        """实际生效的搜索 Provider。

        只有 :data:`IMPLEMENTED_SEARCH_PROVIDERS` 里的名字才可能出现在返回值里 ——
        显式配了 `serper` / `bing`（或 `auto` 下只有它们的 Key）时返回 `seed_only`，
        而不是返回一个代码里查不到实现的名字。被忽略的 Key 由
        :meth:`ignored_search_keys` 报出来，不静默吞掉。
        """
        if self.search_provider in ("seed_only", "disabled"):
            return "seed_only"
        available = {name: bool(getattr(self, field)) for name, field in SEARCH_KEY_FIELDS.items()}
        if self.search_provider == "auto":
            # 按固定顺序取第一个「有 Key 且已实现」的 Provider
            for name, ok in available.items():
                if name in IMPLEMENTED_SEARCH_PROVIDERS and ok:
                    return name
            return "seed_only"
        if self.search_provider not in IMPLEMENTED_SEARCH_PROVIDERS:
            # 配置里预留、代码里没实现 → 降级到 seed_only（而不是谎报生效）
            return "seed_only"
        # 显式指定但缺 Key → 降级而不是报错
        return self.search_provider if available.get(self.search_provider) else "seed_only"

    def ignored_search_keys(self) -> list[str]:
        """配了 Key、但本项目**尚未实现**的搜索 Provider。

        这些 Key 不会生效（`:meth:`search_provider_effective`` 会跳过它们）。
        返回它们是为了能让 /health 与前端如实说出"你的 Key 被忽略了"，
        而不是让用户对着一个填了 Key 的输入框猜为什么搜索还是没联网。
        """
        return [
            name
            for name, field in SEARCH_KEY_FIELDS.items()
            if name not in IMPLEMENTED_SEARCH_PROVIDERS and getattr(self, field)
        ]

    @property
    def map_provider_effective(self) -> str:
        if self.map_provider in ("haversine", "disabled"):
            # 显式要求离线估算（或彻底禁用地图）→ 一律走 haversine，绝不偷偷发网络请求
            return "haversine"
        if self.map_provider in ("amap", "auto") and self.amap_web_key:
            return "amap"
        if self.map_provider == "amap":
            # 显式要 amap 但没 Key → 降级到 osrm（真实路网）而不是崩溃
            return "osrm"
        return "osrm"

    @property
    def is_production(self) -> bool:
        return self.env.lower() in ("production", "prod")

    def degraded_modes(self) -> list[str]:
        """当前处于降级模式的能力清单，供 /health 与 UI 如实展示。"""
        modes: list[str] = []
        if self.llm_provider_effective == "disabled":
            modes.append("llm:disabled(规则引擎+模板兜底)")
        if self.search_provider_effective == "seed_only":
            modes.append("search:seed_only(不联网，仅本地知识库)")
        ignored_search = self.ignored_search_keys()
        if ignored_search:
            # 单独一条：".env 里填了 Key 却没生效" 与 "根本没配 Key" 是两件事，
            # 后者去配置里看一眼就明白，前者不说就只能靠猜。
            modes.append(f"search:{'+'.join(ignored_search)}(已预留、尚未实现：Key 不生效)")
        if self.map_provider_effective != "amap":
            modes.append(f"map:{self.map_provider_effective}(距离/耗时为路网或估算值)")
        if self.weather_provider in ("disabled", "null") or not self.weather_provider:
            modes.append("weather:disabled")
        if self.fault_injection:
            modes.append(f"fault_injection:{self.fault_injection}")
        return modes

    # ── 安全校验 ──

    def assert_safe_for_production(self) -> None:
        """生产环境启动前自检；开发环境只告警不阻断。"""
        problems: list[str] = []
        if self.session_secret.startswith("dev-only"):
            problems.append("SESSION_SECRET 仍是开发默认值")
        # 注意：必须判 `not`，不能判 `== ""`。
        # `_blank_to_none` 已经把空字符串归一化成 None，所以 `admin_token == ""` 永远为假 ——
        # 那是一条**永远不会触发**的检查（后台会静默地无保护）。
        if not self.admin_token:
            problems.append("ADMIN_TOKEN 未设置或为空（后台无保护）")
        if self.fault_injection:
            problems.append(f"FAULT_INJECTION={self.fault_injection} 必须在生产关闭")
        if problems:
            msg = "生产环境配置不安全：" + "；".join(problems)
            if self.is_production:
                raise RuntimeError(msg)


# ════════════════════════════════════════════════════════════════════════════
# 二、评分配置（config/scoring.yaml）
# ════════════════════════════════════════════════════════════════════════════

WEIGHT_KEYS = (
    "preference",
    "efficiency",
    "time_fit",
    "popularity",
    "budget_fit",
    "walking_fit",
    "place_relation",
)


class Weights(BaseModel):
    """一组评分权重。必须恰好覆盖 WEIGHT_KEYS，且之和为 1.0。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preference: float
    efficiency: float
    time_fit: float
    popularity: float
    budget_fit: float
    walking_fit: float
    place_relation: float

    @model_validator(mode="after")
    def _check(self) -> Weights:
        values = self.model_dump()
        for name, value in values.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"权重 {name}={value} 必须在 [0, 1] 区间内")
        total = sum(values.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"评分权重之和必须为 1.0，实际为 {total:.6f}（偏差 {abs(total - 1.0):.6f}）。"
                "请检查 config/scoring.yaml。"
            )
        return self


class ArchetypeConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_popularity_anchors: int | None = None
    min_popularity: float | None = None
    theme_min_ratio: float | None = None


class ArchetypeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    description: str
    weights: Weights
    max_stops: int = Field(gt=1, le=12)
    walking_cap_ratio: float = Field(gt=0, le=1.5)
    constraints: ArchetypeConstraints = ArchetypeConstraints()


class PreferenceDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    icon: str = ""
    field: str | None = None
    categories: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_target(self) -> PreferenceDimension:
        if self.field is None and not self.categories:
            raise ValueError(f"偏好维度 {self.label} 必须指定 field 或 categories 之一")
        return self


class PreferenceFormula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mean_weight: float
    coverage_weight: float
    coverage_min_dim_score: float
    coverage_min_stops: int = Field(ge=1)
    unknown_score_default: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum(self) -> PreferenceFormula:
        total = self.mean_weight + self.coverage_weight
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"preference.mean_weight + coverage_weight 必须为 1.0，实际 {total}")
        return self


class EfficiencyFormula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    good_travel_ratio: float
    bad_travel_ratio: float
    reversal_penalty: float
    reversal_penalty_cap: float

    @model_validator(mode="after")
    def _order(self) -> EfficiencyFormula:
        if self.good_travel_ratio >= self.bad_travel_ratio:
            raise ValueError("efficiency.good_travel_ratio 必须小于 bad_travel_ratio")
        return self


class TimeFitFormula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rush_ratio: float
    rush_penalty_per_stop: float
    rush_penalty_cap: float
    idle_threshold_min: int
    idle_penalty_per_step: float
    idle_penalty_cap: float


class PopularityFormula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mean_weight: float
    max_weight: float

    @model_validator(mode="after")
    def _sum(self) -> PopularityFormula:
        total = self.mean_weight + self.max_weight
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"popularity 的 mean_weight + max_weight 必须为 1.0，实际 {total}")
        return self


class BudgetFitFormula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    under_bonus_ratio: float
    under_floor: float
    under_bonus: float
    over_zero_point: float

    @model_validator(mode="after")
    def _range(self) -> BudgetFitFormula:
        if not 0 < self.under_bonus_ratio <= 1:
            raise ValueError("budget_fit.under_bonus_ratio 必须在 (0, 1] 内")
        if not 0 < self.over_zero_point <= 1:
            raise ValueError("budget_fit.over_zero_point 必须在 (0, 1] 内")
        return self


class WalkingFitFormula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hard_limit_ratio: float = Field(gt=0)


class DiversityMultiplier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    max_bonus: float


class WeatherMultiplier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rain_bonus: float
    heat_bonus: float


class ConflictMultiplier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    penalty_factor: float


class Multipliers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    diversity: DiversityMultiplier
    weather: WeatherMultiplier
    conflict: ConflictMultiplier


class Formulas(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preference: PreferenceFormula
    efficiency: EfficiencyFormula
    time_fit: TimeFitFormula
    popularity: PopularityFormula
    budget_fit: BudgetFitFormula
    walking_fit: WalkingFitFormula
    multipliers: Multipliers


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    default_weights: Weights
    archetypes: dict[str, ArchetypeConfig]
    formulas: Formulas
    preference_dimensions: dict[str, PreferenceDimension]

    @model_validator(mode="after")
    def _check_archetypes(self) -> ScoringConfig:
        required = {"relaxed", "classic", "themed"}
        missing = required - set(self.archetypes)
        if missing:
            raise ValueError(f"scoring.yaml 缺少必需的 archetype：{sorted(missing)}")
        for name, cfg in self.archetypes.items():
            if cfg.walking_cap_ratio > self.formulas.walking_fit.hard_limit_ratio:
                raise ValueError(
                    f"archetype {name} 的 walking_cap_ratio={cfg.walking_cap_ratio} 超过了硬约束上限 "
                    f"{self.formulas.walking_fit.hard_limit_ratio}"
                )
        return self

    def weights_for(self, archetype: str) -> Weights:
        cfg = self.archetypes.get(archetype)
        return cfg.weights if cfg else self.default_weights


# ════════════════════════════════════════════════════════════════════════════
# 三、阈值配置（config/limits.yaml）、TTL、定价
# ════════════════════════════════════════════════════════════════════════════


class PlanningLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_min: int
    candidate_max: int
    beam_width: int
    max_depth: int
    min_route_stops: int = Field(ge=2)
    walk_preferred_max_m: int = Field(gt=0)
    paths_per_archetype: int
    min_output_routes: int
    max_output_routes: int
    max_route_similarity: float
    anchor_pool: int
    neighborhood_radius_m: int
    default_window: dict[str, str]
    single_leg_transit_warn_min: int
    single_leg_transit_prune_min: int
    last_entry_buffer_min: int
    min_stop_duration_min: int
    #: 节奏 → 停留时长系数。缺省 1.0 只是为了向后兼容旧配置；
    #: 生产配置必须显式给出三档，否则 pace 会退化成"只影响步行上限"。
    stay_scale_by_pace: dict[str, float] = Field(default_factory=dict)
    #: 游玩时长偏好 → 每天排多少分钟。键必须覆盖半天与一天（``whole_window`` 例外，
    #: 它表示"用满用户的时间窗"，没有对应数值）。
    day_span_minutes: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistency(self) -> PlanningLimits:
        for pace, scale in self.stay_scale_by_pace.items():
            if scale <= 0:
                raise ValueError(f"planning.stay_scale_by_pace.{pace} 必须为正数（停留时长系数）")
        missing_spans = {"half_day", "full_day"} - set(self.day_span_minutes)
        if missing_spans:
            # fail-fast：缺了 key 会静默退回"用满整个窗口"，而那是另一种游玩时长，
            # 用户选了「半天」却排出一整天，比直接报配置错更难查。
            raise ValueError(f"planning.day_span_minutes 缺少时长定义：{sorted(missing_spans)}")
        for span, minutes in self.day_span_minutes.items():
            if minutes < self.min_stop_duration_min:
                raise ValueError(
                    f"planning.day_span_minutes.{span}（{minutes}）不能短于 "
                    f"min_stop_duration_min（{self.min_stop_duration_min}）—— 那连一站都放不下"
                )
        if self.day_span_minutes["half_day"] >= self.day_span_minutes["full_day"]:
            raise ValueError("planning.day_span_minutes 里 half_day 必须短于 full_day")
        if self.candidate_min >= self.candidate_max:
            raise ValueError("planning.candidate_min 必须小于 candidate_max")
        if self.single_leg_transit_warn_min >= self.single_leg_transit_prune_min:
            raise ValueError("single_leg_transit_warn_min 必须小于 prune_min")
        if self.min_output_routes > self.max_output_routes:
            raise ValueError("min_output_routes 不能大于 max_output_routes")
        for key in ("start", "end"):
            if key not in self.default_window:
                raise ValueError(f"planning.default_window 缺少 {key}")
        return self


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_scope: str
    over_limit_ratio: float = Field(gt=1.0)
    transit_fare_cny: dict[str, float]
    meal_cost_cny: dict[str, float]
    meal_windows: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _meal_windows(self) -> BudgetLimits:
        """餐次时间窗的键必须能在 meal_cost_cny 里找到单价，否则会静默按 0 元计。"""
        for name, window in self.meal_windows.items():
            if name not in self.meal_cost_cny:
                raise ValueError(
                    f"budget.meal_windows 的餐次 {name!r} 在 meal_cost_cny 里没有对应单价"
                )
            if {"start", "end"} - set(window):
                raise ValueError(f"budget.meal_windows.{name} 必须同时给出 start 与 end")
            if window["start"] >= window["end"]:
                raise ValueError(f"budget.meal_windows.{name} 的 start 必须早于 end")
        return self


class RateLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests_per_ip_per_minute: int
    cold_plans_per_ip_per_day: int
    cold_plans_per_session_per_day: int
    revisions_per_session_per_hour: int
    feedback_per_ip_per_hour: int
    body_max_bytes: int
    input_free_text_max_chars: int
    idempotency_window_s: int


class CircuitBreakerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_total_cny: float
    plan_search_cny: float
    plan_map_calls: int
    plan_llm_calls: int


class CostLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    circuit_breaker: CircuitBreakerLimits
    warn_plan_cny: float
    global_daily_cny: float


class ProviderLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    llm_timeout_s: float
    llm_max_retries: int
    llm_max_output_tokens: int
    search_timeout_s: float
    search_max_queries_per_plan: int
    search_max_results_per_query: int
    map_timeout_s: float
    map_max_calls_per_plan: int
    fetch_timeout_s: float
    fetch_max_bytes: int
    fetch_domain_allowlist: list[str]


class DataQualityThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_active_places: int
    min_routes: int
    min_sourced_ratio: float
    min_verified_ratio: float
    aspirational_verified_ratio: float = 0.30
    min_coord_in_bbox_ratio: float
    max_unresolved_duplicate: int
    max_unknown_hours_ratio: float


class TravelMode(BaseModel):
    """一种出行方式的耗时假设（不是实测数据，用于无真实耗时的估算）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    speed_kmh: float = Field(gt=0)
    overhead_min: float = Field(ge=0)


class RelationGraphLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_pair_distance_m: int = Field(gt=0)
    include_modes: list[str]
    batch_size: int = Field(ge=2, le=100)

    @model_validator(mode="after")
    def _modes(self) -> RelationGraphLimits:
        allowed = {"walk", "bike", "metro", "bus", "taxi", "ferry"}
        unknown = set(self.include_modes) - allowed
        if unknown:
            raise ValueError(f"relation_graph.include_modes 含未知出行方式：{sorted(unknown)}")
        return self


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    planning: PlanningLimits
    travel_modes: dict[str, TravelMode]
    relation_graph: RelationGraphLimits
    walking_caps_m: dict[str, int]
    budget: BudgetLimits
    rate_limit: RateLimits
    cost: CostLimits
    providers: ProviderLimits
    data_quality: DataQualityThresholds
    feasibility: dict[str, list[str]]

    @model_validator(mode="after")
    def _walking_caps(self) -> LimitsConfig:
        missing = {"relaxed", "balanced", "packed"} - set(self.walking_caps_m)
        if missing:
            raise ValueError(f"walking_caps_m 缺少节奏定义：{sorted(missing)}")
        if "walk" not in self.travel_modes:
            raise ValueError("travel_modes 必须定义 walk（步行上限的校验依赖它）")
        for mode in self.relation_graph.include_modes:
            if mode not in self.travel_modes:
                raise ValueError(f"relation_graph 引用了未定义的出行方式：{mode}")
        if not self.feasibility.get("enabled_checks"):
            raise ValueError("feasibility.enabled_checks 不得为空（硬约束必须启用）")
        return self


class TtlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    ttl_hours: dict[str, int | None]
    refresh_policy: dict[str, Any]

    def ttl_for(self, kind: str) -> int | None:
        """取某类数据的 TTL（小时）。未定义的键视为永不过期（None）。"""
        return self.ttl_hours.get(kind)


class FxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    usd_cny: float
    fx_updated_at: str
    fx_note: str = ""


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    version: str
    calibrated_at: str | None
    currency: str
    sources: dict[str, str]
    fx: FxConfig
    llm: dict[str, Any]
    search: dict[str, Any]
    map: dict[str, Any]
    weather: dict[str, Any]
    budgets: dict[str, float]

    @property
    def is_fully_calibrated(self) -> bool:
        """是否存在未校准（null）的单价。成本报表必须如实反映该状态。"""
        return not self._has_uncalibrated(self.model_dump())

    @classmethod
    def _has_uncalibrated(cls, node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("needs_calibration") is True:
                return True
            return any(cls._has_uncalibrated(v) for v in node.values())
        if isinstance(node, list):
            return any(cls._has_uncalibrated(v) for v in node)
        return False


# ── 知识库丰富化规则（config/seed.yaml）───────────────────────────────────
#
# 这些模型的意义：把"OSM 原始标签 → 地点字段"的推导规则变成**强类型、可校验、
# 可单测**的配置，而不是散落在脚本里的 if-else。改规则 = 改 YAML，不改代码。

SCORE_DIMENSIONS = (
    "popularity",
    "photo",
    "food",
    "culture",
    "night_view",
    "family",
    "couple",
    "walkability",
    "rainy_day",
)


class TagSignal(BaseModel):
    """一条"真实 OSM 标签 → 分值调整"的信号。每条都必须给出可核验的理由。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    reason: str = ""
    when_tag_key_present: list[str] = Field(default_factory=list)
    when_tag_value_in: dict[str, list[str]] = Field(default_factory=dict)
    when_category_in: list[str] = Field(default_factory=list)
    when_name_matches: list[str] = Field(default_factory=list)
    add: dict[str, float] = Field(default_factory=dict)
    set_min: dict[str, float] = Field(default_factory=dict)
    set_max: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_condition(self) -> TagSignal:
        if not (
            self.when_tag_key_present
            or self.when_tag_value_in
            or self.when_category_in
            or self.when_name_matches
        ):
            raise ValueError(f"标签信号 {self.id} 没有任何触发条件，会造成无差别加分")
        unknown = (set(self.add) | set(self.set_min) | set(self.set_max)) - set(SCORE_DIMENSIONS)
        if unknown:
            raise ValueError(f"标签信号 {self.id} 引用了未知分值维度：{sorted(unknown)}")
        return self


class CategoryOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    when_tag_key_present: list[str] = Field(default_factory=list)
    when_name_matches: list[str] = Field(default_factory=list)
    except_categories: list[str] = Field(default_factory=list)
    set_category: str


class DurationSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    when_tag_value_in: dict[str, list[str]] = Field(default_factory=dict)
    when_tag_key_present: list[str] = Field(default_factory=list)
    set_min: int | None = None
    set_max: int | None = None


class BestTimeSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    when_tag_value_in: dict[str, list[str]]
    set: list[str]


class SeedFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_name_chars: int = Field(ge=1)
    name_blocklist_regex: list[str]
    commodity_chain_regex: list[str]
    non_stop_categories: list[str]


class RelevanceConfig(BaseModel):
    """相关性过滤：决定哪些 OSM 记录值得进知识库。

    背景：整城全量抓取会得到大量"生活类 POI"（瑞幸分店、河涌、社区中心、口袋公园）。
    不做过滤，知识库会被噪声淹没；做过度过滤，又会丢掉真实的旅行目的地。
    折中办法是把判断依据全部写成显式规则（每条都附实测数量），而不是靠直觉删数据。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    drop_primary_tags: list[str]
    require_signal_categories: list[str]
    # 需要"至少一个可核验信号"才保留的具体 OSM 标签。
    # 用途：社区分馆（141 个 amenity=library）绝大多数没有信号，属于噪声；
    # 但地标级图书馆（广州图书馆）有 wikidata，必须留下 —— 用类别判据区分不了这两种。
    require_signal_primary_tags: list[str] = Field(default_factory=list)
    signal_tag_keys: list[str]

    # 品牌分店上限（见 config/seed.yaml 的说明）
    max_branches_per_brand: int = Field(ge=1)
    # 人工整理地点是否跳过所有相关性过滤（见 config/seed.yaml 的说明）
    protect_curated_names: bool = True
    # 同名且在此半径内视为同一实体被重复测绘，只保留信息量最多的一条
    duplicate_merge_radius_m: int = Field(gt=0)

    @model_validator(mode="after")
    def _categories_valid(self) -> RelevanceConfig:
        unknown = set(self.require_signal_categories) - set(PLACE_CATEGORIES)
        if unknown:
            raise ValueError(f"relevance.require_signal_categories 含未知类别：{sorted(unknown)}")
        if not self.signal_tag_keys:
            raise ValueError("relevance.signal_tag_keys 不得为空，否则 require_signal 会丢弃一切")
        return self


class VerificationRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verified_when_tag_key_present: list[str]
    default: str
    unknown_value: str

    @model_validator(mode="after")
    def _statuses_valid(self) -> VerificationRules:
        allowed = {"verified", "probable", "unknown", "stale", "conflicting"}
        for status in (self.default, self.unknown_value):
            if status not in allowed:
                raise ValueError(f"verification 状态 {status} 不在 {sorted(allowed)} 中")
        return self


class AdminConfig(BaseModel):
    """行政归属判定配置（见 config/seed.yaml 的说明）。

    存在意义：整城抓取用矩形 bbox，而城市形状不规则，矩形会切进邻市。
    这里用 OSM 自带的地址标签与不会误伤的名称前缀做保守过滤 ——
    宁可漏掉少量越界地点，也不要把「中山纪念堂」这类广州本地地标误删。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    city_names: list[str]
    districts: list[str]
    neighbor_districts: list[str]
    neighbor_cities: list[str]
    neighbor_name_prefixes: list[str]

    @model_validator(mode="after")
    def _non_empty(self) -> AdminConfig:
        if not self.city_names or not self.districts:
            raise ValueError("admin.city_names 与 admin.districts 不得为空（否则无法判定归属）")
        for prefix in self.neighbor_name_prefixes:
            if prefix in ("中山", "广州"):
                # 裸「中山」会误伤中山纪念堂/中山大学旧址；「广州」显然是本城
                raise ValueError(f"neighbor_name_prefixes 不允许包含 {prefix}（会误伤本地地标）")
        return self


class CoordConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bbox: dict[str, float]
    city_center: dict[str, float]

    @model_validator(mode="after")
    def _bbox_keys(self) -> CoordConfig:
        required = {"min_lat", "max_lat", "min_lng", "max_lng"}
        if not required <= set(self.bbox):
            raise ValueError(f"coord.bbox 缺少字段：{sorted(required - set(self.bbox))}")
        if not {"lat", "lng"} <= set(self.city_center):
            raise ValueError("coord.city_center 需要 lat 与 lng")
        return self


class SeedConfig(BaseModel):
    """OSM → places 的丰富化规则（config/seed.yaml）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    derivation: str

    tag_to_category: dict[str, str]
    fallback_category: str | None
    # OSM 键 → 类别的前缀兜底（显式映射优先）
    tag_prefix_fallbacks: dict[str, str]
    category_overrides: list[CategoryOverride]

    score_bases: dict[str, dict[str, float]]
    tag_signals: list[TagSignal]

    duration_bases: dict[str, int]
    duration_signals: list[DurationSignal]

    indoor_by_category: dict[str, bool | None]
    best_time_by_category: dict[str, list[str]]
    best_time_signals: list[BestTimeSignal]

    tags_by_category: dict[str, list[str]]
    tags_by_signal: dict[str, list[str]]
    tags_from_tags: dict[str, list[str]]

    filters: SeedFilters
    relevance: RelevanceConfig
    admin: AdminConfig
    verification: VerificationRules
    unknown_field_rules: dict[str, str]
    coord: CoordConfig

    @model_validator(mode="after")
    def _consistency(self) -> SeedConfig:
        categories = set(self.score_bases)
        for label, keys in (
            ("duration_bases", set(self.duration_bases)),
            ("indoor_by_category", set(self.indoor_by_category)),
            ("tags_by_category", set(self.tags_by_category)),
        ):
            missing = categories - keys
            if missing:
                raise ValueError(f"seed.yaml 的 {label} 缺少类别定义：{sorted(missing)}")

        for category, base in self.score_bases.items():
            unknown = set(base) - set(SCORE_DIMENSIONS)
            if unknown:
                raise ValueError(f"score_bases[{category}] 含未知维度：{sorted(unknown)}")

        signal_ids = {s.id for s in self.tag_signals}
        for signal_id in self.tags_by_signal:
            if signal_id not in signal_ids:
                raise ValueError(f"tags_by_signal 引用了不存在的信号 id：{signal_id}")

        # 派生出的类别必须是 places.category 合法值（单一事实源：app.domain.categories）
        derived = set(self.tag_to_category.values())
        if self.fallback_category is not None:
            derived.add(self.fallback_category)
        derived |= set(self.tag_prefix_fallbacks.values())
        invalid = derived - set(PLACE_CATEGORIES)
        if invalid:
            raise ValueError(
                f"tag_to_category 派生出了未定义的类别：{sorted(invalid)}；"
                f"合法类别见 app/domain/categories.py"
            )
        for override in self.category_overrides:
            if override.set_category not in PLACE_CATEGORIES:
                raise ValueError(
                    f"category_overrides[{override.name}] 设置了未定义的类别 {override.set_category}"
                )

        # "不作为游玩站点的类别"必须与代码里的常量一致，否则会出现
        # "配置说地铁站可以当站点、代码说不行"这种只在运行时才暴露的分歧。
        if set(self.filters.non_stop_categories) != set(NON_STOP_CATEGORIES):
            raise ValueError(
                f"filters.non_stop_categories={sorted(self.filters.non_stop_categories)} 与 "
                f"app/domain/categories.py 的 NON_STOP_CATEGORIES={sorted(NON_STOP_CATEGORIES)} 不一致"
            )
        return self


# ════════════════════════════════════════════════════════════════════════════
# 四、加载器
# ════════════════════════════════════════════════════════════════════════════


class ConfigError(RuntimeError):
    """配置文件缺失或非法。"""


def _load_yaml(name: str) -> dict[str, Any]:
    path = config_dir() / name
    if not path.exists():
        raise ConfigError(f"缺少配置文件：{path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - 仅在文件损坏时触发
        raise ConfigError(f"配置文件 {path} 解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件 {path} 的顶层必须是映射（dict）")
    return raw


def _load_model[T: BaseModel](name: str, model: type[T]) -> T:
    try:
        return model.model_validate(_load_yaml(name))
    except Exception as exc:
        raise ConfigError(f"配置 {name} 校验失败：{exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_scoring_config() -> ScoringConfig:
    """加载并校验评分配置。权重之和不等于 1.0 时抛 ConfigError（fail-fast）。"""
    return _load_model("scoring.yaml", ScoringConfig)


@lru_cache(maxsize=1)
def get_limits_config() -> LimitsConfig:
    return _load_model("limits.yaml", LimitsConfig)


@lru_cache(maxsize=1)
def get_ttl_config() -> TtlConfig:
    return _load_model("ttl.yaml", TtlConfig)


@lru_cache(maxsize=1)
def get_pricing_config() -> PricingConfig:
    return _load_model("pricing.yaml", PricingConfig)


@lru_cache(maxsize=1)
def get_seed_config() -> SeedConfig:
    return _load_model("seed.yaml", SeedConfig)


def clear_config_cache() -> None:
    """测试用：清空配置缓存。"""
    for fn in (
        get_settings,
        get_scoring_config,
        get_limits_config,
        get_ttl_config,
        get_pricing_config,
        get_seed_config,
    ):
        fn.cache_clear()
