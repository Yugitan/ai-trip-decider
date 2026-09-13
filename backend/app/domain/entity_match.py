"""实体匹配与去重（**纯函数，零 IO**）。对应 PRD §14。

职责：
    - 将自由文本中的地点名对齐到知识库中的具体地点（如 "广州搭" → 广州塔）。
    - 判断两个地点记录是否为同一实体（去重）。
    - 所有计算基于字符串相似度 + 地理距离，不查询数据库。

依赖说明：
    ``rapidfuzz`` 与 ``pypinyin`` 是纯计算库，不涉及网络/磁盘 IO，
    因此可以在 domain 层使用（与 ``import math`` 同理）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pypinyin import lazy_pinyin
from rapidfuzz import fuzz

from app.domain.geo import haversine_m
from app.domain.models import Place

__all__ = [
    "MatchLevel",
    "MatchResult",
    "MergeVerdict",
    "compute_similarity",
    "match_entity",
    "normalize_name",
    "pinyin_of",
    "should_merge",
]


# ── 名称归一化 ──────────────────────────────────────────────────────────────

# 常见可去除后缀/前缀（PRD §14.1）
_STRIP_SUFFIXES: tuple[str, ...] = (
    "景区",
    "公园",
    "博物馆",
    "纪念馆",
    "旧址",
    "店",
    "分店",
    "旗舰店",
    "百货",
    "步行街",
    "风景区",
    "游览区",
    "旅游区",
    "岛",
)

# 括号内容正则
_PAREN_RE = re.compile(r"[（(].*?[）)]")


def normalize_name(name: str) -> str:
    """名称归一化（PRD §14.1）。

    流程：
        1. NFC 规范化（全角/半角统一）
        2. 去除空格、制表符、换行
        3. 去除括号内容
        4. 去除常见后缀/前缀
        5. 统一标点：·、•、・ → 空
        6. 小写化（英文部分）
    """
    # 1. NFC 规范化（会处理大部分全角/半角问题）
    s = unicodedata.normalize("NFC", name)

    # 2. 去除空白
    s = "".join(s.split())

    # 3. 去除括号内容
    s = _PAREN_RE.sub("", s)

    # 4. 去除常见后缀（从长到短，避免部分匹配）
    for suffix in sorted(_STRIP_SUFFIXES, key=len, reverse=True):
        if s.endswith(suffix) and len(s) > len(suffix) + 1:
            s = s[: -len(suffix)]

    # 5. 统一标点
    s = s.replace("·", "").replace("•", "").replace("・", "")

    # 6. 小写化
    s = s.lower()

    return s


# ── 拼音 ────────────────────────────────────────────────────────────────────


def pinyin_of(name: str) -> str:
    """返回名称的**无声调拼音**字符串（连续，无分隔符）。

    示例：``"广州塔"`` → ``"guangzhouta"``
    """
    return "".join(lazy_pinyin(name))


# ── 相似度计算 ──────────────────────────────────────────────────────────────


def compute_similarity(a: str, b: str) -> float:
    """计算两个名称的综合相似度（0–1）。

    算法（PRD §14.2 Level 3）：
        ratio      = fuzz.ratio(a, b)      × 0.35
        partial    = fuzz.partial_ratio(a, b) × 0.25
        token_set  = fuzz.token_set_ratio(a, b) × 0.20
        pinyin     = fuzz.ratio(pinyin_a, pinyin_b) × 0.20
    """
    norm_a = normalize_name(a)
    norm_b = normalize_name(b)

    if not norm_a or not norm_b:
        return 0.0

    ratio = fuzz.ratio(norm_a, norm_b) / 100.0
    partial = fuzz.partial_ratio(norm_a, norm_b) / 100.0
    token_set = fuzz.token_set_ratio(norm_a, norm_b) / 100.0

    py_a = pinyin_of(norm_a)
    py_b = pinyin_of(norm_b)
    pinyin_sim = fuzz.ratio(py_a, py_b) / 100.0 if py_a and py_b else 0.0

    return ratio * 0.35 + partial * 0.25 + token_set * 0.20 + pinyin_sim * 0.20


# ── 匹配结果 ────────────────────────────────────────────────────────────────


MatchLevel = Literal["exact", "alias", "fuzzy"]


@dataclass(frozen=True, slots=True)
class MatchResult:
    """`match_entity` 的返回结构。"""

    place: Place
    level: MatchLevel
    confidence: float
    matched_name: str = ""


@dataclass(frozen=True, slots=True)
class MergeVerdict:
    """`should_merge` 的返回结构。"""

    same: bool
    confidence: float
    reason: str = ""


# ── 实体匹配 ────────────────────────────────────────────────────────────────


def match_entity(
    query: str,
    candidates: Sequence[Place],
    *,
    alias_index: Mapping[str, Sequence[Place]] | None = None,
    threshold: float = 0.65,
) -> MatchResult | None:
    """在候选集中按多级流水线匹配实体（PRD §14.2 Level 0–3）。

    参数：
        query: 用户输入的原始名称（如 "广州搭"）。
        candidates: 候选地点序列（通常来自知识库）。
        alias_index: 归一化别名 → 地点列表 的索引（用于 Level 1）。
        threshold: 模糊匹配的最低综合分。

    返回：
        最匹配的 ``MatchResult``，或无匹配时 ``None``。
    """
    norm_q = normalize_name(query)

    if not norm_q:
        return None

    # Level 0：精确匹配 display_name / name
    for place in candidates:
        if norm_q == normalize_name(place.name):
            return MatchResult(place, "exact", 1.0, place.name)

    # Level 1：别名表匹配
    if alias_index is not None:
        for key, places in alias_index.items():
            if norm_q == key:
                return MatchResult(places[0], "alias", 1.0, key)

    # 若查询含拉丁字母，视为拼音输入，优先走拼音通道
    is_pinyin_query = bool(re.search(r"[a-zA-Z]", norm_q))

    # Level 2/3：模糊匹配（取最高分的候选）
    best: MatchResult | None = None
    best_score = 0.0

    for place in candidates:
        if is_pinyin_query:
            # 拼音输入：直接比较候选地点的拼音
            py_place = pinyin_of(place.name)
            score = fuzz.ratio(norm_q, py_place) / 100.0
        else:
            score = compute_similarity(query, place.name)

        # 若别名索引未命中，也尝试与别名索引的键匹配（兜底）
        if alias_index is not None:
            for key in alias_index:
                if is_pinyin_query:
                    alias_score = fuzz.ratio(norm_q, pinyin_of(key)) / 100.0
                else:
                    alias_score = compute_similarity(query, key)
                if alias_score > score:
                    score = alias_score

        if score >= threshold and score > best_score:
            best_score = score
            best = MatchResult(place, "fuzzy", round(score, 4), place.name)

    return best


# ── 合并判定 ────────────────────────────────────────────────────────────────


def should_merge(
    a: Place,
    b: Place,
    *,
    threshold: float = 0.82,
    max_distance_m: float = 800.0,
) -> MergeVerdict:
    """判断两个地点是否为同一实体（PRD §14.2 Level 3–4）。

    规则：
        - 相似度 ≥ threshold 且 距离 ≤ max_distance_m → 同一实体
        - 相似度 ≥ threshold 且 距离 > max_distance_m → 同名不同地（不合并）
        - 相似度 < threshold → 不合并
    """
    if a.id == b.id:
        return MergeVerdict(same=True, confidence=1.0, reason="同一 ID")

    sim = compute_similarity(a.name, b.name)
    distance = haversine_m(a.lat, a.lng, b.lat, b.lng)

    if sim >= threshold:
        if distance <= max_distance_m:
            return MergeVerdict(
                same=True,
                confidence=round(sim, 4),
                reason=f"相似度 {sim:.2f} 且距离 {distance:.0f}m ≤ {max_distance_m}m",
            )
        return MergeVerdict(
            same=False,
            confidence=round(sim, 4),
            reason=f"同名不同地：距离 {distance:.0f}m > {max_distance_m}m",
        )

    return MergeVerdict(
        same=False,
        confidence=round(sim, 4),
        reason=f"相似度 {sim:.2f} < {threshold}",
    )
