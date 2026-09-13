"""pytest 共享夹具。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _fresh_config_cache() -> Iterator[None]:
    """每个测试都从干净的配置缓存开始，避免 monkeypatch 污染后续用例。"""
    from app.core.config import clear_config_cache

    clear_config_cache()
    yield
    clear_config_cache()
