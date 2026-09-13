"""地点类别常量（``app/domain/categories.py``）的一致性测试。

为什么值得单独测：
    这个模块自称"单一事实源"，同时被三处消费 ——
    数据库 CHECK 约束、丰富化规则的合法性校验、评分与候选生成。
    它的正确性不体现在"代码有没有 bug"，而体现在**三处是否始终一致**：
    配置里写了一个数据库不接受的类别、或某个类别永远产不出来，都只会
    在运行时以"数据莫名其妙是空的"这种形式暴露。

    实测踩过的两个坑，都变成了这里的回归：
      - `photo` 与 `citywalk` 曾经**没有任何产生者** —— 类别名留在枚举里，
        但没有任何 tag_to_category / 前缀兜底 / category_overrides 能产出它，
        于是这两个类别永远是空的（见 TASKS.md M1 问题 10、22）。
      - `CATEGORY_LABELS` 与 `PLACE_CATEGORIES` 脱节会让界面直接露出英文键名。
"""

from __future__ import annotations

import pytest

from app.core.config import get_seed_config
from app.domain.categories import CATEGORY_LABELS, NON_STOP_CATEGORIES, PLACE_CATEGORIES

pytestmark = pytest.mark.unit

# 目前**刻意**没有产生者的类别：值写的是原因，不是豁免理由。
# 想往里加条目，必须先回答"为什么允许它永远是空的"。
INTENTIONALLY_UNPRODUCED: dict[str, str] = {
    "citywalk": (
        "「适合 CityWalk」被定义为**属性**（walkability_score）而非类型，"
        "见 TASKS.md M1 问题 22。类别名保留是为了让 curated 数据仍能显式指定它。"
    ),
}


def _producers() -> set[str]:
    """config/seed.yaml 里所有能产出类别的规则：显式映射 + 前缀兜底 + 覆盖 + 兜底类别。"""
    seed = get_seed_config()
    produced = set(seed.tag_to_category.values())
    produced |= set(seed.tag_prefix_fallbacks.values())
    produced |= {override.set_category for override in seed.category_overrides}
    if seed.fallback_category is not None:
        produced.add(seed.fallback_category)
    return produced


# ── 枚举本身 ────────────────────────────────────────────────────────────────

def test_place_categories_are_unique() -> None:
    assert len(PLACE_CATEGORIES) == len(set(PLACE_CATEGORIES)), "PLACE_CATEGORIES 有重复项"


def test_place_categories_are_lowercase_snake_case() -> None:
    """类别会出现在 URL 查询参数与数据库 CHECK 约束里，取值风格必须统一。"""
    for category in PLACE_CATEGORIES:
        assert category == category.lower(), f"{category} 不是小写"
        assert category.replace("_", "").isalpha(), f"{category} 含非字母字符"


def test_non_stop_categories_are_a_subset() -> None:
    """"不作为游玩站点"的类别必须本身是合法类别，否则 CHECK 约束会拦下它。"""
    assert set(PLACE_CATEGORIES) >= NON_STOP_CATEGORIES


def test_transport_hub_is_the_only_non_stop_category() -> None:
    """当前只有交通枢纽不作为游玩站点。改动这里会直接影响路线站点选择，故显式钉住。"""
    assert frozenset({"transport_hub"}) == NON_STOP_CATEGORIES


# ── 中文标签与类别的对应 ────────────────────────────────────────────────────

def test_labels_cover_exactly_the_categories() -> None:
    """★ 一一对应：少一个会让界面露出英文键名，多一个说明有残留的死标签。"""
    assert set(CATEGORY_LABELS) == set(PLACE_CATEGORIES)


def test_labels_are_non_empty_and_translated() -> None:
    """标签不能为空，也不能等于键名（那等于没翻译）。"""
    for category, label in CATEGORY_LABELS.items():
        assert label.strip(), f"{category} 的中文名为空"
        assert label != category, f"{category} 没有中文名（标签等于键名）"


def test_labels_are_chinese() -> None:
    """面向用户，必须是中文。用"是否含中日韩字符"做粗判即可。"""
    for category, label in CATEGORY_LABELS.items():
        assert any("\u4e00" <= ch <= "\u9fff" for ch in label), (
            f"{category} 的中文名 {label!r} 里没有汉字"
        )


