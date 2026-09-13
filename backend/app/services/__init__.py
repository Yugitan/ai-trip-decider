"""服务层：缓存、成本、检索链、限流等**跨领域编排**。

与 ``app/domain/`` 的分工：domain 是纯算法（零 IO），services 负责把它们
与数据库、外部 Provider 组装起来。业务逻辑（评分/校验/组合）不写在这里。
"""

from __future__ import annotations
