"""地点名称归一化与相似度（**纯函数，零 IO**）。

用途：
1. 知识库建库时的重复检测（避免同一地点入库两次）
2. 实体匹配（PRD §14）的 Level 3 模糊匹配打分
3. 自然语言修改里的地点名解析（"不要广州塔" → 命中地点实体）

设计取舍（重要）：
- ``normalize_name`` **只做无损归一化**（全半角、括号、分隔符、大小写），
  不剥离"步行街/风景区"这类通名后缀。因为它同时是**精确匹配的键**，
  过度归一化会把不同实体（如"广州塔"与"广州塔码头"）错误合并。
- 通名后缀的剥离只发生在 :func:`similarity` 内部，作为打分的一个分量，
  且必须配合地理围栏（见 PRD §14.2 Level 4）才能触发合并。
- 中文分词/向量检索在本机不可用（无 zhparser / pgvector），
  因此相似度由 rapidfuzz 的多指标加权组合实现，可解释、可测试、零外部依赖。
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

__all__ = [
    "GENERIC_SUFFIXES",
    "is_meaningless_name",
    "name_variants",
    "normalize_name",
    "similarity",
    "strip_generic_suffix",
]

# 通名后缀：与主体名组合时通常**仍指向同一实体**（"白云山" == "白云山风景区"）。
# 仅用于相似度打分，**不用于精确匹配键**。
#
# 刻意不收录的后缀及其原因（都是"加上去就变成另一个实体"的高危通名）：
#   码头    → 广州塔 vs 广州塔码头（不同的登船点）
#   广场    → 花城广场 vs 花城广场地下空间
#   地铁站  → 陈家祠 vs 陈家祠地铁站
#   店      → 陶陶居 vs 陶陶居第十甫路店
#   大厦/购物中心/商业中心 → 常是独立建筑实体
GENERIC_SUFFIXES: tuple[str, ...] = (
    "商业步行街",
    "步行街",
    "旅游度假区",
    "度假区",
    "旅游区",
    "风景区",
    "景区",
    "公园",
    "博物馆",
    "纪念馆",
    "陈列馆",
    "美术馆",
    "艺术馆",
    "文化馆",
    "图书馆",
    "旧址",
    "故居",
    "分馆",
    "分店",
    "旗舰店",
    "总店",
)

# 括号：中英文全半角都要处理
_BRACKET_PATTERNS = (
    re.compile(r"（[^（）]*）"),
    re.compile(r"\([^()]*\)"),
    re.compile(r"【[^【】]*】"),
    re.compile(r"\[[^\[\]]*\]"),
)

# 分隔符与装饰性字符
_SEPARATORS = re.compile(r"[\s\u3000·•・∙‧\-—–_~、,，.。/／|｜]+")

# 只有数字/符号/空白构成的名称没有意义（如 "7-11"、"P3"）
_MEANINGLESS = re.compile(r"^[\d\s\W_]+$")

# 全角转半角后仍需要保留的中文字符范围不需要特殊处理；这里只处理字母数字与常见标点


def normalize_name(name: str) -> str:
    """无损归一化：用于**精确匹配**的规范形式。

    >>> normalize_name("广州塔（小蛮腰）")
    '广州塔'
    >>> normalize_name("陈家祠  ")
    '陈家祠'
    >>> normalize_name("ＣＡＦＥ　２１")
    'cafe21'
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name)
    for pattern in _BRACKET_PATTERNS:
        text = pattern.sub("", text)
    text = _SEPARATORS.sub("", text)
    return text.strip().lower()


def strip_generic_suffix(name: str) -> str:
    """剥离通名后缀，用于相似度打分（不用于精确匹配键）。

    只剥离一次，且要求剥离后仍有内容，避免 "公园" 这种整名都是后缀的情况被清空。

    >>> strip_generic_suffix("北京路步行街")
    '北京路'
    >>> strip_generic_suffix("白云山风景区")
    '白云山'
    >>> strip_generic_suffix("公园")
    '公园'
    """
    normalized = normalize_name(name)
    for suffix in GENERIC_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[: -len(suffix)]
    return normalized


def is_meaningless_name(name: str) -> bool:
    """判断名称是否没有信息量（纯数字/符号）。"""
    if not name or len(name.strip()) < 2:
        return True
    return bool(_MEANINGLESS.match(name.strip()))


def name_variants(name: str) -> set[str]:
    """生成可用于别名表的名称变体。

    注意：这里只生成**由同一名称机械推导出的变体**（如去括号、去后缀），
    不包含需要外部知识才能知道的别名（如"小蛮腰" → 广州塔）。
    后者必须来自人工整理的 curated 数据，禁止程序猜测。
    """
    variants: set[str] = set()
    base = normalize_name(name)
    if base:
        variants.add(base)
    stripped = strip_generic_suffix(name)
    if stripped and stripped != base:
        variants.add(stripped)
    # 括号内内容常常是别名："广州塔（小蛮腰）" → "小蛮腰"
    for match in re.finditer(r"[（(]([^（）()]{2,20})[）)]", name):
        inner = normalize_name(match.group(1))
        if inner and inner != base:
            variants.add(inner)
    variants.discard("")
    return variants


def _prefix_remainder(left: str, right: str) -> str | None:
    """若一方是另一方的严格前缀，返回多出来的那段；否则 None。"""
    if left.startswith(right) and len(left) > len(right):
        return left[len(right) :]
    if right.startswith(left) and len(right) > len(left):
        return right[len(left) :]
    return None


# 名称互为前缀时的两种处理：
#   1) 多出来的部分不是通名后缀 → 几乎肯定是不同实体（广州塔 / 广州塔码头），压到阈值以下
#   2) 多出来的部分**正是**通名后缀 → 几乎肯定是同一实体（北京路 / 北京路步行街），抬到阈值以上
# 这两条是对称的，缺一条就会漏判：纯靠 fuzz 打分时 (1) 会偏高、(2) 会偏低。
_PREFIX_MISMATCH_CAP = 0.60
_PREFIX_SUFFIX_FLOOR = 0.85


def similarity(a: str, b: str) -> float:
    """两个地点名的相似度（0..1），多指标加权。

    权重（与 PRD §14.2 Level 3 一致；其中"拼音"分量暂由"去后缀"分量替代，
    拼音匹配将在 M2 引入 pypinyin 后补上）：
        ratio             0.35   整体编辑距离
        partial_ratio     0.25   一方是另一方子串时给高分
        token_set_ratio   0.20   词序无关
        去后缀后 ratio     0.20   处理 "北京路" vs "北京路步行街"

    安全守卫：互为前缀时按"多出的部分是通名后缀与否"做上/下限修正，
    宁可漏合并（进人工/LLM 队列），也不要把两个不同地点合成一个
    —— 那会让用户按错误的坐标规划路线。
    """
    left, right = normalize_name(a), normalize_name(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    score = (
        0.35 * fuzz.ratio(left, right)
        + 0.25 * fuzz.partial_ratio(left, right)
        + 0.20 * fuzz.token_set_ratio(left, right)
    ) / 100.0

    left_stripped, right_stripped = strip_generic_suffix(a), strip_generic_suffix(b)
    score += 0.20 * (fuzz.ratio(left_stripped, right_stripped) / 100.0)

    remainder = _prefix_remainder(left, right)
    if remainder is not None:
        score = (
            _PREFIX_SUFFIX_FLOOR if remainder in GENERIC_SUFFIXES else min(score, _PREFIX_MISMATCH_CAP)
        )

    return round(min(score, 1.0), 6)
