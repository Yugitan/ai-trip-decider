# OSM 广州 POI 抓取摘要

- 抓取时间：2026-09-10 07:02:05 UTC
- OSM 数据快照时间（osm3s.timestamp_osm_base）：116992
- bbox（south,west,north,east）：(22.5, 112.9, 23.95, 114.05)
- 耗时：522.7s
- **有效记录总数：10279**

## 分组命中数（原始元素数，包含被判为无效而丢弃的）

| 分组 | 原始元素 |
| --- | ---: |
| A_attractions | 1534 |
| B_nature | 3307 |
| C_food_shopping | 4916 |
| D_public | 569 |
| E_transport | 0 |

## primary_tag 分布（降序）

| primary_tag | 数量 |
| --- | ---: |
| amenity=restaurant | 2076 |
| leisure=park | 1625 |
| amenity=cafe | 1473 |
| natural=peak | 939 |
| natural=water | 629 |
| shop=mall | 518 |
| amenity=marketplace | 444 |
| tourism=attraction | 413 |
| amenity=place_of_worship | 329 |
| tourism=artwork | 260 |
| tourism=museum | 190 |
| amenity=community_centre | 178 |
| amenity=library | 141 |
| historic=memorial | 126 |
| amenity=cinema | 107 |
| historic=building | 102 |
| tourism=viewpoint | 73 |
| tourism=theme_park | 65 |
| leisure=garden | 63 |
| historic=yes | 59 |
| amenity=arts_centre | 55 |
| amenity=theatre | 55 |
| shop=department_store | 53 |
| historic=ruins | 51 |
| amenity=food_court | 40 |
| historic=tomb | 36 |
| historic=monument | 26 |
| leisure=nature_reserve | 24 |
| tourism=gallery | 23 |
| historic=boundary_stone | 20 |
| historic=manor | 13 |
| historic=city_gate | 12 |
| historic=archaeological_site | 9 |
| historic=castle | 7 |
| tourism=zoo | 7 |
| tourism=yes | 6 |
| historic=cannon | 5 |
| historic=wayside_shrine | 5 |
| natural=beach | 5 |
| tourism=aquarium | 4 |
| historic=5 | 2 |
| historic=stone | 2 |
| historic=temple | 2 |
| historic=aircraft | 1 |
| historic=fort | 1 |
| historic=monastery | 1 |
| historic=railway_station | 1 |
| historic=tower | 1 |
| historic=water_well | 1 |
| historic=wharf | 1 |

## 字段覆盖率（**如实报告，不粉饰**）

| 字段 | 非空数量 | 占比 |
| --- | ---: | ---: |
| name | 10279 | 100.0% |
| name_en | 0 | 0.0% |
| opening_hours | 393 | 3.8% |
| addr:district | 450 | 4.4% |
| website | 268 | 2.6% |
| phone | 249 | 2.4% |
| wikidata | 240 | 2.3% |
| wikipedia | 257 | 2.5% |

## 归一化名称重复（≥2 次，共 522 组，最多列 40 组）

| 归一化名称 | 次数 |
| --- | ---: |
| 瑞幸咖啡 | 319 |
| 星巴克 | 287 |
| 喜茶 | 96 |
| 蜜雪冰城 | 87 |
| 奈雪的茶 | 76 |
| 必胜客 | 73 |
| 霸王茶姬 | 72 |
| 萨莉亚 | 50 |
| 茉莉奶白 | 38 |
| 海底捞火锅 | 37 |
| coco都可 | 33 |
| 库迪咖啡 | 32 |
| 茶百道 | 31 |
| 遇见小面 | 30 |
| 木屋烧烤 | 26 |
| 古茗 | 22 |
| 老碗会西安面馆 | 22 |
| 1点点 | 21 |
| mannercoffee | 17 |
| 丘大叔 | 17 |
| 味千拉面 | 16 |
| 寿司郎 | 14 |
| 探鱼烤鱼 | 13 |
| mstand | 12 |
| 点都德 | 12 |
| 沪上阿姨 | 11 |
| 益禾堂 | 11 |
| gaga | 10 |
| 胖哥俩肉蟹煲 | 10 |
| 荷花池 | 10 |
| 费大厨辣椒炒肉 | 10 |
| 中山公园 | 9 |
| 图书馆 | 9 |
| 沙县小吃 | 9 |
| 万达广场 | 8 |
| 太二酸菜鱼 | 8 |
| 官山涌 | 8 |
| 袁记云饺 | 8 |
| 五指山 | 7 |
| 兰州拉面 | 7 |

## 数据缺口（已知，后续丰富化阶段处理）

- `opening_hours` 覆盖率低意味着大量地点的营业时间只能标 `unknown`，
  按 PRD 要求必须写 NULL + 记入 `unknown_fields`，并在 UI 提示「出发前确认官方信息」。
- `price_min/max` 在 OSM 中基本缺失，不得用估算值冒充真实票价。
- `addr:district` 缺失时无法按行政区聚合，但可由坐标反推（不写死猜测值）。
