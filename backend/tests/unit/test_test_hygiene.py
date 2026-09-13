"""测试自身的卫生检查（"测试的测试"）。

为什么需要这个文件：
    实测发现 `tests/unit/test_logging.py` 漏写了 module-level `pytestmark`，
    于是它的 **17 个测试在 `make test-unit` 里被静默跳过** ——
    全量运行通过、按层筛选也"通过"，但那一层其实没跑。
    这类问题不会报错，只会让你的"测试通过"变成局部假象。

    因此这里对测试目录本身做断言：**每个测试文件必须声明 marker，且与所在目录一致**。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS_DIR = Path(__file__).resolve().parent.parent
MARKER_PATTERN = re.compile(r"^pytestmark\s*=\s*pytest\.mark\.(unit|integration|contract|slow)\b", re.M)

# 目录名 → 该目录下测试必须声明的 marker
ALLOWED_BY_DIRECTORY = {
    "unit": "unit",
    "integration": "integration",
    "contract": "contract",
}


def _test_files() -> list[Path]:
    return sorted(path for path in TESTS_DIR.rglob("test_*.py"))


def test_there_are_test_files() -> None:
    assert len(_test_files()) >= 5, "没找到测试文件，测试发现机制可能坏了"


def test_every_test_file_declares_a_marker() -> None:
    missing = [str(path.relative_to(TESTS_DIR)) for path in _test_files() if not MARKER_PATTERN.search(path.read_text(encoding="utf-8"))]
    assert not missing, (
        "以下测试文件没有声明 module-level `pytestmark`，"
        f"会被 `make test-unit` / `make test-integration` 静默跳过：{missing}"
    )


def test_marker_matches_directory() -> None:
    """tests/unit/ 下的文件必须标 unit，tests/integration/ 下的必须标 integration。

    标错方向的代价很实在：把集成测试标成 unit 会让"快速测试"需要数据库。
    """
    mismatched: list[str] = []
    for path in _test_files():
        relative = path.relative_to(TESTS_DIR)
        if len(relative.parts) < 2:
            continue
        expected = ALLOWED_BY_DIRECTORY.get(relative.parts[0])
        if expected is None:
            continue
        match = MARKER_PATTERN.search(path.read_text(encoding="utf-8"))
        if match is None or match.group(1) != expected:
            mismatched.append(f"{relative} 期望 {expected}，实际 {match.group(1) if match else '无'}")
    assert not mismatched, f"marker 与目录不一致：{mismatched}"


def test_every_test_file_has_a_docstring() -> None:
    """测试文件必须有文档字符串说明它在守什么。

    没有说明的测试在半年后没人敢删，也没人知道它为什么存在。
    """
    missing: list[str] = []
    for path in _test_files():
        text = path.read_text(encoding="utf-8").lstrip()
        if not text.startswith('"""'):
            missing.append(str(path.relative_to(TESTS_DIR)))
    assert not missing, f"以下测试文件缺少文档字符串：{missing}"


def test_no_leftover_comment_markers_in_tests() -> None:
    """测试里不应留 `# TODO` / `# FIXME` 注释标记。

    只匹配**注释形式**的标记（`# TODO`），不匹配字符串里出现的字样 ——
    否则 test_curated_data.py 里那个"站点名不能是 TODO 这类占位符"的检查清单会被误判
    （第一版实现就是这么写错的，守卫先把作者自己抓了出来，算是它工作正常）。
    这里**允许** print：数据库夹具会用 print 报告"正在建库"的进度。
    """
    comment_marker = re.compile(r"#\s*(TODO|FIXME)\b")
    offenders: list[str] = []
    for path in _test_files():
        # 本文件必须在文档里讨论这些标记（否则没法说明这条规则在守什么），
        # 所以它对自身豁免。这是刻意的例外，不是漏检。
        if path.name == Path(__file__).name:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if comment_marker.search(line):
                offenders.append(f"{path.relative_to(TESTS_DIR)}:{lineno}")
    assert not offenders, f"测试里残留 TODO/FIXME 注释：{offenders}"
