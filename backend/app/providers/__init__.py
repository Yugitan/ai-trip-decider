"""外部能力抽象层（PRD §13、§20.2）。

设计原则：
- 每个 Provider 由 ``Protocol`` 定义契约，业务层只依赖契约，不感知具体厂商。
- **没有 Key 不是错误，是降级模式**：每个能力都有对应的 null / 离线实现，
  装配时按 ``Settings`` 的 ``*_effective`` 属性选择，失败可自动回退。
- Provider 只负责"取数据"，不负责"决定要不要取"（那是 RetrievalChain 的职责）。

本包允许 import ``httpx``（与 ``app/domain/**`` 的零 IO 铁律相反）——
它存在的意义就是把 IO 关在这一层里。
"""

from __future__ import annotations

from app.providers.base import LatLng, ProviderHealth
from app.providers.registry import Providers, build_providers

__all__ = ["LatLng", "ProviderHealth", "Providers", "build_providers"]
