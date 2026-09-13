"""OSM 记录丰富化管线（``app/domain/enrichment.py``）的单元测试。

为什么这个文件必须存在：
    这是知识库的**唯一入口** —— 10 279 条 OSM 原始记录里，哪些能进知识库、
    哪些被丢弃、丢弃原因是什么，全部由这个模块决定。它此前只有集成测试间接覆盖
    （跑完 12 秒建库后看地点总数），意味着"某条规则把地标误杀了"只能靠人眼发现。

    本项目真实踩过的坑，全部在这里变成回归测试：
      - 广州塔（tourism=artwork）被相关性过滤误杀
      - 白云山（natural=peak）被 drop_primary_tags 误杀
      - 深圳野生动物园混进广州知识库（行政归属判定缺失）
      - 中山纪念堂被"中山"前缀规则误删
      - district 字段混进了 addr:city 的值（出现"广州市"这种不是区的值）

这组测试**不需要数据库、不需要网络**，全部在内存里构造记录，毫秒级反馈。
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from app.core.config import SeedConfig, get_seed_config
from app.core.paths import config_dir
from app.domain.enrichment import (
    SOURCE_CREDIBILITY,
    SOURCE_NAME,
    DropReason,
    EnrichedPlace,
    EnrichOutcome,
    OsmRecord,
    enrich,
    enrich_or_reason,
)
from app.domain.geo import CityBoundary

pytestmark = pytest.mark.unit


# ════════════════════════════════════════════════════════════════════════════
# 夹具与构造工具
# ════════════════════════════════════════════════════════════════════════════

# 覆盖广州主城区的一个正方形边界，用于验证"边界优先于一切"。
# 边界外的点（如深圳，纬度 22.5 左右）会被判为城市外。
CITY_BOUNDARY = CityBoundary(
    outer=(
        (
            (22.90, 113.10),
            (22.90, 113.50),
            (23.30, 113.50),
            (23.30, 113.10),
        ),
    )
)

INSIDE_CITY = (23.1291, 113.2644)  # 广州市中心（越秀/天河一带）

# ★ 关键构造：落在**矩形 bbox 内**、但在（本测试的）行政边界之外。
#   这正是真实 bug 的形态 —— 抓取用的矩形 bbox（22.50–23.95 / 112.90–114.05）
#   面积约 18 800 km²，而广州实际只有约 7 434 km²，四角全切进邻市。
#   深圳野生动物园、锦绣中华民俗村就是这么混进广州知识库的。
OUTSIDE_BOUNDARY = (22.55, 113.95)


@pytest.fixture(scope="module")
def cfg() -> SeedConfig:
    return get_seed_config()


def make_record(
    name: str = "陈家祠",
    *,
    primary_tag: str = "tourism=attraction",
    lat: float = INSIDE_CITY[0],
    lng: float = INSIDE_CITY[1],
    tags: dict[str, str] | None = None,
    osm_type: str = "node",
    osm_id: int = 1,
    name_en: str | None = None,
) -> OsmRecord:
    """构造一条 OSM 记录。

    ``tags`` 默认为空字典，调用方需要显式给出信号标签。
    注意真实抓取流程里 ``tags`` 是**包含** primary_tag 的键值对的
    （见 ``fetch_osm_guangzhou.py`` 的 ``clean_tags``），
    因此断言信号命中时要自己把 ``tourism=viewpoint`` 之类的键值写进 ``tags``。
    """
    return OsmRecord(
        osm_type=osm_type,
        osm_id=osm_id,
        name=name,
        name_en=name_en,
        lat=lat,
        lng=lng,
        source_url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
        primary_tag=primary_tag,
        tags=dict(tags or {}),
    )


def raw_seed() -> dict[str, Any]:
    """读取 config/seed.yaml 的原始字典，用于构造"合成配置"。"""
    data = yaml.safe_load((config_dir() / "seed.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict), "seed.yaml 的顶层不是映射，测试前提失效"
    return data


# ════════════════════════════════════════════════════════════════════════════
# 一、OsmRecord：原始数据的解析与外部 ID
# ════════════════════════════════════════════════════════════════════════════


def test_osm_record_from_raw_parses_all_fields() -> None:
    record = OsmRecord.from_raw(
        {
            "osm_type": "way",
            "osm_id": 42,
            "name": "广州塔",
            "name_en": "Canton Tower",
            "lat": 23.1066,
            "lng": 113.3245,
            "source_url": "https://www.openstreetmap.org/way/42",
            "primary_tag": "tourism=artwork",
            "tags": {"wikidata": "Q193345"},
        }
    )
    assert record.osm_type == "way"
    assert record.osm_id == 42
    assert record.name_en == "Canton Tower"
    assert record.external_id == "way/42"
    assert record.tags == {"wikidata": "Q193345"}


@pytest.mark.parametrize("raw_name_en", [None, ""])
def test_osm_record_from_raw_normalizes_empty_name_en(raw_name_en: str | None) -> None:
    """空字符串与缺失都归一化为 None，避免下游出现"有英文名但为空串"的分歧。"""
    record = OsmRecord.from_raw(
        {
            "osm_type": "node",
            "osm_id": 1,
            "name": "陈家祠",
            "name_en": raw_name_en,
            "lat": 23.1,
            "lng": 113.2,
            "source_url": "https://www.openstreetmap.org/node/1",
            "primary_tag": "tourism=attraction",
        }
    )
    assert record.name_en is None


def test_osm_record_from_raw_tolerates_missing_tags() -> None:
    record = OsmRecord.from_raw(
        {
            "osm_type": "node",
            "osm_id": 1,
            "name": "陈家祠",
            "lat": 23.1,
            "lng": 113.2,
            "source_url": "https://www.openstreetmap.org/node/1",
            "primary_tag": "tourism=attraction",
        }
    )
    assert record.tags == {}


def test_external_id_is_unique_across_osm_types() -> None:
    """同名同 id 但类型不同的元素必须区分开（node/1 与 way/1 是两个实体）。"""
    node = make_record(osm_type="node", osm_id=7)
    way = make_record(osm_type="way", osm_id=7)
    assert node.external_id != way.external_id


# ════════════════════════════════════════════════════════════════════════════
# 二、EnrichOutcome：保留/丢弃的判定
# ════════════════════════════════════════════════════════════════════════════


def test_outcome_kept_reflects_place_presence() -> None:
    assert EnrichOutcome(None, DropReason.NAME_TOO_SHORT).kept is False
    assert EnrichOutcome(None).kept is False
    assert EnrichOutcome(place=None).reason is None


def test_every_drop_reason_is_distinct_and_serializable() -> None:
    """DropReason 会写进建库报告，取值必须稳定（改动会破坏报告的历史可比性）。"""
    values = [reason.value for reason in DropReason]
    assert len(values) == len(set(values)), "DropReason 有重复取值"
    assert all(v == v.lower() for v in values), "DropReason 取值应统一为小写下划线风格"


# ════════════════════════════════════════════════════════════════════════════
# 三、丢弃路径：每一条过滤规则都要有一个"必被拦下"的用例
# ════════════════════════════════════════════════════════════════════════════


def test_short_name_is_dropped(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(make_record("祠", tags={"tourism": "attraction"}), cfg)
    assert outcome.kept is False
    assert outcome.reason is DropReason.NAME_TOO_SHORT


def test_meaningless_name_is_dropped(cfg: SeedConfig) -> None:
    """纯数字/符号的名称没有信息量（如 "7-11"、"P3" 之外的 "123"）。"""
    outcome = enrich_or_reason(make_record("1234", tags={"tourism": "attraction"}), cfg)
    assert outcome.reason is DropReason.MEANINGLESS_NAME


def test_coord_out_of_bbox_is_dropped(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(make_record(lat=20.0, lng=113.2, tags={"tourism": "attraction"}), cfg)
    assert outcome.reason is DropReason.COORD_OUT_OF_BBOX


def test_coord_just_outside_bbox_is_dropped(cfg: SeedConfig) -> None:
    """bbox 是 22.50–23.95 / 112.90–114.05，边界值本身算在内。"""
    bbox = cfg.coord.bbox
    assert (
        enrich_or_reason(
            make_record(lat=bbox["min_lat"], lng=bbox["min_lng"], tags={"tourism": "attraction"}), cfg
        ).kept
        is True
    )
    assert (
        enrich_or_reason(
            make_record(lat=bbox["min_lat"] - 0.001, lng=bbox["min_lng"], tags={"tourism": "attraction"}),
            cfg,
        ).reason
        is DropReason.COORD_OUT_OF_BBOX
    )


def test_boundary_catches_what_tag_and_name_fallbacks_miss(cfg: SeedConfig) -> None:
    """★ 核心回归：既无邻市地址标签、名称也不含邻市名的越界地点，**只有边界能拦下**。

    这正是真实 bug 的形态 —— 矩形 bbox 的四角切进邻市，而那些地点往往
    既没有 addr:city、名称里也不含"深圳/东莞"。地址标签与名称前缀过滤对它们无能为力。
    """
    record = make_record(
        "某某野生动物园",
        primary_tag="tourism=zoo",
        lat=OUTSIDE_BOUNDARY[0],
        lng=OUTSIDE_BOUNDARY[1],
        tags={"tourism": "zoo"},
    )
    # 没有边界数据时：这个越界地点会被**放行**（历史 bug 的成因）
    assert enrich_or_reason(record, cfg).kept is True, "前提失效：该地点本应能通过 bbox 检查"
    # 有边界数据时：被拦下
    outcome = enrich_or_reason(record, cfg, boundary=CITY_BOUNDARY)
    assert outcome.reason is DropReason.OUTSIDE_CITY_BOUNDARY


def test_boundary_takes_precedence_over_name_fallback(cfg: SeedConfig) -> None:
    """同时能被边界和名称前缀判出越界时，报**边界**原因（更权威、更接近事实）。"""
    outcome = enrich_or_reason(
        make_record(
            "深圳野生动物园",
            primary_tag="tourism=zoo",
            lat=OUTSIDE_BOUNDARY[0],
            lng=OUTSIDE_BOUNDARY[1],
            tags={"tourism": "zoo"},
        ),
        cfg,
        boundary=CITY_BOUNDARY,
    )
    assert outcome.reason is DropReason.OUTSIDE_CITY_BOUNDARY


def test_name_fallback_still_works_without_boundary(cfg: SeedConfig) -> None:
    """边界数据缺失（Overpass 曾 504）时，名称前缀兜底必须继续生效。"""
    outcome = enrich_or_reason(
        make_record("深圳野生动物园", primary_tag="tourism=zoo", tags={"tourism": "zoo"}), cfg
    )
    assert outcome.reason is DropReason.OUTSIDE_CITY_BY_NAME


def test_boundary_check_precedes_curated_protection(cfg: SeedConfig) -> None:
    """★ 保护名单保护的是"是不是真实目的地"，不是"在不在广州"。

    深圳的动物园再有名、再被人工保护，也不该出现在广州路线里。
    """
    outcome = enrich_or_reason(
        make_record(
            "深圳野生动物园",
            primary_tag="tourism=zoo",
            lat=OUTSIDE_BOUNDARY[0],
            lng=OUTSIDE_BOUNDARY[1],
            tags={"tourism": "zoo"},
        ),
        cfg,
        protected_names=frozenset({"深圳野生动物园"}),
        boundary=CITY_BOUNDARY,
    )
    assert outcome.reason is DropReason.OUTSIDE_CITY_BOUNDARY


def test_outside_city_by_addr_tag_is_dropped(cfg: SeedConfig) -> None:
    """边界不可用时的兜底：addr:city 明确写了邻市。"""
    outcome = enrich_or_reason(make_record(tags={"tourism": "attraction", "addr:city": "深圳市"}), cfg)
    assert outcome.reason is DropReason.OUTSIDE_CITY


def test_outside_city_by_neighbor_district_is_dropped(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(make_record(tags={"tourism": "attraction", "addr:district": "南山区"}), cfg)
    assert outcome.reason is DropReason.OUTSIDE_CITY


def test_guangzhou_addr_tag_is_not_treated_as_outside(cfg: SeedConfig) -> None:
    """明确属于广州的地址标签必须放行，不能因为"含城市名"就误判。"""
    for addr_city in ("广州", "广州市", "Guangzhou"):
        outcome = enrich_or_reason(make_record(tags={"tourism": "attraction", "addr:city": addr_city}), cfg)
        assert outcome.kept is True, f"addr:city={addr_city} 被误判为城市外"


def test_guangzhou_district_is_not_treated_as_outside(cfg: SeedConfig) -> None:
    for district in ("越秀区", "天河区", "海珠区"):
        outcome = enrich_or_reason(
            make_record(tags={"tourism": "attraction", "addr:district": district}), cfg
        )
        assert outcome.kept is True, f"addr:district={district} 被误判为城市外"


def test_outside_city_by_name_prefix_is_dropped(cfg: SeedConfig) -> None:
    for name in ("东莞可园", "佛山祖庙", "深圳湾公园", "中山市博物馆"):
        outcome = enrich_or_reason(
            make_record(name, primary_tag="leisure=park", tags={"leisure": "park", "wikidata": "Q1"}), cfg
        )
        assert outcome.reason is DropReason.OUTSIDE_CITY_BY_NAME, f"{name} 未被识别为邻市地点"


@pytest.mark.parametrize(
    "name",
    [
        "中山纪念堂",  # 广州本地地标
        "中山纪念图书馆",
        "国立中山大学旧址",
        "孙中山文献馆",
        "中山大学",
    ],
)
def test_bare_zhongshan_prefix_is_not_filtered(cfg: SeedConfig, name: str) -> None:
    """★ 回归：neighbor_name_prefixes 刻意不含裸「中山」。

    实测教训：用「中山」做前缀过滤会把中山纪念堂、国立中山大学旧址、
    孙中山文献馆成批误删 —— 这些都是广州本地地标。
    配置层还有一道断言禁止把「中山」写进前缀表（见 test_config.py）。
    """
    outcome = enrich_or_reason(
        make_record(name, primary_tag="historic=building", tags={"historic": "building"}), cfg
    )
    assert outcome.reason is not DropReason.OUTSIDE_CITY_BY_NAME


def test_blocklisted_name_is_dropped(cfg: SeedConfig) -> None:
    for name in ("公共厕所", "停车场", "地铁站", "充电站"):
        outcome = enrich_or_reason(make_record(name, tags={"tourism": "attraction"}), cfg)
        assert outcome.reason is DropReason.BLOCKLISTED_NAME, f"{name} 未被黑名单拦下"


def test_commodity_chain_is_dropped(cfg: SeedConfig) -> None:
    """★ 通用商品连锁整条丢弃：瑞幸 319 家、星巴克 287 家会把候选池冲垮。"""
    for name in ("瑞幸咖啡(天河路店)", "星巴克臻选", "蜜雪冰城", "麦当劳", "7-11便利店"):
        outcome = enrich_or_reason(
            make_record(name, primary_tag="amenity=cafe", tags={"amenity": "cafe"}), cfg
        )
        assert outcome.reason in (DropReason.COMMODITY_CHAIN, DropReason.MEANINGLESS_NAME), (
            f"{name} 未被连锁品牌规则拦下，实际原因 {outcome.reason}"
        )


def test_local_food_brand_is_not_blocklisted(cfg: SeedConfig) -> None:
    """非连锁的本地餐饮不应被误杀。"""
    outcome = enrich_or_reason(
        make_record("陈添记", primary_tag="amenity=restaurant", tags={"amenity": "restaurant"}), cfg
    )
    assert outcome.kept is True


def test_dropped_primary_tag_is_dropped(cfg: SeedConfig) -> None:
    """★ 回归：白云山在 OSM 里是 natural=peak，整类丢弃曾把地标误杀。"""
    outcome = enrich_or_reason(
        make_record("无名小丘", primary_tag="natural=peak", tags={"natural": "peak"}), cfg
    )
    assert outcome.reason is DropReason.DROPPED_TAG


def test_unknown_category_is_dropped(cfg: SeedConfig) -> None:
    """primary_tag 既不在显式映射里、前缀也没有兜底 → 不猜类别，直接丢弃。"""
    outcome = enrich_or_reason(
        make_record("某汽车修理厂", primary_tag="shop=car_repair", tags={"shop": "car_repair"}), cfg
    )
    assert outcome.reason is DropReason.UNKNOWN_CATEGORY


def test_missing_verifiable_signal_is_dropped_for_required_category(cfg: SeedConfig) -> None:
    """★ nature 类别必须带可核验信号：1625 个 leisure=park 里多数是口袋公园。"""
    outcome = enrich_or_reason(
        make_record("某某社区公园", primary_tag="leisure=park", tags={"leisure": "park"}), cfg
    )
    assert outcome.reason is DropReason.MISSING_VERIFIABLE_SIGNAL


def test_missing_verifiable_signal_is_dropped_for_required_primary_tag(cfg: SeedConfig) -> None:
    """★ 141 个 amenity=library 里绝大多数是社区分馆；广州图书馆有 wikidata 才留下。"""
    outcome = enrich_or_reason(
        make_record("某某社区图书馆", primary_tag="amenity=library", tags={"amenity": "library"}), cfg
    )
    assert outcome.reason is DropReason.MISSING_VERIFIABLE_SIGNAL


def test_artwork_without_signal_is_dropped(cfg: SeedConfig) -> None:
    """★ 回归：tourism=artwork 有 260 条无名街头雕塑，粒度过细。"""
    outcome = enrich_or_reason(
        make_record("无名雕塑", primary_tag="tourism=artwork", tags={"tourism": "artwork"}), cfg
    )
    assert outcome.reason is DropReason.MISSING_VERIFIABLE_SIGNAL


@pytest.mark.parametrize(
    "signal_tag",
    [
        {"wikidata": "Q1"},
        {"wikipedia": "zh:广州图书馆"},
        {"website": "https://www.gzlib.org.cn"},
        {"opening_hours": "09:00-17:00"},
        {"phone": "+86 20 1234 5678"},
    ],
)
def test_any_single_signal_rescues_a_required_category(cfg: SeedConfig, signal_tag: dict[str, str]) -> None:
    """signal_tag_keys 里**任意一个**标签存在即可通过 —— 这是 AND 之外的 OR 语义。"""
    tags = {"amenity": "library", **signal_tag}
    outcome = enrich_or_reason(make_record("广州图书馆", primary_tag="amenity=library", tags=tags), cfg)
    assert outcome.kept is True, f"{signal_tag} 未能让地点通过信号门槛"


# ════════════════════════════════════════════════════════════════════════════
# 四、人工保护名单
# ════════════════════════════════════════════════════════════════════════════


def test_protected_name_skips_blocklist(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record("停车场", tags={"tourism": "attraction"}),
        cfg,
        protected_names=frozenset({"停车场"}),
    )
    assert outcome.kept is True


def test_protected_name_skips_dropped_primary_tag(cfg: SeedConfig) -> None:
    """★ 白云山（natural=peak）就是靠这条留下来的。"""
    outcome = enrich_or_reason(
        make_record("白云山", primary_tag="natural=peak", tags={"natural": "peak"}),
        cfg,
        protected_names=frozenset({"白云山"}),
    )
    assert outcome.kept is True


def test_protected_name_skips_signal_requirement_and_gets_flagged(cfg: SeedConfig) -> None:
    """★ 广州塔（tourism=artwork，无 wikidata）靠保护名单入库，但必须打标记。"""
    outcome = enrich_or_reason(
        make_record("广州塔", primary_tag="tourism=artwork", tags={"tourism": "artwork"}),
        cfg,
        protected_names=frozenset({"广州塔"}),
    )
    assert outcome.kept is True
    assert outcome.place is not None
    assert "kept_by_curated_protection" in outcome.place.data_quality_flags


def test_protected_name_with_signal_does_not_get_protection_flag(cfg: SeedConfig) -> None:
    """有信号的地点本来就该入库，不该被标成"靠保护进来的"。"""
    outcome = enrich_or_reason(
        make_record("广州塔", primary_tag="tourism=artwork", tags={"tourism": "artwork", "wikidata": "Q1"}),
        cfg,
        protected_names=frozenset({"广州塔"}),
    )
    assert outcome.kept is True
    assert outcome.place is not None
    assert "kept_by_curated_protection" not in outcome.place.data_quality_flags


def test_protection_uses_normalized_names(cfg: SeedConfig) -> None:
    """保护名单比对的是归一化名称，所以带括号/空格的人工条目也能命中。"""
    outcome = enrich_or_reason(
        make_record("广州塔（小蛮腰）", primary_tag="tourism=artwork", tags={"tourism": "artwork"}),
        cfg,
        protected_names=frozenset({"广州塔"}),
    )
    assert outcome.kept is True


def test_protection_can_be_disabled_by_config() -> None:
    """protect_curated_names=false 时规则必须重新生效（配置开关不能是摆设）。"""
    raw = raw_seed()
    raw["relevance"]["protect_curated_names"] = False
    strict = SeedConfig.model_validate(raw)
    outcome = enrich_or_reason(
        make_record("广州塔", primary_tag="tourism=artwork", tags={"tourism": "artwork"}),
        strict,
        protected_names=frozenset({"广州塔"}),
    )
    assert outcome.reason is DropReason.MISSING_VERIFIABLE_SIGNAL


def test_unprotected_names_are_unaffected(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record("无名雕塑", primary_tag="tourism=artwork", tags={"tourism": "artwork"}),
        cfg,
        protected_names=frozenset({"广州塔"}),
    )
    assert outcome.reason is DropReason.MISSING_VERIFIABLE_SIGNAL


# ════════════════════════════════════════════════════════════════════════════
# 五、类别判定：显式映射 → 前缀兜底 → 类别覆盖
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("primary_tag", "expected"),
    [
        ("tourism=museum", "museum"),
        ("tourism=gallery", "museum"),
        ("tourism=artwork", "photo"),
        ("tourism=attraction", "attraction"),
        ("tourism=viewpoint", "nightview"),
        ("tourism=zoo", "family"),
        ("historic=memorial", "historic"),
        ("historic=building", "historic"),
        ("amenity=restaurant", "food"),
        ("amenity=cafe", "cafe"),
        ("amenity=marketplace", "shopping"),
        ("amenity=place_of_worship", "historic"),
        ("shop=mall", "shopping"),
        ("leisure=park", "nature"),
        ("railway=station", "transport_hub"),
    ],
)
def test_explicit_tag_mapping(cfg: SeedConfig, primary_tag: str, expected: str) -> None:
    key, value = primary_tag.split("=", 1)
    tags = {key: value, "wikidata": "Q1"}  # 带上信号，避免被 require_signal 拦下
    outcome = enrich_or_reason(make_record(primary_tag=primary_tag, tags=tags), cfg)
    assert outcome.kept is True, f"{primary_tag} 被丢弃（{outcome.reason}）"
    assert outcome.place is not None
    assert outcome.place.category == expected


@pytest.mark.parametrize(
    ("primary_tag", "expected"),
    [
        ("historic=city_gate", "historic"),  # ★ 南海神庙：曾经因未枚举而无法归类
        ("tourism=camp_site", "attraction"),
        ("leisure=playground", "nature"),
        ("natural=cave_entrance", "nature"),
        ("place=suburb", "district"),
    ],
)
def test_prefix_fallback_covers_divergent_values(cfg: SeedConfig, primary_tag: str, expected: str) -> None:
    """★ 回归：OSM 的 historic 有 30+ 种取值，逐个枚举必漏。

    实测教训：historic=city_gate 未枚举到，导致南海神庙无法归类而被丢弃。
    """
    key, value = primary_tag.split("=", 1)
    outcome = enrich_or_reason(
        make_record("测试地点", primary_tag=primary_tag, tags={key: value, "wikidata": "Q1"}), cfg
    )
    assert outcome.kept is True, f"{primary_tag} 被丢弃（{outcome.reason}）"
    assert outcome.place is not None
    assert outcome.place.category == expected


def test_explicit_mapping_beats_prefix_fallback(cfg: SeedConfig) -> None:
    """显式映射优先于前缀兜底：tourism=viewpoint 应是 nightview 而不是 attraction。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=viewpoint", tags={"tourism": "viewpoint", "wikidata": "Q1"}), cfg
    )
    assert outcome.place is not None
    assert outcome.place.category == "nightview"


