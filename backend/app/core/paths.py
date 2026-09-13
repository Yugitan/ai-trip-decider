"""项目路径定位。

约定：项目根目录是同时包含 ``PRD.md`` 与 ``config/`` 的最近祖先目录。
所有脚本与配置加载都基于它，因此从任意工作目录运行都能正确定位资源。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_MARKER_FILES = ("PRD.md",)
_MARKER_DIRS = ("config",)


@lru_cache(maxsize=1)
def project_root() -> Path:
    """定位项目根目录。"""
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if all((candidate / name).exists() for name in _MARKER_FILES) and all(
            (candidate / name).is_dir() for name in _MARKER_DIRS
        ):
            return candidate
    raise RuntimeError(
        "无法定位项目根目录：未找到同时包含 PRD.md 与 config/ 的目录。"
        f"（从 {here} 向上查找失败）"
    )


def config_dir() -> Path:
    return project_root() / "config"


def backend_dir() -> Path:
    return project_root() / "backend"


def data_dir() -> Path:
    return backend_dir() / "data"


def raw_data_dir() -> Path:
    return data_dir() / "raw"


def curated_data_dir() -> Path:
    return data_dir() / "curated"


def docs_dir() -> Path:
    return project_root() / "docs"


def env_file() -> Path:
    return project_root() / ".env"
