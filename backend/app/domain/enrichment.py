"""OSM 原始记录 → ``places`` 字段的丰富化（**纯函数，零 IO**）。

★ 诚实性边界（必须遵守，测试会守住）★
    本模块产出的**事实字段**（名称、坐标、类别、营业时间原文、官网、电话）全部来自
    OSM 原始标签，逐条可回溯到 source_url。
    产出的**分值字段**（popularity/photo/...）不是事实，而是
    「类别基线 + 真实标签信号」按 config/seed.yaml 里的规则推导出的**编辑性评分**，
    用于排序；它们会带 ``score_derived`` 标记，绝不冒充客观数据。
    任何拿不到的字段一律留空并记入 ``unknown_fields``，**绝不用估算值填充**。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.config import SCORE_DIMENSIONS, SeedConfig, TagSignal
from app.domain.geo import CityBoundary, point_in_rings
from app.domain.naming import is_meaningless_name, name_variants, normalize_name

__all__ = ["SOURCE_NAME", "DropReason", "EnrichOutcome", "EnrichedPlace", "OsmRecord", "enrich", "enrich_or_reason"]

SOURCE_NAME = "OpenStreetMap"
# OSM 是社区维护的结构化数据：身份与坐标可信，但营业时间/价格可能过期。
# 按 PRD §8.2.1 的分级，给 T2 偏下一点的 0.70，避免与官方来源同权。
SOURCE_CREDIBILITY = 0.70

_CONFIDENCE_BY_STATUS = {"verified": 0.90, "probable": 0.70, "unknown": 0.30}

# field → OSM 标签（用于 unknown_field_rules 的 when_osm_tag_absent 判定）
_FIELD_TO_OSM_TAG = {
    "opening_hours": "opening_hours",
    "website": "website",
    "phone": "phone",
    "description": "description",
}

_PRICE_TAGS = ("fee", "charge")


class DropReason(StrEnum):
    """记录被丢弃的原因。

    为什么要显式记录原因：建库报告必须能回答"抓了 10279 条，为什么只入库 N 条"。
    一个不说明原因的过滤流程 = 一个没人敢信任的数据集。
    """

    NAME_TOO_SHORT = "name_too_short"
    MEANINGLESS_NAME = "meaningless_name"
    BLOCKLISTED_NAME = "blocklisted_name"
    COMMODITY_CHAIN = "commodity_chain"
    COORD_OUT_OF_BBOX = "coord_out_of_bbox"
    DROPPED_TAG = "dropped_primary_tag"
    UNKNOWN_CATEGORY = "unknown_category"
    MISSING_VERIFIABLE_SIGNAL = "missing_verifiable_signal"
    OUTSIDE_CITY = "outside_city"
    OUTSIDE_CITY_BY_NAME = "outside_city_by_name"
    OUTSIDE_CITY_BOUNDARY = "outside_city_boundary"


@dataclass(frozen=True, slots=True)
class EnrichOutcome:
    place: EnrichedPlace | None
    reason: DropReason | None = None

    @property
    def kept(self) -> bool:
        return self.place is not None


@dataclass(frozen=True, slots=True)
class OsmRecord:
    """一条 OSM 原始记录（字段与 fetch 脚本输出一一对应）。"""

    osm_type: str
    osm_id: int
    name: str
    name_en: str | None
    lat: float
    lng: float
    source_url: str
    primary_tag: str
    tags: Mapping[str, str]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> OsmRecord:
        return cls(
            osm_type=str(raw["osm_type"]),
            osm_id=int(raw["osm_id"]),
            name=str(raw["name"]),
            name_en=(str(raw["name_en"]) if raw.get("name_en") else None),
            lat=float(raw["lat"]),
            lng=float(raw["lng"]),
            source_url=str(raw["source_url"]),
            primary_tag=str(raw["primary_tag"]),
            tags=dict(raw.get("tags") or {}),
        )

    @property
    def external_id(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"


@dataclass(frozen=True, slots=True)
class EnrichedPlace:
    canonical_name: str
    display_name: str
    name_en: str | None
    category: str
    district: str | None
    address: str | None
    latitude: float
    longitude: float
    recommended_duration_min: int | None
    opening_hours_raw: str | None
    best_time: tuple[str, ...]
    indoor: bool | None
    tags: tuple[str, ...]
    scores: Mapping[str, float]
    aliases: tuple[str, ...]
    verification_status: str
    confidence: float
    unknown_fields: tuple[str, ...]
    data_quality_flags: tuple[str, ...]
    external_source: str
    external_id: str
    source_name: str
    source_url: str
    source_credibility: float
    source_field_scope: tuple[str, ...]
    matched_signals: tuple[str, ...]
    # 该记录命中的“可核验信号”数量：用于分店上限排序与质量报告
    signal_count: int = 0
    raw_tag_count: int = 0

    def to_scores_payload(self) -> dict[str, float]:
        """转成 ``places`` 表的分值列（缺失维度不写入，保持 NULL 语义）。"""
        return {f"{dim}_score": value for dim, value in self.scores.items()}


# ── 条件判定 ────────────────────────────────────────────────────────────────


def _matches_signal(tags: Mapping[str, str], name: str, category: str, signal: TagSignal) -> bool:
    """信号触发判定：**所有**已声明的条件都必须满足（AND 语义）。"""
    if signal.when_category_in and category not in signal.when_category_in:
        return False
    if signal.when_tag_key_present and not any(key in tags for key in signal.when_tag_key_present):
        return False
    for key, allowed in signal.when_tag_value_in.items():
        value = tags.get(key)
        if value is None:
            return False
        if "*" not in allowed and value not in allowed:
            return False
    return not (
        signal.when_name_matches
        and not any(re.search(pattern, name, re.IGNORECASE) for pattern in signal.when_name_matches)
    )


def _resolve_category(record: OsmRecord, cfg: SeedConfig) -> str | None:
    category = cfg.tag_to_category.get(record.primary_tag)
    if category is None:
        # 前缀兜底：OSM 标签取值发散（historic 有 30+ 种），逐个枚举必漏。
        # 实测教训：historic=city_gate 曾因未枚举到而导致南海神庙无法归类被丢弃。
        key = record.primary_tag.split("=", 1)[0]
        category = cfg.tag_prefix_fallbacks.get(key, cfg.fallback_category)
    if category is None:
        return None
    for override in cfg.category_overrides:
        if category in override.except_categories:
            continue
        if override.when_tag_key_present and not any(k in record.tags for k in override.when_tag_key_present):
            continue
        if override.when_name_matches and not any(
            re.search(p, record.name, re.IGNORECASE) for p in override.when_name_matches
        ):
            continue
        category = override.set_category
        break  # 顺序匹配，先命中先用
    return category


def _apply_scores(category: str, tags: Mapping[str, str], name: str, cfg: SeedConfig) -> tuple[
    dict[str, float], list[str]
]:
    scores: dict[str, float] = {dim: float(cfg.score_bases[category].get(dim, 0.0)) for dim in SCORE_DIMENSIONS}
    matched: list[str] = []
    for signal in cfg.tag_signals:
        if not _matches_signal(tags, name, category, signal):
            continue
        matched.append(signal.id)
        for dim, delta in signal.add.items():
            scores[dim] = scores.get(dim, 0.0) + delta
        for dim, floor in signal.set_min.items():
            scores[dim] = max(scores.get(dim, 0.0), floor)
        for dim, ceiling in signal.set_max.items():
            scores[dim] = min(scores.get(dim, 0.0), ceiling)
    return {dim: round(min(max(value, 0.0), 1.0), 3) for dim, value in scores.items()}, matched


def _apply_duration(category: str, tags: Mapping[str, str], cfg: SeedConfig) -> int:
    duration = cfg.duration_bases[category]
    for signal in cfg.duration_signals:
        if signal.when_tag_value_in and not all(
            tags.get(key) in allowed or "*" in allowed for key, allowed in signal.when_tag_value_in.items()
        ):
            continue
        if signal.when_tag_key_present and not any(k in tags for k in signal.when_tag_key_present):
            continue
        if signal.set_min is not None:
            duration = max(duration, signal.set_min)
        if signal.set_max is not None:
            duration = min(duration, signal.set_max)
    return duration


def _apply_best_time(category: str, tags: Mapping[str, str], cfg: SeedConfig) -> tuple[str, ...]:
    best = list(cfg.best_time_by_category.get(category, []))
    for signal in cfg.best_time_signals:
        if all(
            tags.get(key) in allowed or "*" in allowed for key, allowed in signal.when_tag_value_in.items()
        ):
            best = list(signal.set)
    return tuple(best)


def _collect_tags(
    category: str, tags: Mapping[str, str], matched_signals: Sequence[str], cfg: SeedConfig
) -> tuple[str, ...]:
    collected: list[str] = list(cfg.tags_by_category.get(category, []))
    for signal_id in matched_signals:
        collected += cfg.tags_by_signal.get(signal_id, [])
    for tag_key, mapped in cfg.tags_from_tags.items():
        if tag_key in tags:
            collected += mapped
    # 去重但保持首次出现顺序（可复现的输出，便于快照测试）
    seen: set[str] = set()
    ordered: list[str] = []
    for tag in collected:
        if tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return tuple(ordered)


def _unknown_fields(tags: Mapping[str, str], cfg: SeedConfig) -> tuple[str, ...]:
    unknown: list[str] = []
    for field, rule in cfg.unknown_field_rules.items():
        if rule == "always":
            unknown.append(field)
        elif rule == "when_osm_tag_absent":
            osm_tag = _FIELD_TO_OSM_TAG.get(field, field)
            if osm_tag not in tags:
                unknown.append(field)
        elif rule == "always_unless_fee_tag":
            if not any(tag in tags for tag in _PRICE_TAGS):
                unknown.append(field)
        else:  # pragma: no cover - 配置校验应已拦截未知规则
            raise ValueError(f"未知的 unknown_field_rules 规则：{field}={rule}")
    return tuple(unknown)


def _data_quality_flags(
    record: OsmRecord, category: str, has_district: bool, matched_signals: Sequence[str]
) -> tuple[str, ...]:
    flags = ["score_derived"]
    if "opening_hours" in record.tags:
        # OSM 的营业时间可能长期未更新，UI 必须提示"出发前确认"
        flags.append("hours_from_osm_may_be_stale")
    if not has_district:
        flags.append("district_unknown")
    if "website" not in record.tags:
        flags.append("no_website")
    if not matched_signals:
        flags.append("no_verifiable_signal")
    return tuple(flags)


def _verifiable_signal_count(record: OsmRecord, cfg: SeedConfig) -> int:
    return sum(1 for key in cfg.relevance.signal_tag_keys if key in record.tags)


def _outside_city_by_tag(record: OsmRecord, cfg: SeedConfig) -> bool:
    """用 OSM 的地址标签判断该地点是否明确属于邻市。

    为什么可信：`addr:city` / `addr:district` 是**测绘者写下的归属信息**，
    不是我们的推测。实测广州周边的深圳/东莞/佛山/江门的区名会出现在这两个标签里。
    """
    admin = cfg.admin
    for key in ("addr:city", "addr:district"):
        value = record.tags.get(key)
        if not value:
            continue
        # 先看是否明确属于广州（"广州市" / 广州的某个区）—— 明确属于广州就直接放行
        if any(name in value for name in admin.city_names):
            return False
        if any(district in value for district in admin.districts):
            return False
        if any(city in value for city in admin.neighbor_cities):
            return True
        if any(district in value for district in admin.neighbor_districts):
            return True
    return False


def _outside_city_by_name(record: OsmRecord, cfg: SeedConfig) -> bool:
    """名称以邻市名开头（东莞/佛山/深圳/中山市）。

    刻意**不含**裸「中山」：「中山纪念堂」「中山纪念图书馆」「国立中山大学旧址」
    「孙中山文献馆」都是广州本地地标，用「中山」前缀会把它们成批误删。
    """
    name = record.name.strip()
    return any(name.startswith(prefix) for prefix in cfg.admin.neighbor_name_prefixes)


def _field_scope(record: OsmRecord) -> tuple[str, ...]:
    """该来源**实际支持**哪些字段（字段级溯源）。

    关键：OSM 只证明它能证明的东西。它没有价格信息，就绝不把 price_min 放进
    field_scope —— 否则后续会有人误以为"价格有来源"。
    """
    scope = ["name", "latitude", "longitude", "category"]
    if "opening_hours" in record.tags:
        scope.append("opening_hours")
    if "website" in record.tags:
        scope.append("website")
    if "phone" in record.tags:
        scope.append("phone")
    if any(key in record.tags for key in ("addr:street", "addr:district")):
        scope.append("address")
    if "description" in record.tags:
        scope.append("description")
    return tuple(scope)


# ── 主入口 ──────────────────────────────────────────────────────────────────


def enrich_or_reason(
    record: OsmRecord,
    cfg: SeedConfig,
    *,
    protected_names: frozenset[str] = frozenset(),
    boundary: CityBoundary | None = None,
) -> EnrichOutcome:
    """把一条 OSM 记录丰富化为地点字段，或给出**明确的丢弃原因**。

    过滤顺序（从便宜到贵，与代码逐条对应；改代码时请同步改这里）：

        1. 名称长度            → NAME_TOO_SHORT
        2. 无意义名称（纯数字/符号） → MEANINGLESS_NAME
        3. 坐标越出矩形 bbox    → COORD_OUT_OF_BBOX
        4. 行政边界之外         → OUTSIDE_CITY_BOUNDARY   ← 权威判据
        5. 地址标签指向邻市     → OUTSIDE_CITY            ← 边界不可用时的兜底
        6. 名称前缀是邻市       → OUTSIDE_CITY_BY_NAME    ← 同上
        7. 名称黑名单 ┐
        8. 通用连锁品牌 ├─ 被 protected_names 命中时整体跳过
        9. 噪声 primary_tag ┘
       10. 类别不可判定         → UNKNOWN_CATEGORY
       11. 需要信号但一个都没有 → MISSING_VERIFIABLE_SIGNAL  ← 同样受保护名单豁免

    ★ 行政归属判定刻意排在保护名单**之前** ★
    保护名单保护的是"这是不是一个真实目的地"，而第 4–6 步问的是"它在不在广州"。
    深圳的动物园再有名也不该出现在广州路线里 —— 所以越界地点即使被人工保护也照样丢弃。
    第 11 步则相反：人工确认过的地点允许没有 OSM 信号（广州塔只有 tourism=artwork），
    但仍会打上 ``kept_by_curated_protection`` 标记以便审计。

    每一步的丢弃都会被计数并写进建库报告，保证"为什么没入库"是可回答的。

    ``boundary`` 是城市行政边界多边形。**这是判定"地点在不在本市"的权威判据**：
    矩形 bbox 会切进邻市（实测让深圳野生动物园、锦绣中华民俗村进了广州知识库），
    地址标签与名称前缀过滤能拦下大部分但会漏掉无标签的地点，只有边界是可靠的。

    ``protected_names`` 是人工整理确认过的地点名（归一化后）。
    它们在通过名称与坐标校验后，会**跳过全部相关性过滤** —— 因为机器规则必然误伤，
    而人工已经确认"这是真实目的地"。实测教训：广州塔(tourism=artwork)、
    白云山(natural=peak)、荔湾湖公园(leisure=park 但无任何信号) 都曾被规则误杀。
    """
    name = record.name.strip()

    if len(name) < cfg.filters.min_name_chars:
        return EnrichOutcome(None, DropReason.NAME_TOO_SHORT)
    if is_meaningless_name(name):
        return EnrichOutcome(None, DropReason.MEANINGLESS_NAME)

    bbox = cfg.coord.bbox
    if not (bbox["min_lat"] <= record.lat <= bbox["max_lat"] and bbox["min_lng"] <= record.lng <= bbox["max_lng"]):
        return EnrichOutcome(None, DropReason.COORD_OUT_OF_BBOX)

    # 行政归属判定**先于**保护名单执行：被保护的是"这是不是一个真实目的地"，
    # 而这里是"它在不在广州"。深圳的动物园再有名也不该出现在广州路线里。
    if boundary is not None and not point_in_rings(record.lat, record.lng, boundary):
        return EnrichOutcome(None, DropReason.OUTSIDE_CITY_BOUNDARY)
    # 以下两条是边界不可用时的兜底（也作为防御纵深：边界数据本身可能过期）
    if _outside_city_by_tag(record, cfg):
        return EnrichOutcome(None, DropReason.OUTSIDE_CITY)
    if _outside_city_by_name(record, cfg):
        return EnrichOutcome(None, DropReason.OUTSIDE_CITY_BY_NAME)

    protected = cfg.relevance.protect_curated_names and normalize_name(name) in protected_names

    if not protected:
        for pattern in cfg.filters.name_blocklist_regex:
            if re.search(pattern, name, re.IGNORECASE):
                return EnrichOutcome(None, DropReason.BLOCKLISTED_NAME)

        for pattern in cfg.filters.commodity_chain_regex:
            if re.search(pattern, name, re.IGNORECASE):
                return EnrichOutcome(None, DropReason.COMMODITY_CHAIN)

        if record.primary_tag in cfg.relevance.drop_primary_tags:
            return EnrichOutcome(None, DropReason.DROPPED_TAG)

    category = _resolve_category(record, cfg)
    if category is None:
        return EnrichOutcome(None, DropReason.UNKNOWN_CATEGORY)

    signal_count = _verifiable_signal_count(record, cfg)
    needs_signal = (
        category in cfg.relevance.require_signal_categories
        or record.primary_tag in cfg.relevance.require_signal_primary_tags
    )
    if not protected and needs_signal and signal_count == 0:
        return EnrichOutcome(None, DropReason.MISSING_VERIFIABLE_SIGNAL)

    scores, matched_signals = _apply_scores(category, record.tags, name, cfg)
    # ★ 语义修正 ★：district 只接受 addr:district。
    # 早期实现把 addr:city 也当成 district 的候选，导致"越秀区"和"广州市"混在同一个字段里，
    # 前端按行政区聚合时会出现"广州市"这种不是区的值。
    district = record.tags.get("addr:district") or None
    address = record.tags.get("addr:street") or None

    verification = (
        "verified"
        if any(key in record.tags for key in cfg.verification.verified_when_tag_key_present)
        else cfg.verification.default
    )

    place = EnrichedPlace(
        canonical_name=name,
        display_name=name,
        name_en=record.name_en,
        category=category,
        district=district,
        address=address,
        latitude=record.lat,
        longitude=record.lng,
        recommended_duration_min=_apply_duration(category, record.tags, cfg),
        opening_hours_raw=record.tags.get("opening_hours") or None,
        best_time=_apply_best_time(category, record.tags, cfg),
        indoor=cfg.indoor_by_category.get(category),
        tags=_collect_tags(category, record.tags, matched_signals, cfg),
        scores=scores,
        aliases=tuple(
            sorted(
                alias
                for alias in name_variants(name)
                if normalize_name(alias) != normalize_name(name)
            )
        ),
        verification_status=verification,
        confidence=_CONFIDENCE_BY_STATUS.get(verification, 0.30),
        unknown_fields=_unknown_fields(record.tags, cfg),
        # 被保护但缺少可核验信号：仍然入库（人工确认过），但必须打标记以便审计
        data_quality_flags=(
            *_data_quality_flags(record, category, bool(district), matched_signals),
            *(("kept_by_curated_protection",) if (protected and signal_count == 0) else ()),
        ),
        external_source=SOURCE_NAME,
        external_id=record.external_id,
        source_name=SOURCE_NAME,
        source_url=record.source_url,
        source_credibility=SOURCE_CREDIBILITY,
        source_field_scope=_field_scope(record),
        matched_signals=tuple(matched_signals),
        signal_count=signal_count,
        raw_tag_count=len(record.tags),
    )
    return EnrichOutcome(place)


def enrich(record: OsmRecord, cfg: SeedConfig) -> EnrichedPlace | None:
    """便利包装：只要结果，不要原因（供单元测试与简单调用方使用）。"""
    return enrich_or_reason(record, cfg).place
