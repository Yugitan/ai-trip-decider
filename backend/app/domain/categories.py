"""地点类别的单一事实源（**纯常量模块，零依赖**）。

为什么单独一个模块：``places.category`` 的取值同时被三处使用——
1. 数据库 CHECK 约束（app/db/models.py）
2. 丰富化规则的合法性校验（app/core/config.py）
3. 评分与候选生成（app/domain/*）

如果每处各写一份清单，迟早会出现"配置里写了一个数据库不接受的类别"这类
只在运行时才炸的问题。这里作为唯一来源，其他地方一律 import。
"""

from __future__ import annotations

from typing import Final

# 注意：这里的每一项都必须在 config/seed.yaml 里能落到具体的推导规则上
# （要么由 tag_to_category 派生，要么由 category_overrides 覆盖得到）。
PLACE_CATEGORIES: Final[tuple[str, ...]] = (
    "attraction",     # 景点
    "district",       # 街区 / 商圈（OSM 无对应主标签，由名称规则覆盖得到）
    "food",           # 餐饮
    "cafe",           # 咖啡 / 茶饮
    "museum",         # 博物馆 / 美术馆 / 展览
    "historic",       # 历史建筑 / 文物 / 宗教建筑
    "citywalk",       # 适合步行漫游的路线段
    "photo",          # 拍照机位
    "nightview",      # 夜景 / 观景
    "family",         # 亲子
    "shopping",       # 购物
    "nature",         # 自然 / 公园 / 山林
    "transport_hub",  # 交通枢纽（只作为路线锚点，不作为游玩站点）
)

# 只作为路线起终点锚点、不参与游玩的类别（与 config/seed.yaml 的
# filters.non_stop_categories 保持一致，由配置校验与测试共同保证）
NON_STOP_CATEGORIES: Final[frozenset[str]] = frozenset({"transport_hub"})

# 面向用户的中文名。放在这里而不是散落在前端，是为了让 API 与 UI 用同一套叫法
# （否则同一个类别在接口里叫 food、在界面上叫「美食」、在报告里叫「餐饮」）。
CATEGORY_LABELS: Final[dict[str, str]] = {
    "attraction": "景点",
    "district": "街区商圈",
    "food": "餐饮",
    "cafe": "咖啡茶饮",
    "museum": "博物馆展览",
    "historic": "历史人文",
    "citywalk": "适合漫游",
    "photo": "拍照机位",
    "nightview": "夜景观景",
    "family": "亲子",
    "shopping": "购物",
    "nature": "自然公园",
    "transport_hub": "交通枢纽",
}
