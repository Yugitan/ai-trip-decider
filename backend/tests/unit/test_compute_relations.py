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

from scripts.compute_relations import cluster_batches, count_eligible_pairs

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


# ── 分批：按候选关系图聚类，而不是按 UUID 切块 ──────────────────────────────
#
# 为什么值得专门守：分批直接决定"这对地点能不能拿到真实路网距离"。
# 实测按 UUID 切块时 5445 对里只有 130 对同批（2.4%），于是结果页上
# 几乎每一段通勤都是 estimated —— 用户看到的"通勤耗时不真实"就是这个。


def _ring(n: int) -> tuple[list[uuid.UUID], list[tuple[uuid.UUID, uuid.UUID]]]:
    """n 个点围成一圈，每个点只与左右相邻的两点配成候选对。"""
    nodes = [uuid.uuid4() for _ in range(n)]
    pairs = [(nodes[i], nodes[(i + 1) % n]) for i in range(n)]
    return nodes, pairs


def test_batching_keeps_neighbours_together() -> None:
    """一圈相邻点的比对：聚类能把 ≥90% 的比对放进同批，按 UUID 切块则差一个量级。

    60 个点围成环、每点只与左右相邻者配对，分 3 批就会有 3 条环边被批次边界切断
    （57/60）—— 这是分批的固有代价，不是实现缺陷；关键是别把"能同批的"也切掉。
    """
    nodes, pairs = _ring(60)
    clustered = count_eligible_pairs(pairs, cluster_batches(nodes, pairs, 25))
    # 旧实现：按 UUID 切连续块。UUID 与"哪两个点挨着"毫无关系，同批纯属巧合。
    by_uuid = {node: i // 25 for i, node in enumerate(sorted(nodes, key=str))}
    baseline = count_eligible_pairs(pairs, by_uuid)
    assert clustered >= 0.9 * len(pairs), clustered
    assert clustered > baseline, (clustered, baseline)


def test_batching_respects_the_batch_size_limit() -> None:
    """batch_size 是 OSRM table 的硬上限，超一批就会被服务端拒掉。"""
    nodes, pairs = _ring(60)
    chunk_of = cluster_batches(nodes, pairs, 25)
    assert set(chunk_of) == set(nodes), "每个点都必须分到批，漏一个就静默退回估算"
    sizes: dict[int, int] = {}
    for index in chunk_of.values():
        sizes[index] = sizes.get(index, 0) + 1
    assert max(sizes.values()) <= 25
    assert sum(sizes.values()) == len(nodes)


def test_batching_fills_the_last_batch_geographically() -> None:
    """孤立点（没有任何候选对）不能占不满批：空槽位不产生任何覆盖，白花一次请求。"""
    lonely = [uuid.uuid4() for _ in range(30)]  # 地理序就是传入顺序
    chunk_of = cluster_batches(lonely, [], 25)
    assert len(set(chunk_of.values())) == 2
    first = [node for node in lonely if chunk_of[node] == 0]
    assert first == lonely[:25], "补位要按传入的地理序，否则批次会跨城"


def test_batching_is_deterministic() -> None:
    """同一份输入必须得到同一批划分，否则报告里的 coverage 又变成一个会自己漂的数。"""
    nodes, pairs = _ring(50)
    assert cluster_batches(nodes, pairs, 25) == cluster_batches(nodes, pairs, 25)


def test_batching_beats_uuid_order_on_a_knn_like_graph() -> None:
    """在一张"每个点连最近几个点"的图上，聚类覆盖率必须显著高于按 UUID 切块。

    这是这次修改的**目的本身**：不改接口、不改 batch_size，只改怎么分批。
    """
    nodes, pairs = _ring(40)
    pairs = pairs + [(nodes[(i + 2) % 40], nodes[i]) for i in range(40)]
    assert count_eligible_pairs(pairs, cluster_batches(nodes, pairs, 25)) >= 70


def test_batching_rejects_a_degenerate_batch_size() -> None:
    with pytest.raises(ValueError):
        cluster_batches([_A, _B], [(_A, _B)], 1)


def test_batching_ignores_pairs_with_unknown_ends() -> None:
    """候选对里出现了不在地点集合里的 id（超了 max_places）时不能 KeyError。"""
    chunk_of = cluster_batches([_A, _B], [(_A, _B), (_A, _C)], 25)
    assert set(chunk_of) == {_A, _B}
