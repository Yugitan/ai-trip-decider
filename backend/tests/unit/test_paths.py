"""项目路径定位的单元测试（``app/core/paths.py``）。

为什么值得单独测：
    这个模块决定了"config/ 在哪、原始数据在哪"。它靠"向上找同时含 PRD.md 与 config/
    的目录"来定位根目录 —— 一旦定位错，所有配置与数据都会读到错误的位置，
    而症状会是"某个字段莫名缺失"这种极难排查的表现。

    正常路径由其他测试间接覆盖（能加载到配置就说明定位对了），
    这里专门覆盖**定位失败**时必须 fail-fast 而不是返回错误路径。
"""

from __future__ import annotations

import pytest

from app.core import paths

pytestmark = pytest.mark.unit


def test_project_root_contains_markers() -> None:
    root = paths.project_root()
    assert (root / "PRD.md").exists()
    assert (root / "config").is_dir()


def test_derived_dirs_are_under_project_root() -> None:
    root = paths.project_root()
    assert paths.config_dir() == root / "config"
    assert paths.backend_dir() == root / "backend"
    assert paths.data_dir() == root / "backend" / "data"
    assert paths.raw_data_dir() == root / "backend" / "data" / "raw"
    assert paths.curated_data_dir() == root / "backend" / "data" / "curated"
    assert paths.docs_dir() == root / "docs"
    assert paths.env_file() == root / ".env"


def test_config_dir_actually_has_configs() -> None:
    names = {path.name for path in paths.config_dir().glob("*.yaml")}
    assert {"scoring.yaml", "limits.yaml", "ttl.yaml", "pricing.yaml", "seed.yaml"} <= names


def test_project_root_raises_when_markers_cannot_be_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 定位失败必须抛错，而不是返回一个猜的路径。

    返回猜的路径会让下游"读到不存在的配置文件"，报出的错误是"缺少配置文件：/xxx"，
    排查者会以为是文件被删了，而不是"根目录定位逻辑坏了"。
    """
    monkeypatch.setattr(paths, "_MARKER_FILES", ("definitely-not-a-real-marker.md",))
    paths.project_root.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="无法定位项目根目录"):
            paths.project_root()
    finally:
        paths.project_root.cache_clear()


def test_project_root_raises_when_marker_dir_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """标记目录（config/）不存在时同样必须失败 —— 只匹配文件名是不够的。"""
    monkeypatch.setattr(paths, "_MARKER_DIRS", ("definitely-not-a-real-dir",))
    paths.project_root.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="无法定位项目根目录"):
            paths.project_root()
    finally:
        paths.project_root.cache_clear()


def test_project_root_is_cached() -> None:
    """带 lru_cache：路径定位要遍历父目录，每个请求都算一遍是浪费。"""
    paths.project_root.cache_clear()
    first = paths.project_root()
    second = paths.project_root()
    assert first is second
    assert paths.project_root.cache_info().hits >= 1
