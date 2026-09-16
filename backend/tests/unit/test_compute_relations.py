"""关系图报告的统计口径（`scripts/compute_relations.py`）。

为什么单独立一个文件：`docs/RELATION_REPORT.json` 是要入库的**证据**，
而它上一版只有 `osrm_pairs` 一个数 —— 一次重算从 163 变成 161，
读者**无法从这个文件里分辨**是"分批边界变了"还是"OSRM 失败了"。
现在报告里有 `osrm_eligible_pairs`（结构性上限）与 `osrm_batches_failed`（真实失败），
这里守住前者那个纯函数的语义。

（真正的跑库与联网部分在集成/手动运行里：`make relations [OSRM=1]`。）
"""

from __future__ import annotations

import uuid

import pytest

from scripts.compute_relations import count_eligible_pairs

pytestmark = pytest.mark.unit

_A = uuid.uuid4()
_B = uuid.uuid4()
_C = uuid.uuid4()


def test_only_same_chunk_pairs_are_eligible() -> None:
    """同一批里的点对才**可能**拿到真实路网距离（table 只算批内矩阵）。"""
    chunk_of = {_A: 0, _B: 0, _C: 1}
    assert count_eligible_pairs([(_A, _B)], chunk_of) == 1
    assert count_eligible_pairs([(_A, _C), (_B, _C)], chunk_of) == 0


def test_mixed_pairs_count_only_the_same_chunk_ones() -> None:
    chunk_of = {_A: 0, _B: 0, _C: 1}
    assert count_eligible_pairs([(_A, _B), (_A, _C), (_B, _C)], chunk_of) == 1
    assert count_eligible_pairs([], chunk_of) == 0


def test_pairs_without_a_chunk_are_never_eligible() -> None:
    """没登记过批次的两个点对不许因为 `.get` 都返回 None 而被算成“同批”。

    这是实现里真实存在过的坑：`chunk_of.get(a) == chunk_of.get(b)` 在两边都缺失时
    是 `None == None` → 真 —— 会把"根本没分过批"的点对算进结构性上限，
    于是报告里的上限是虚高的。
    """
    assert count_eligible_pairs([(_A, _B)], {}) == 0
    assert count_eligible_pairs([(_A, _B)], {_A: 0}) == 0
    # 一边登记过、另一边没登记，同样不算
    assert count_eligible_pairs([(_A, _C)], {_A: 0, _B: 0}) == 0