def test_category_labels_do_not_leak_preference_wording() -> None:
    """★ 回归：类别分布表曾经混入偏好维度的叫法（「美食」），
    导致同一张表里既有「餐饮」又有「博物馆」两套词汇。

    注意「亲子」「购物」在两个词汇表里**指同一个概念**，重名是正确的 ——
    所以要钉的不是"两表不能有任何交集"，而是"偏好怎么说 ≠ 类别怎么说"的那几个词
    绝不能出现在类别名里。类别分布由 /cities/{slug}/stats 输出，
    偏好怎么说属于 /meta/scoring-config 的职责。
    """
    preference_only_words = {"美食", "拍照", "文化", "夜景"}
    leaked = preference_only_words & set(CATEGORY_LABELS.values())
    assert not leaked, (
        f"偏好维度的叫法漏进了类别名：{sorted(leaked)}。"
        "类别应当用「餐饮」「拍照机位」「历史人文」「夜景观景」。"
    )


def test_category_stats_use_category_labels_not_preference_labels() -> None:
    """反向确认：偏好维度的标签里确实存在上面那几个"另一种说法"，否则上一条会空转。"""
    from app.core.config import get_scoring_config

    scoring_labels = {dim.label for dim in get_scoring_config().preference_dimensions.values()}
    assert {"美食", "拍照", "文化", "夜景"} <= scoring_labels, (
        "偏好维度里没有这些说法，说明测试前提变了 —— 请重新确认类别/偏好的用词分工"
    )


# ── 与 config/seed.yaml 的一致性 ────────────────────────────────────────────

def test_every_category_has_a_producer() -> None:
    """★ 每个类别都必须能从 OSM 标签推导出来，否则它永远是空的。

    这是本项目最容易犯的一类配置错误：类别名加进枚举、忘了加产生规则，
    结果"200+ 地点"里该类别一条都没有，而没有任何报错。
    """
    missing = set(PLACE_CATEGORIES) - _producers()
    unexpected = missing - set(INTENTIONALLY_UNPRODUCED)
    assert not unexpected, (
        f"这些类别没有任何产生者（会永远为空）：{sorted(unexpected)}。"
        "要么补上 tag_to_category / 前缀兜底 / category_overrides 规则，"
        "要么加进 INTENTIONALLY_UNPRODUCED 并写明原因。"
    )


def test_known_unproduced_categories_stay_documented() -> None:
    """已知缺口必须有书面理由；若将来补上了产生者，请把条目删掉。"""
    produced = _producers()
    for category, reason in INTENTIONALLY_UNPRODUCED.items():
        assert category in PLACE_CATEGORIES, f"{category} 已不在类别枚举里"
        assert reason.strip(), f"{category} 被标记为刻意不产出，但没有写原因"
    already_produced = sorted(set(INTENTIONALLY_UNPRODUCED) & produced)
    assert not already_produced, (
        f"这些类别已经有产生者了，请从 INTENTIONALLY_UNPRODUCED 里删掉：{already_produced}"
    )


def test_every_category_has_scoring_and_metadata() -> None:
    """类别在 seed.yaml 里必须有分值基线、时长基线、室内判定与标签，缺一个就会在打分时炸。"""
    seed = get_seed_config()
    for category in PLACE_CATEGORIES:
        assert category in seed.score_bases, f"{category} 缺少 score_bases"
        assert category in seed.duration_bases, f"{category} 缺少 duration_bases"
        assert category in seed.indoor_by_category, f"{category} 缺少 indoor_by_category"
        assert category in seed.tags_by_category, f"{category} 缺少 tags_by_category"


def test_score_bases_only_cover_known_categories() -> None:
    """反向：seed.yaml 里不得出现枚举之外的类别（写错字会让规则静默失效）。"""
    seed = get_seed_config()
    unknown = set(seed.score_bases) - set(PLACE_CATEGORIES)
    assert not unknown, f"seed.yaml 的 score_bases 含未定义的类别：{sorted(unknown)}"


def test_config_validator_agrees_with_code_constant() -> None:
    """配置校验用的那份清单必须与这里的常量是同一套（单一事实源不能有两个副本）。"""
    seed = get_seed_config()
    assert set(seed.filters.non_stop_categories) == set(NON_STOP_CATEGORIES)
