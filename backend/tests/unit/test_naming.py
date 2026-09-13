"""名称归一化与相似度的单元测试。

这组测试对应 PRD §14.4 的去重用例，并且额外守住了两个**高危边界**：
- "广州塔" 与 "广州塔码头" 绝不能合并（合并会让用户跑到错误的坐标）
- "北京路" 与 "北京路步行街" 必须合并（否则同一地点入库两次，方案里会出现重复站点）
"""

from __future__ import annotations

import pytest

from app.domain.naming import (
    GENERIC_SUFFIXES,
    is_meaningless_name,
    name_variants,
    normalize_name,
    similarity,
    strip_generic_suffix,
)

pytestmark = pytest.mark.unit

MERGE_THRESHOLD = 0.82


# ── normalize_name：无损归一化 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("广州塔（小蛮腰）", "广州塔"),
        ("广州动物园(先烈中路)", "广州动物园"),
        ("陈家祠", "陈家祠"),
        ("  陈家祠  ", "陈家祠"),
        ("上下九·步行街", "上下九步行街"),
        ("ＣＡＦＥ　２１", "cafe21"),
        ("Chen Clan Hall", "chenclanhall"),
        ("永庆坊【西关】", "永庆坊"),
        ("", ""),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_name_is_idempotent() -> None:
    for raw in ("广州塔（小蛮腰）", "  上下九·步行街 ", "ＣＡＦＥ　２１"):
        once = normalize_name(raw)
        assert normalize_name(once) == once


def test_normalize_name_never_strips_generic_suffixes() -> None:
    """精确匹配键必须无损：剥离后缀会制造错误的等价关系。"""
    assert normalize_name("北京路步行街") == "北京路步行街"
    assert normalize_name("广州塔码头") == "广州塔码头"


# ── strip_generic_suffix：仅用于相似度 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("北京路步行街", "北京路"),
        ("白云山风景区", "白云山"),
        ("越秀公园", "越秀"),
        ("陈家祠", "陈家祠"),  # 无后缀时保持不变
        ("公园", "公园"),  # 整名都是后缀时不清空
        ("广州塔码头", "广州塔码头"),  # 码头不是通名后缀（高危通名）
    ],
)
def test_strip_generic_suffix(raw: str, expected: str) -> None:
    assert strip_generic_suffix(raw) == expected


def test_dangerous_suffixes_are_not_in_generic_list() -> None:
    """回归：把"码头/广场/地铁站/店"当成通名后缀会造成不同实体的错误合并。"""
    for dangerous in ("码头", "广场", "地铁站", "店", "大厦", "购物中心"):
        assert dangerous not in GENERIC_SUFFIXES, f"{dangerous} 不应被当作通名后缀"


# ── is_meaningless_name ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "meaningless"),
    [("7-11", True), ("123", True), ("  ", True), ("a", True), ("陈家祠", False), ("P3", False)],
)
def test_is_meaningless_name(raw: str, meaningless: bool) -> None:
    assert is_meaningless_name(raw) is meaningless


# ── name_variants ──────────────────────────────────────────────────────────


def test_name_variants_extracts_bracket_alias() -> None:
    variants = name_variants("广州塔（小蛮腰）")
    assert "广州塔" in variants
    assert "小蛮腰" in variants


def test_name_variants_includes_suffix_stripped_form() -> None:
    assert "北京路" in name_variants("北京路步行街")


def test_name_variants_does_not_invent_external_knowledge() -> None:
    """机械变体不得包含需要外部知识才知道的别名（如 陈家祠→陈氏书院）。"""
    assert "陈氏书院" not in name_variants("陈家祠")


# ── similarity：合并用例（PRD §14.4）────────────────────────────────────────


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("石室圣心大教堂", "圣心大教堂"),  # 一方是另一方的后缀 → 高相似度，可自动合并
        ("广州塔", "广州塔（小蛮腰）"),
        ("北京路", "北京路步行街"),
        ("上下九", "上下九步行街"),
        ("白云山", "白云山风景区"),
        ("广州动物园", "广州动物园（先烈中路）"),
        ("天河城", "天河城"),
    ],
)
def test_similar_names_reach_merge_threshold(left: str, right: str) -> None:
    score = similarity(left, right)
    if normalize_name(left) == normalize_name(right):
        assert score == 1.0
    else:
        assert score >= MERGE_THRESHOLD, f"{left} 与 {right} 的相似度为 {score}，低于合并阈值"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("广州塔", "广州塔码头"),
        ("陈家祠", "陈家祠地铁站"),
        ("陶陶居", "陶陶居第十甫路店"),
        ("花城广场", "花城广场地下空间"),
        ("陈家祠", "陈氏书院"),  # 同名同地但字面不同 → 必须靠别名表，不能靠模糊匹配
        ("沙面", "沙面岛"),  # 同上，走别名表
        ("宝华路", "宝华面店"),
        ("天河城", "天河城百货"),
        ("广州塔", "珠江夜游"),
    ],
)
def test_dangerous_pairs_stay_below_threshold(left: str, right: str) -> None:
    """宁可漏合并（进人工/LLM 队列），也不能把两个不同地点合成一个。"""
    score = similarity(left, right)
    assert score < MERGE_THRESHOLD, f"{left} 与 {right} 的相似度为 {score}，会被错误合并"


def test_similarity_is_symmetric() -> None:
    pairs = [("北京路", "北京路步行街"), ("广州塔", "广州塔码头"), ("陈家祠", "宝华路")]
    for left, right in pairs:
        assert similarity(left, right) == similarity(right, left)


def test_similarity_is_bounded() -> None:
    for left, right in [("", "陈家祠"), ("陈家祠", ""), ("a", "b"), ("广州塔", "广州塔")]:
        assert 0.0 <= similarity(left, right) <= 1.0


def test_identical_names_score_one() -> None:
    assert similarity("永庆坊", "永庆坊（西关）") == 1.0
    assert similarity("陈家祠", "陈家祠") == 1.0