def test_override_historic_wins_over_attraction(cfg: SeedConfig) -> None:
    """带 historic 标签的实体归为历史人文，而不是景点。"""
    outcome = enrich_or_reason(
        make_record(
            "某历史建筑",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "historic": "building", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.category == "historic"


def test_override_except_categories_are_respected(cfg: SeedConfig) -> None:
    """transport_hub 在 except_categories 里：火车站即使带 historic 也不改成 historic。"""
    outcome = enrich_or_reason(
        make_record(
            "广州南站",
            primary_tag="railway=station",
            tags={"railway": "station", "historic": "building", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.category == "transport_hub"


def test_override_by_name_suffix_walking_street(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "北京路步行街",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.category == "district"


def test_override_by_name_suffix_cafe(cfg: SeedConfig) -> None:
    """规则是「名称**以**咖啡结尾」，不是「名称包含咖啡」—— 后缀锚点是有意的。"""
    outcome = enrich_or_reason(
        make_record(
            "老树咖啡",
            primary_tag="amenity=restaurant",
            tags={"amenity": "restaurant", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.category == "cafe"


def test_name_override_requires_suffix_anchor(cfg: SeedConfig) -> None:
    """「咖啡馆」结尾不匹配 —— 规则锚定的是「咖啡」结尾，不是任意包含。

    这里把实际行为钉住：如果将来要放宽成"包含"，需要同步改配置与这条测试。
    """
    outcome = enrich_or_reason(
        make_record(
            "某某咖啡馆",
            primary_tag="amenity=restaurant",
            tags={"amenity": "restaurant", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.category == "food"


def test_overrides_are_first_match_wins(cfg: SeedConfig) -> None:
    """★ 顺序匹配、先命中先用：带 historic 标签且名称以步行街结尾时，
    第 1 条规则（historic）先命中，就不会再被第 2 条改成 district。
    """
    outcome = enrich_or_reason(
        make_record(
            "某某步行街",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "historic": "yes", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.category == "historic"


# ════════════════════════════════════════════════════════════════════════════
# 六、分值推导
# ════════════════════════════════════════════════════════════════════════════


def test_scores_start_from_category_base(cfg: SeedConfig) -> None:
    """没有任何信号时，分值就等于类别基线（可复现、可审计）。"""
    outcome = enrich_or_reason(
        make_record("陈家祠", primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    for dim, base in cfg.score_bases["attraction"].items():
        assert outcome.place.scores[dim] == pytest.approx(base)


def test_signal_adds_to_scores(cfg: SeedConfig) -> None:
    """wikidata 信号：popularity +0.24、food +0.06。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    base = cfg.score_bases["attraction"]
    assert outcome.place.scores["popularity"] == pytest.approx(base["popularity"] + 0.24)
    assert outcome.place.scores["food"] == pytest.approx(base["food"] + 0.06)


def test_multiple_signals_stack(cfg: SeedConfig) -> None:
    """多个信号叠加：wikidata + website + opening_hours 都给 popularity 加分。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={
                "tourism": "attraction",
                "wikidata": "Q1",
                "website": "https://example.org",
                "opening_hours": "09:00-17:00",
            },
        ),
        cfg,
    )
    assert outcome.place is not None
    expected = cfg.score_bases["attraction"]["popularity"] + 0.24 + 0.08 + 0.08
    assert outcome.place.scores["popularity"] == pytest.approx(round(expected, 3))


def test_matched_signals_are_recorded(cfg: SeedConfig) -> None:
    """命中过哪些信号必须可回溯 —— 否则"这个分值是怎么来的"无法回答。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1", "website": "https://example.org"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert set(outcome.place.matched_signals) >= {"wikidata_or_wikipedia", "has_website"}


def test_signal_requires_tag_value_match(cfg: SeedConfig) -> None:
    """tourism_viewpoint 只在 tourism=viewpoint 时命中，attraction 不命中。"""
    attraction = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    viewpoint = enrich_or_reason(
        make_record(primary_tag="tourism=viewpoint", tags={"tourism": "viewpoint"}), cfg
    )
    assert attraction.place is not None and viewpoint.place is not None
    assert "tourism_viewpoint" not in attraction.place.matched_signals
    assert "tourism_viewpoint" in viewpoint.place.matched_signals


def test_signal_when_category_in_is_enforced() -> None:
    """when_category_in 是类别门禁：不满足时该信号**完全不应生效**。

    当前 config/seed.yaml 没有用到 when_category_in，这条通过合成配置覆盖 ——
    否则这个分支会一直是未执行代码，等 M2 真用上时才发现判反了。
    """
    raw = raw_seed()
    raw["tag_signals"].append(
        {
            "id": "synthetic_food_only",
            "when_category_in": ["food"],
            "when_tag_key_present": ["wikidata"],
            "add": {"food": 0.30},
        }
    )
    synthetic = SeedConfig.model_validate(raw)

    # 餐饮：命中
    food = enrich_or_reason(
        make_record(
            "陈添记", primary_tag="amenity=restaurant", tags={"amenity": "restaurant", "wikidata": "Q1"}
        ),
        synthetic,
    )
    assert food.place is not None
    assert "synthetic_food_only" in food.place.matched_signals

    # 景点：类别不符，不命中
    attraction = enrich_or_reason(
        make_record(
            "陈家祠", primary_tag="tourism=attraction", tags={"tourism": "attraction", "wikidata": "Q1"}
        ),
        synthetic,
    )
    assert attraction.place is not None
    assert "synthetic_food_only" not in attraction.place.matched_signals


def test_signal_is_not_triggered_by_missing_tag(cfg: SeedConfig) -> None:
    """条件里的标签不存在时必须判为不命中，而不是当成"空值等于空值"而命中。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    # restaurant 信号要求 amenity ∈ {restaurant, food_court}，这里没有 amenity 键
    assert "restaurant" not in outcome.place.matched_signals
    # always_open 要求 opening_hours == 24/7，这里没有 opening_hours 键
    assert "always_open" not in outcome.place.matched_signals


def test_scores_are_clamped_to_unit_interval(cfg: SeedConfig) -> None:
    """任何信号叠加都不得把分值推过 1.0 或压到 0 以下。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={
                "tourism": "attraction",
                "wikidata": "Q1",
                "wikipedia": "zh:陈家祠",
                "website": "https://example.org",
                "opening_hours": "24/7",
                "phone": "+86 20 1234 5678",
                "heritage": "yes",
                "fee": "no",
                "wheelchair": "yes",
                "cuisine": "cantonese",
                "historic": "building",
            },
        ),
        cfg,
    )
    assert outcome.place is not None
    for dim, value in outcome.place.scores.items():
        assert 0.0 <= value <= 1.0, f"{dim}={value} 越界"


def test_scores_are_rounded_to_three_decimals(cfg: SeedConfig) -> None:
    """分值保留 3 位小数：让快照可复现，也避免浮点噪声写进数据库。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1", "website": "https://example.org"},
        ),
        cfg,
    )
    assert outcome.place is not None
    for value in outcome.place.scores.values():
        assert value == round(value, 3)


def test_signal_set_min_and_set_max_are_applied() -> None:
    """set_min / set_max 是"保底/封顶"语义，与 add 的"累加"不同。

    当前 config/seed.yaml 的 tag_signals 只用到了 add，这条测试通过**合成配置**
    覆盖 set_min / set_max 两个分支 —— 否则它们会一直是未执行代码，
    等 M2 真的用上时才发现语义写错。
    """
    raw = raw_seed()
    raw["tag_signals"].append(
        {
            "id": "synthetic_floor_ceiling",
            "when_tag_key_present": ["wikidata"],
            "set_min": {"culture": 0.95, "photo": 0.90},
            "set_max": {"food": 0.05, "night_view": 0.02},
        }
    )
    synthetic = SeedConfig.model_validate(raw)
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        synthetic,
    )
    assert outcome.place is not None
    scores = outcome.place.scores
    assert scores["culture"] == pytest.approx(0.95)  # 抬到下限
    assert scores["photo"] == pytest.approx(0.90)
    assert scores["food"] == pytest.approx(0.05)  # 压到上限
    assert scores["night_view"] == pytest.approx(0.02)


def test_signal_set_max_does_not_raise_a_lower_value() -> None:
    """set_max 是封顶不是赋值：本来就低于上限的值不应被抬上去。"""
    raw = raw_seed()
    raw["tag_signals"].append(
        {
            "id": "synthetic_ceiling",
            "when_tag_key_present": ["wikidata"],
            "set_max": {"culture": 0.99},
        }
    )
    synthetic = SeedConfig.model_validate(raw)
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        synthetic,
    )
    assert outcome.place is not None
    # 基线 0.55 + 无 culture 加成 = 0.55 < 0.99，应保持 0.55
    assert outcome.place.scores["culture"] == pytest.approx(0.55)


def test_signal_set_min_does_not_lower_a_higher_value() -> None:
    """set_min 是保底不是赋值：本来就高于下限的值不应被压下去。"""
    raw = raw_seed()
    raw["tag_signals"].append(
        {
            "id": "synthetic_floor",
            "when_tag_key_present": ["wikidata"],
            "set_min": {"culture": 0.10},
        }
    )
    synthetic = SeedConfig.model_validate(raw)
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        synthetic,
    )
    assert outcome.place is not None
    assert outcome.place.scores["culture"] == pytest.approx(0.55)


def test_to_scores_payload_maps_to_column_names() -> None:
    """分值要写成 ``{dim}_score`` 列；缺失维度不写入（保持 NULL 语义）。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), get_seed_config()
    )
    assert outcome.place is not None
    payload = outcome.place.to_scores_payload()
    assert "popularity_score" in payload
    assert "popularity" not in payload
    assert all(key.endswith("_score") for key in payload)


# ════════════════════════════════════════════════════════════════════════════
# 七、停留时长与最佳时段
# ════════════════════════════════════════════════════════════════════════════


def test_duration_comes_from_category_base(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(make_record(primary_tag="amenity=cafe", tags={"amenity": "cafe"}), cfg)
    assert outcome.place is not None
    assert outcome.place.recommended_duration_min == cfg.duration_bases["cafe"]


def test_duration_signal_raises_to_minimum(cfg: SeedConfig) -> None:
    """主题乐园/动物园/水族馆至少 240 分钟 —— 基线 180 分钟明显不够。"""
    outcome = enrich_or_reason(
        make_record("广州动物园", primary_tag="tourism=zoo", tags={"tourism": "zoo", "wikidata": "Q1"}),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.recommended_duration_min == 240


def test_duration_set_min_never_lowers_a_higher_base(cfg: SeedConfig) -> None:
    """set_min 是保底不是赋值：attraction 基线 90 分钟 > 下限 60，应保持 90。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠", primary_tag="tourism=attraction", tags={"tourism": "attraction", "wikidata": "Q1"}
        ),
        cfg,
    )
    assert outcome.place is not None
    assert cfg.duration_bases["attraction"] == 90
    assert outcome.place.recommended_duration_min == 90


def test_duration_signal_raises_short_categories(cfg: SeedConfig) -> None:
    """cafe 基线 45 分钟，wikidata 的 set_min=60 会把它抬到 60。"""
    outcome = enrich_or_reason(
        make_record("老树咖啡", primary_tag="amenity=cafe", tags={"amenity": "cafe", "wikidata": "Q1"}),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.recommended_duration_min == 60


def test_duration_without_signals_equals_base(cfg: SeedConfig) -> None:
    """没有任何信号时，停留时长就是类别基线。"""
    outcome = enrich_or_reason(
        make_record("老树咖啡", primary_tag="amenity=cafe", tags={"amenity": "cafe"}),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.recommended_duration_min == cfg.duration_bases["cafe"]


def test_duration_set_max_is_applied() -> None:
    """set_max 分支：当前配置未使用，用合成配置覆盖。"""
    raw = raw_seed()
    raw["duration_signals"].append({"when_tag_key_present": ["wikidata"], "set_max": 20})
    synthetic = SeedConfig.model_validate(raw)
    outcome = enrich_or_reason(
        make_record(
            "陈家祠", primary_tag="tourism=attraction", tags={"tourism": "attraction", "wikidata": "Q1"}
        ),
        synthetic,
    )
    assert outcome.place is not None
    assert outcome.place.recommended_duration_min == 20


def test_best_time_from_category(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(primary_tag="amenity=restaurant", tags={"amenity": "restaurant"}), cfg
    )
    assert outcome.place is not None
    assert outcome.place.best_time == tuple(cfg.best_time_by_category["food"])


def test_best_time_signal_overrides_category(cfg: SeedConfig) -> None:
    """观景点的最佳时段是日落与夜晚，覆盖 nightview 的类别默认值。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=viewpoint", tags={"tourism": "viewpoint"}), cfg
    )
    assert outcome.place is not None
    assert outcome.place.best_time == ("sunset", "night")


def test_transport_hub_has_no_best_time(cfg: SeedConfig) -> None:
    """交通枢纽不作为游玩站点，因此没有最佳时段。"""
    outcome = enrich_or_reason(
        make_record("广州南站", primary_tag="railway=station", tags={"railway": "station"}), cfg
    )
    assert outcome.place is not None
    assert outcome.place.best_time == ()


# ════════════════════════════════════════════════════════════════════════════
# 八、标签生成
# ════════════════════════════════════════════════════════════════════════════


def test_tags_include_category_tags(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(primary_tag="amenity=restaurant", tags={"amenity": "restaurant"}), cfg
    )
    assert outcome.place is not None
    assert set(cfg.tags_by_category["food"]) <= set(outcome.place.tags)


def test_tags_include_signal_tags(cfg: SeedConfig) -> None:
    """命中 wikidata 信号 → 附带「知名」标签。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "知名" in outcome.place.tags


def test_tags_from_osm_tags_are_mapped(cfg: SeedConfig) -> None:
    """cuisine / religion / historic 标签会被映射成归一化标签。"""
    outcome = enrich_or_reason(
        make_record(
            "某某茶楼",
            primary_tag="amenity=restaurant",
            tags={"amenity": "restaurant", "cuisine": "cantonese"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "美食" in outcome.place.tags


def test_tags_are_deduplicated_and_order_preserved(cfg: SeedConfig) -> None:
    """去重但保持首次出现顺序 —— 可复现的输出才能做快照测试。"""
    outcome = enrich_or_reason(
        make_record(
            "某某茶楼",
            primary_tag="amenity=restaurant",
            tags={"amenity": "restaurant", "cuisine": "cantonese"},
        ),
        cfg,
    )
    assert outcome.place is not None
    tags = outcome.place.tags
    assert len(set(tags)) == len(tags), f"标签有重复：{tags}"
    assert list(tags).count("美食") == 1


# ════════════════════════════════════════════════════════════════════════════
# 九、诚实性：unknown_fields / field_scope / 质量标记
# ════════════════════════════════════════════════════════════════════════════


def test_unknown_fields_records_missing_opening_hours(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert "opening_hours" in outcome.place.unknown_fields
    assert outcome.place.opening_hours_raw is None


def test_unknown_fields_does_not_record_present_opening_hours(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "opening_hours": "09:00-17:00"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "opening_hours" not in outcome.place.unknown_fields


def test_price_is_unknown_without_fee_tag(cfg: SeedConfig) -> None:
    """★ 没有价格来源时必须记 unknown，绝不填估算值。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert "price_min" in outcome.place.unknown_fields
    assert "price_max" in outcome.place.unknown_fields


def test_price_unknown_cleared_when_fee_tag_present(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "fee": "no"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "price_min" not in outcome.place.unknown_fields
    assert "price_max" not in outcome.place.unknown_fields


def test_always_unknown_fields_are_recorded(cfg: SeedConfig) -> None:
    """季节性/拥挤度/预约要求没有可靠来源 → 恒为 unknown。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert {"best_season", "crowd_level", "reservation_required"} <= set(outcome.place.unknown_fields)


def test_field_scope_only_claims_what_osm_can_prove(cfg: SeedConfig) -> None:
    """★ 字段级溯源：OSM 没有价格信息，price_min 就绝不能出现在 field_scope 里。

    否则后来者会误以为"价格是有来源的"。
    """
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    scope = set(outcome.place.source_field_scope)
    assert {"name", "latitude", "longitude", "category"} <= scope
    assert "price_min" not in scope
    assert "price_max" not in scope
    assert "best_season" not in scope


def test_field_scope_grows_with_available_tags(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={
                "tourism": "attraction",
                "opening_hours": "09:00-17:00",
                "website": "https://example.org",
                "phone": "+86 20 1234 5678",
                "addr:district": "荔湾区",
                "description": "广州著名宗祠建筑",
            },
        ),
        cfg,
    )
    assert outcome.place is not None
    scope = set(outcome.place.source_field_scope)
    assert {"opening_hours", "website", "phone", "address", "description"} <= scope


def test_field_scope_excludes_unavailable_fields(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    scope = set(outcome.place.source_field_scope)
    assert "website" not in scope
    assert "phone" not in scope
    assert "address" not in scope
    assert "description" not in scope


def test_quality_flags_always_include_score_derived(cfg: SeedConfig) -> None:
    """分值永远不是事实，必须恒带 score_derived 标记。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert "score_derived" in outcome.place.data_quality_flags


def test_quality_flags_mark_stale_hours(cfg: SeedConfig) -> None:
    """OSM 的营业时间可能长期未更新，UI 必须提示"出发前确认"。"""
    outcome = enrich_or_reason(
        make_record(
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "opening_hours": "09:00-17:00"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "hours_from_osm_may_be_stale" in outcome.place.data_quality_flags


def test_quality_flags_mark_missing_district_and_website(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    flags = outcome.place.data_quality_flags
    assert "district_unknown" in flags
    assert "no_website" in flags


def test_quality_flags_mark_no_verifiable_signal(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert "no_verifiable_signal" in outcome.place.data_quality_flags


def test_quality_flags_absent_when_data_is_complete(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={
                "tourism": "attraction",
                "wikidata": "Q1",
                "website": "https://example.org",
                "addr:district": "荔湾区",
            },
        ),
        cfg,
    )
    assert outcome.place is not None
    flags = outcome.place.data_quality_flags
    assert "district_unknown" not in flags
    assert "no_website" not in flags
    assert "no_verifiable_signal" not in flags


# ════════════════════════════════════════════════════════════════════════════
# 十、字段映射与来源溯源
# ════════════════════════════════════════════════════════════════════════════


def test_district_only_comes_from_addr_district(cfg: SeedConfig) -> None:
    """★ 回归：把 addr:city 当成 district 候选，会让"越秀区"与"广州市"混在同一字段。

    前端按行政区聚合时就会出现"广州市"这种不是区的值。
    """
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "addr:city": "广州市", "addr:street": "中山七路"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.district is None, "addr:city 不应写进 district"
    assert outcome.place.address == "中山七路"


def test_district_comes_from_addr_district(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "addr:district": "荔湾区"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.district == "荔湾区"


def test_missing_fact_fields_stay_null(cfg: SeedConfig) -> None:
    """拿不到的字段一律 NULL，禁止用占位字符串填充。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert outcome.place.opening_hours_raw is None
    assert outcome.place.address is None
    assert outcome.place.district is None
    assert outcome.place.name_en is None


def test_indoor_is_null_when_undeterminable(cfg: SeedConfig) -> None:
    """indoor 的三态：True / False / None（无法确定）。attraction 属于 None。"""
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert outcome.place.indoor is None


def test_indoor_is_false_for_outdoor_categories(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "越秀公园",
            primary_tag="leisure=park",
            tags={"leisure": "park", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.indoor is False


def test_source_fields_are_traceable(cfg: SeedConfig) -> None:
    """★ 每条地点都必须能回到原始来源 —— 否则数据质量问题无法反馈修复。"""
    outcome = enrich_or_reason(
        make_record("陈家祠", primary_tag="tourism=attraction", tags={"tourism": "attraction"}, osm_id=99),
        cfg,
    )
    assert outcome.place is not None
    place = outcome.place
    assert place.source_name == SOURCE_NAME == "OpenStreetMap"
    assert place.external_source == SOURCE_NAME
    assert place.external_id == "node/99"
    assert place.source_url == "https://www.openstreetmap.org/node/99"
    assert place.source_credibility == pytest.approx(SOURCE_CREDIBILITY)


def test_verification_status_verified_with_wikidata(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.verification_status == "verified"
    assert outcome.place.confidence == pytest.approx(0.90)


def test_verification_status_probable_without_wikidata(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert outcome.place is not None
    assert outcome.place.verification_status == "probable"
    assert outcome.place.confidence == pytest.approx(0.70)


def test_wikipedia_also_triggers_verified(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikipedia": "zh:陈家祠"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.verification_status == "verified"


def test_signal_count_counts_distinct_signal_keys(cfg: SeedConfig) -> None:
    """signal_count 用于分店上限排序，必须等于命中的信号标签种类数。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={
                "tourism": "attraction",
                "wikidata": "Q1",
                "website": "https://example.org",
                "phone": "+86 20 1234 5678",
            },
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.signal_count == 3


def test_raw_tag_count_reflects_input(cfg: SeedConfig) -> None:
    tags = {"tourism": "attraction", "wikidata": "Q1", "website": "https://example.org"}
    outcome = enrich_or_reason(make_record(primary_tag="tourism=attraction", tags=tags), cfg)
    assert outcome.place is not None
    assert outcome.place.raw_tag_count == len(tags)


# ════════════════════════════════════════════════════════════════════════════
# 十一、别名生成
# ════════════════════════════════════════════════════════════════════════════


def test_aliases_exclude_the_canonical_name_itself(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "广州塔",
            primary_tag="tourism=artwork",
            tags={"tourism": "artwork", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "广州塔" not in outcome.place.aliases


def test_aliases_extract_bracket_content(cfg: SeedConfig) -> None:
    """「广州塔（小蛮腰）」→ 别名「小蛮腰」。这是机器唯一能安全推导的别名。"""
    outcome = enrich_or_reason(
        make_record(
            "广州塔（小蛮腰）",
            primary_tag="tourism=artwork",
            tags={"tourism": "artwork", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "小蛮腰" in outcome.place.aliases


def test_aliases_are_sorted_and_unique(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "北京路步行街",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    aliases = list(outcome.place.aliases)
    assert aliases == sorted(aliases)
    assert len(set(aliases)) == len(aliases)


def test_aliases_never_invent_external_knowledge(cfg: SeedConfig) -> None:
    """★ 程序不得猜测需要外部知识的别名（陈家祠 → 陈氏书院 只能来自人工数据）。"""
    outcome = enrich_or_reason(
        make_record(
            "陈家祠",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction", "wikidata": "Q1"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert "陈氏书院" not in outcome.place.aliases


def test_canonical_and_display_name_match_input(cfg: SeedConfig) -> None:
    outcome = enrich_or_reason(
        make_record(
            "  陈家祠  ",
            primary_tag="tourism=attraction",
            tags={"tourism": "attraction"},
        ),
        cfg,
    )
    assert outcome.place is not None
    assert outcome.place.canonical_name == "陈家祠"
    assert outcome.place.display_name == "陈家祠"


# ════════════════════════════════════════════════════════════════════════════
# 十二、enrich() 便利包装
# ════════════════════════════════════════════════════════════════════════════


def test_enrich_returns_place_for_valid_record(cfg: SeedConfig) -> None:
    place = enrich(
        make_record("陈家祠", primary_tag="tourism=attraction", tags={"tourism": "attraction"}), cfg
    )
    assert isinstance(place, EnrichedPlace)


def test_enrich_returns_none_for_dropped_record(cfg: SeedConfig) -> None:
    assert enrich(make_record("祠", tags={"tourism": "attraction"}), cfg) is None


def test_enrich_matches_enrich_or_reason() -> None:
    """两个入口对同一条记录必须给出一致结果（包装函数不能有额外逻辑）。"""
    cfg = get_seed_config()
    record = make_record("陈家祠", primary_tag="tourism=attraction", tags={"tourism": "attraction"})
    assert enrich(record, cfg) == enrich_or_reason(record, cfg).place


def test_enrichment_is_deterministic(cfg: SeedConfig) -> None:
    """同一输入必须得到完全相同的输出 —— 幂等建库的前提。"""
    record = make_record(
        "陈家祠",
        primary_tag="tourism=attraction",
        tags={"tourism": "attraction", "wikidata": "Q1", "website": "https://example.org"},
    )
    assert enrich_or_reason(record, cfg).place == enrich_or_reason(record, cfg).place


def test_enrichment_does_not_mutate_the_input_record(cfg: SeedConfig) -> None:
    """纯函数：不得就地修改传入的 tags。"""
    tags = {"tourism": "attraction", "wikidata": "Q1"}
    record = make_record("陈家祠", primary_tag="tourism=attraction", tags=tags)
    enrich_or_reason(record, cfg)
    assert tags == {"tourism": "attraction", "wikidata": "Q1"}
