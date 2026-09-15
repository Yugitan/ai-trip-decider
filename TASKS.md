# TASKS.md — 任务清单与完成状态

> 最后更新：2026-09-15
> 当前进度：**M0–M5 全部完成**（M0 脚手架 + M1 广州知识库 + M2 领域内核 +
> M3 Provider 与检索链 + M4 规划编排与 API + **M5 前端结果页**：首页提交 → 三套方案时间线 /
> 逐项对比表 → `/trip/{id}` 独立结果页 → 改路线 / 撤销 / 分享 / 分享页「复制这套路线」 /
> 地图（需配 `NEXT_PUBLIC_AMAP_JS_KEY`）→ **分享页 OG 图 + JSON-LD（AC-9.5）**）。
> 下一步：M6（数据缺口，等 Overpass）/ M7（E2E + 性能）/ M8（交付报告）。

状态图例：✅ 完成并已验证 · 🟡 部分完成 · ⏳ 未开始 · ⛔ 被外部条件阻塞

---

## M0 脚手架

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M0-1 | 仓库初始化（git / .gitignore / README / Makefile / .env.example） | ✅ | `make help` 输出 30 个目标；`git status` 干净可提交 |
| M0-2 | `config/` 五份配置（scoring / limits / ttl / pricing / seed） | ✅ | 全部通过 Pydantic 强校验；权重和非 1.0 会 fail-fast（有测试） |
| M0-3 | 后端骨架（Python 3.12 + FastAPI + 核心层 + `/health`） | ✅ | `/api/v1/health` 返回 200 且如实报告降级模式 |
| M0-4 | 数据库迁移（25 张表 + pg_trgm + 索引） | ✅ | `alembic upgrade head` → `alembic check` 无差异；`downgrade base` 可逆 |
| M0-5 | 前端骨架（Next 15 + TS + Tailwind v4 + 设计系统 + 首页） | ✅ | `pnpm typecheck / lint / test / build` 四条全绿；`/` 与 `/about/data` 静态预渲染 |
| M0-6 | 测试骨架与 `make check` | ✅ | 593 个后端测试（487 unit + 106 integration）+ 93 个前端测试通过；后端覆盖率 100%；mypy strict 49 文件 0 错；ruff 0 问题；`make check` 约 35 秒 |

### M0 期间发现并修复的问题（详见 §"问题记录"）

1. YAML 里裸写的 `null:` 被解析成 `None` 键，导致定价配置校验失败
2. `LoggerAdapter` 吞掉调用方 `extra`，结构化日志的 `event`/`context` 全部丢失
3. 未知路径 404 返回纯文本，绕过了统一错误 envelope
4. `error::DeprecationWarning` 把第三方依赖的弃用告警变成错误，整个测试套件无法收集
5. 脱敏正则漏掉 `Authorization: Bearer <token>`（分隔符是空格）

---

## M1 广州知识库

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M1-1 | OSM 真实数据采集脚本 + 原始数据 | ✅ | `scripts/fetch_osm_guangzhou.py` 抓得 **10 279** 条真实 POI（3.8MB），带 OSM permalink |
| M1-13 | 城市行政边界抓取 + 点面判定 | ✅ | `scripts/fetch_city_boundary.py`（relation 3287346，5059 点）+ `geo.point_in_rings`（纯函数）；剔除约 2 000 条邻市地点 |
| M1-2 | 丰富化纯逻辑（类别/分值/时长/未知字段/可信度） | ✅ | `app/domain/enrichment.py`，零 IO，规则全部来自 `config/seed.yaml` |
| M1-3 | 名称归一化与相似度（实体匹配基础） | ✅ | `app/domain/naming.py`，11 组去重向量全部符合预期（含"广州塔 vs 广州塔码头"不合并） |
| M1-4 | 建库流水线（幂等 + 可解释的丢弃原因） | ✅ | 10 279 → **1 622** 地点（剔除邻市后）；每种丢弃原因都计数并写入报告 |
| M1-5 | 人工整理数据（63 个地点覆盖 + 44 条路线模板） | ✅ | 59/63 匹配成功；44/44 路线入库 |
| M1-6 | 路线站点解析（含别名回退） | ✅ | 44 条路线全部解析到 ≥3 个站点 |
| M1-7 | 质检脚本（门槛 → 可执行断言） | ✅ | `scripts/validate_seed.py`：**0 阻断项失败**，4 项告警如实记录 |
| M1-8 | 数据质量报告 | ✅ | `docs/DATA_REPORT.md`（107 行，含每个过滤步骤的原因与数量） |
| M1-9 | 地点关系图 | 🟡 | `docs/RELATION_REPORT.json`：5 116 对关系，其中 163 对用真实路网距离、其余为估算（逐条标注） |
| M1-10 | 知识库集成测试 | ✅ | 15 个专项断言（含"关键地标必须在库"回归测试） |
| M1-11 | 人工数据单元校验（不需数据库） | ✅ | 21 个断言：非法分值维度/类别、<3 站伪路线、slug 重复、时间区间反了、占位符站点名 |
| M1-12 | 测试自身的卫生守卫 | ✅ | 强制每个测试文件声明 marker 且与目录一致 —— 修掉了 `test_logging.py` 漏 marker 导致 **17 个测试被 `make test-unit` 静默跳过**的问题 |

### M1 期间发现并修复的问题

6. **Overpass 用 `200 + remark` 返回查询错误**，脚本把"查询失败"当成"该类别没有数据"
7. **`overpass.osm.ch` 镜像没有中国数据**，返回 200 + 空结果 → 加了"镜像数据覆盖探针"
8. 相关性过滤**误杀真实地标**：广州塔（`tourism=artwork`）、白云山（`natural=peak`）、
   南海神庙（`historic=city_gate`）、荔湾湖公园（无信号的 `leisure=park`）
9. `historic=*` 取值发散导致**未知类别** → 引入前缀兜底规则
10. **`photo` 类别没有任何产生者** → 纠正为"拍照是属性不是类型"，改由 `photo_score` 承载
11. **规范名唯一索引**把真实的同名地点挡在库外（广州有 9 个"中山公园"）
12. `kb_version` 用日期命名导致同一天重跑撞唯一约束，且会让 `plan_cache` 失效逻辑错乱
13. 路线解析**只查规范名不查别名**，导致 44 条路线里 23 条被跳过（地点其实都在库里）
14. 集成测试跑在**空的测试库**上全部 skip（假绿）→ 改为自动灌入真实知识库
15. **跨事件循环的引擎缓存**导致 TestClient 里 `/health` 抛 `Event loop is closed`
16. OSRM 关系统计口径错误，打印出负数的"失败对数"
17. 人工路线数据 14 处错误（12 条只有 2 站、2 条 `route_type` 写错）由 Schema 校验拦下
18. **`tests/unit/test_logging.py` 漏写 module-level marker**，17 个测试在 `make test-unit` 里被静默跳过（全量运行正常，按层筛选才暴露）→ 补 marker 并加"测试的测试"守卫
19. **★ 矩形 bbox 切进邻市（本轮最严重的数据 bug）★**：整城抓取用矩形 bbox（约 18 800 km²），
    而广州实际行政面积只有约 7 434 km² —— 超过一半的矩形是东莞/深圳/佛山/中山/惠州/清远。
    结果约 **2 000 条邻市地点**混进广州知识库，「深圳野生动物园」「锦绣中华民俗村」
    还因为信号多而排到热度榜最前面。修法分三步：地址标签过滤（369 条）→ 名称前缀过滤
    （不能含裸「中山」，否则会误删中山纪念堂/国立中山大学旧址）→ **抓取行政边界做点面判定**
    （OSM relation 3287346，5059 点）。入库地点因此从 3 776 降到 **1 622**，
    被剔除的全是其他城市的地点。已加边界回归测试与「邻市地点必须不存在」测试
20. **集成测试跑在过期数据上**：fixture 只在「地点少于 200」时才建库，于是数据规则改动后
    测试继续跑在旧数据上并通过（边界 bug 修完后，测试库仍留着 2000 条邻市地点而全绿）
    → 改为**每次重建测试库**
21. `district` 字段语义错误：把 `addr:city` 也当成 district 的候选，
    导致「越秀区」与「广州市」混在同一字段 → 只接受 `addr:district`
22. `citywalk` 类别没有产生者（唯一一条来自本就在邻市的地点）→ 与 `photo` 同理，
    「适合 CityWalk」是属性（`walkability_score`）而非类型，取消该类别的数量门槛
23. `app/schemas/curated.py` 覆盖率 0%：人工数据的校验只在建库时才跑，单测里没人碰 → 新增 21 个针对真实 YAML 的单元断言，总覆盖率 80% → 91%

---

## 代码审查与测试强化（2026-09-10 第二轮）

> 范围：审查 M0 + M1 已完成的代码（含前端），修复缺陷并补齐测试。
> 结果：后端测试 **212 → 593**，前端测试 **23 → 93**，后端覆盖率 **90%（失真）→ 100%**。

### 修复的缺陷

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| R1 | **覆盖率配置缺 `concurrency`，async 端点覆盖率被系统性低估** —— SQLAlchemy async 引擎内部用 greenlet 切换上下文，coverage 默认只跟踪 thread，导致所有 `async def` 端点**只统计到第一个 `await` 之前**。实测 `api/v1/catalog.py` 被报成 54%，实际 98%；全仓总覆盖率被低估约 5 个点 | 🔴 高 | `pyproject.toml` 加 `concurrency = ["thread", "greenlet"]`；并在 `make test-cov` 上加 `--cov-fail-under=93` 闸门 |
| R2 | **未捕获异常时 `request_id` 丢失** —— 中间件在 except 分支里 `set_request_id(None)` 后才重新抛出，最外层异常处理器读到的是 None，于是响应体 `meta.request_id` 为 null、响应头也没有 `X-Request-Id`。**但错误提示偏偏写着"请把响应头里的 X-Request-Id 反馈给我们"** | 🔴 高 | 中间件异常分支不再清空 request_id；`_unhandled` 处理器补上 `X-Request-Id` |
| R3 | **5xx 响应缺少安全头与 CORS 头** —— 未捕获异常走最外层的 `ServerErrorMiddleware`，绕过全部自定义中间件。前端在浏览器里读到的是"跨域被拦"而不是我们写的错误提示 | 🟡 中 | `_unhandled` 手动补 `_SECURITY_HEADERS` + 按白名单回显 Origin；白名单抽成 `allowed_origins()` 供两处共用 |
| R4 | **`assert_safe_for_production` 里有一条永远不触发的检查** —— 判的是 `admin_token == ""`，而 `_blank_to_none` 已把空串归一化成 `None`，于是"后台无保护"永远不会被拦下 | 🟡 中 | 改为 `not self.admin_token`（空串与未设置都拦） |
| R5 | **`travel_minutes` 取整少算 1 分钟** —— `int(minutes + 0.999)` 在分钟小数部分落在 (0, 0.001] 时返回 floor 而非 ceil | 🟢 低 | 改为「按 1e-6 规整后 `ceil`」，同时避免浮点噪声（如 18.000000000000004）凭空多算一分钟 |
| R6 | `point_in_polygon` 的 docstring 声称"边界上的点视为在内"，实际只保证**顶点命中**，边中点不保证 | 🟢 低（文档） | docstring 写清确切语义并说明为何不加 epsilon；新增测试钉住实际行为 |
| R7 | `enrich_or_reason` 的 docstring 过滤顺序与代码不一致（坐标范围实际在名称黑名单**之前**） | 🟢 低（文档） | docstring 改成与代码逐条对应的编号列表 |
| R8 | `main.py` 里"add_middleware 后加的更外层，因此 CORS 在内"的注释与事实相反（实测 CORS 在外层） | 🟢 低（文档） | 更正注释并说明为何刻意让 CORS 在外层 |
| R9 | `geo.py` 的 `bbox_contains` 与 `DEFAULT_ROUTE_FACTOR` 无任何调用方 | 🟢 低（死代码） | 保留（属对外导出，M2 可能用），补测试并加注释标注"当前无调用方" |
| R10 | **前端 `engineNote` 把「接口没实现」与「输入被拒绝」混为一谈** —— 400/422 被归进"引擎开发中"，还附一句"需求已被前端完整校验"；但 422 恰恰意味着后端认为需求不合法，这句话会把用户引向"等引擎上线"而不是"改输入" | 🟡 中（诚实性） | 拆成两组状态码：404/405/501 → 引擎开发中；400/422 → 需求不合法；其余 → 中性说明 |
| R11 | 前端 `formatDuration` 用 `if (!minutes)` 判断，把 `0`、`NaN`、`null` 混为一谈（同文件的 `formatDistance` 用的是显式 null 判断，两者不一致） | 🟢 低 | 显式判断 `null / 非有限数 / <= 0`；并把它与 `formatDistance`、枚举翻译一起从 async 服务端组件**提取到 `lib/format.ts`** 使其可单测 |
| R12 | **`/cities` 是 1 + 3N 的 N+1 查询** —— 每个城市各查 3 次（地点数/路线数/最新版本），城市数从 1 涨到 20 时查询数从 4 涨到 61 | 🟡 中（性能） | 改为 3 条批量聚合（`GROUP BY city_id` × 2 + PostgreSQL `DISTINCT ON` 取每城最新版本）。**配查询计数测试**：临时插入一个城市，用 `before_cursor_execute` 数 SQL 条数，断言不随城市数增长 —— 因为 N+1 与批量聚合的响应体完全一样，基于响应的断言抓不到回归 |
| R13 | 前端 `PlaceCard` 直接把 `sources[0].url` 放进 `<a href>`，未校验协议（`javascript:` / `data:` 点击即执行） | 🟢 低（防御纵深） | 新增 `safeExternalUrl()`（仅放行 http/https）；非法协议**降级为纯文本但保留来源署名**（来源可追溯是硬性要求） |
| R14 | `app/domain/categories.py` 自称"单一事实源"，被三处消费却没有任何一致性测试 | 🟢 低 | 新增 `tests/unit/test_categories.py`（14 例）。最有价值的一条是**"每个类别都必须有产生者"** —— `photo` 与 `citywalk` 都曾因漏配规则而永远是空的；`citywalk` 至今是刻意例外，用 `INTENTIONALLY_UNPRODUCED` 记录原因并禁止悄悄扩容 |
| R15 | `app/explore/[city]/page.tsx`（async 服务端组件）完全靠人工点开验证 | 🟢 低 | 新增 18 个测试。它本质是 `async (props) => JSX` 的纯函数，`await ExplorePage({...})` 后直接渲染即可，无需 Next.js 运行时 |

### 新增测试

| 文件 | 数量 | 覆盖内容 |
| --- | --- | --- |
| `tests/unit/test_geo.py` | 62 | **此前该模块零专用测试**。射线法内部/外部/凹多边形、顶点命中与边中点的语义差异、洞（飞地）、多外环并集、闭合环、winding 方向、haversine 基准值、`travel_minutes` 取整契约、`route_factor` 阈值、`CityBoundary` 校验与反序列化 |
| `tests/unit/test_enrichment.py` | 132 | **此前该模块零专用测试**。全部 11 条 `DropReason` 丢弃路径、边界优先于保护名单、`中山` 前缀不误伤、连锁品牌过滤、类别覆盖与前缀兜底、信号 AND 语义与 `when_category_in`、`set_min`/`set_max`（用合成配置覆盖当前未用到的分支）、分值裁剪、`unknown_fields`、字段级溯源 `field_scope`、质量标记、别名生成 |
| `tests/unit/test_config_validators.py` | 128 | `config.py` 全部 fail-fast 分支（权重范围/求和、公式一致性、archetype 交叉校验、出行方式白名单、定价未校准的嵌套与列表探测、seed 规则自洽性、admin 危险前缀）+ `schemas/curated.py` 全部非法输入分支 |
| `tests/unit/test_paths.py` | 6 | 项目根目录定位失败时必须抛错而不是返回猜测路径；派生目录正确性；缓存生效 |
| `tests/integration/test_error_handling.py` | 36 | 中间件与安全响应头、CORS 对错误响应可见、未捕获异常的统一 envelope 与不泄露堆栈、404/405 的路径语义错误码、校验错误只回显字段摘要、health 的数据库不可用/迁移未跑/ready 503 三条故障路径、目录 API 的空结果与分页边界 |
| `frontend/lib/__tests__/api.test.ts` | 23 | **前端唯一网络出口，此前零覆盖**（组件测试全都 `vi.mock` 掉了它）。envelope 拆包、四类错误映射（网络中断/非 JSON/`ok:false`/`data:null`）、requestId 的优先级与回退、URL 构造与编码、`with_stops` 默认值 |
| `frontend/lib/__tests__/format.test.ts` | 17 | 格式化边界值：`null`/`0`/负数/`NaN`、1000 米单位切换点、未知枚举原样返回 |
| `frontend/components/__tests__/planner-form-submit.test.tsx` | +6 | 区分「接口未实现」与「输入被拒绝」的回归（R10） |
| `tests/unit/test_categories.py` | 14 | 类别"单一事实源"的一致性：每个类别都要有产生者、有中文名、有分值/时长/标签基线；类别名不得漏入偏好维度的叫法 |
| `tests/integration/test_catalog_api.py` | +3 | **查询次数（N+1 回归）**、聚合默认值为 0、批量化后统计值不变 |
| `frontend/app/explore/[city]/__tests__/page.test.tsx` | 18 | 服务端组件渲染：诚实性标注、后端不可用两条路径、空结果、分页提示、搜索回显、`aria-current`、来源链接协议降级 |

### 覆盖率变化

| 模块 | 修复前 | 修复后 |
| --- | --- | --- |
| `app/api/v1/catalog.py` | 54%（失真） | 100% |
| `app/domain/geo.py` | 92% | 100% |
| `app/domain/enrichment.py` | 94% | 100% |
| `app/core/config.py` | 93% | 100% |
| `app/main.py` | 86% | 100% |
| `app/schemas/curated.py` | 88% | 100% |
| **TOTAL** | **90%（失真，真实约 95%）** | **100%** |

---

## 阻塞与缺口（如实记录）

| # | 事项 | 状态 | 原因与影响 |
| --- | --- | --- | --- |
| B1 | `place=*` 类目（街区/岛屿/村落）未抓取 | ⛔ | 脚本已写好，运行时 Overpass 两个镜像均不可用（探针拒绝返回空数据）。影响：`district`=8、`citywalk`=1 偏低；东山口/二沙岛/永庆坊/小洲村 未入库 |
| B2 | `transport_hub`（地铁站/公交/轮渡）未抓取 | ⛔ | 整块 bbox 查询 HTTP 504，分片重试仍失败。影响：暂无"从哪开始"的起点建议 |
| B3 | `wikidata.org` / `zh.wikipedia.org` 不可达 | ⛔ | 无法用它们提升"身份可交叉核验"占比（当前 5.3%，长期目标 30%） |
| B4 | 真实路网关系覆盖仅约 3% | ⛔ | 公共 OSRM 的 table 服务批量上限所致；需自建 OSRM 或商业服务 |
| B5 | 路线预算留空 | 🟡 | 没有可靠的餐饮/门票价格来源，**刻意不填估算值**（有测试守住） |
| B6 | 营业时间结构化解析 | ⏳ | `opening_hours_raw` 原文已存，`opening_hours` 列留 NULL 并记入 `unknown_fields`；解析器属 M2 |
| B7 | 第三方 Key 的真实调用验证 | 🟡 部分解除 | **DeepSeek**：M4 已实跑（11 次真实调用，见 `docs/COST_REPORT.md`）。**Tavily**：2026-09-14 已配 Key 并活体验证（`tests/integration/test_search_live.py`，3 条）。**高德**：拿到的是 **Web端(JS API)** 类型的 Key（实测调 Web服务接口返回 `USERKEY_PLAT_NOMATCH`），已在真实浏览器里验证地图渲染（`RUNNING.md` §8.7）；**后端路由仍缺 Web服务类型的 Key**，`map_provider_effective` 依旧是 `osrm` |
| ~~B8~~ | ~~`pricing.yaml` 的 DeepSeek 单价仍是 `null`~~ | ✅ 已解决（2026-09-13） | 单价已按官方价目页回填 `deepseek-flash` / `deepseek-v4-pro` 并写 `calibrated_at: "2026-09-13"`（按**高峰价**填，低峰为半价），`meta.llm.cost_calibrated` 因此由 `false` 变 `true`（M4-16），本机 11 次真实调用已产出金额（`docs/COST_REPORT.md`）。**仍未校准的是 `map.amap` / `search.serper` / `search.bing` / `llm.openai` / `llm.anthropic`** —— 「未校准单价金额记 0 且标记不可信」这条路径仍由它们守着（有测试） |
| ~~B9~~ | ~~前端覆盖率提供商装不上~~ | ✅ 已解决 | `pnpm add -D @vitest/coverage-v8@3.2.4` 装上了；`make test-frontend-cov` 现在真出报告（98.56%）并带闸门，已接进 `make check` |

## 环境适配（非代码问题，需知悉）

| 项 | 情况 | 处理 |
| --- | --- | --- |
| npm 官方源不可达 | `registry.npmjs.org` 连接超时（`overpass-api.de`、`open-meteo`、OSRM 均正常） | 已配 `.npmrc` 指向 `registry.npmmirror.com`（含原因注释） |
| Docker daemon 未运行 | 无法 `docker compose up` | 开发用本机 Postgres 16；`docker-compose.yml` 保留给 CI |
| 系统 `python3` 是 3.9 | 不满足现代 FastAPI/SQLAlchemy 要求 | 用 uv 固定 Python 3.12.13 |
| `pnpm-workspace.yaml` 占位值 | pnpm 生成的 `allowBuilds: esbuild: set this to true or false` 会让**所有** `pnpm <script>` 直接退出 1 | 改为 `esbuild: false` |

---

## M2 领域内核

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M2-1 | 领域数据结构 `models.py`（frozen dataclass + 纯函数工具） | ✅ | 15 个值对象/函数，全部零 IO；`route_metrics` 已覆盖空序列与正常序列 |
| M2-2 | 通勤估算 `transit.py` + 预算 `budget.py` | ✅ | 速度假设全部来自 `config/limits.yaml`；`source=estimated` 诚实标记贯穿 |
| M2-3 | 评分 `scoring.py`（7 维权重 + 3 乘数） | ✅ | 权重和 1.0 fail-fast；`ScoreBreakdown` 细项可追踪；覆盖率 ≥ 95% |
| M2-4 | 可行性校验 `feasibility.py`（18 项校验码） | ✅ | 硬约束/软约束分层；`WALKING_OVER_LIMIT` 与 `BUDGET_OVER_LIMIT` 可开关；射线法跨江判定 |
| M2-5 | 候选生成 `candidates.py` | ✅ | 硬过滤 → 偏好打分 → 截断 → 锚点；不足 8 个时放宽并记录原因 |
| M2-6 | 路线组合 `planner.py`（束搜索） | ✅ | 多锚点扩展 + 局部硬约束剪枝 + Jaccard 去重 + 完整评分；`leg_to_next` 不可变更新已修复 |
| M2-7 | 意图解析规则引擎 `intent.py` | ✅ | 9 类中文模式全覆盖；否定/双重否定；数字解析（预算/人数/天数/时间）；30 条语料成功率 ≥ 70%；注入检测；43 个单元测试 |
| M2-8 | 实体匹配 `entity_match.py` | ✅ | `normalize_name`（全角/括号/后缀/标点）；`pinyin_of`；rapidfuzz 组合分 + 拼音通道；多级匹配流水线；`should_merge` 含地理围栏；33 个单元测试 |
| M2-9 | 跑通 `make check`（lint / typecheck / coverage gate） | ✅ | ruff 0 问题；mypy strict 61 文件 0 错；覆盖率 **95.64%**（> 93% 闸门）；新增 76 个单元测试 |

### M2 期间发现并修复的问题

24. `intent.py` 排除规则中 trigger 自带否定词（"不要"/"别去"/"不去"）被误计入否定检测，导致 "不要广州塔" 不被排除 → 扣除 trigger 内部否定词后再算 extra_neg。
25. `intent.py` `_negation_count` 只检查否定词**存在性**（`w in text`）而非**出现次数**，导致 "不是不去" 只数到 1 个否定 → 改为 `text.count(w)`。
26. `intent.py` 后置结构 "陈家祠不去" 从 trigger 后提取地点得到空字符串 → 增加 trigger 前回退提取。
27. `intent.py` `_TIME_RE` 中 `\d{0,2}` 优先匹配空字符串而不选 "半" → 调整顺序为 `(半|\d{0,2})`。
28. `intent.py` 规则顺序导致 "晚上有安排" 被 time_window 误拦截 → time_window 优先于 night_view，并加 `_looks_like_place` 过滤不合理地点候选。
29. `planner.py` 路径扩展时 `leg_to_next` 未更新 → 构造 `updated_prev` 替换前一站。
30. `feasibility.py` 访问不存在的 `limits.formulas_walking_hard_limit_ratio` → 改为从 `scoring.formulas.walking_fit.hard_limit_ratio` 读取。
31. `feasibility.py` `_reversal_indices` 与 `scoring.py` 重复实现 → 统一从 `scoring` 导入 `reversal_indices`。

---

## M3 Provider 与检索链

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M3-1 | Provider 通用契约（`ProviderHealth` / `LatLng`） | ✅ | `providers/base.py`；健康状态是"诚实性"在 Provider 层的落点：不可用必须被报告，而不是抛异常让上层猜 |
| M3-2 | LLM：DeepSeek（fast/strong 双档、JSON mode、token 用量、超时重试）+ Null 降级 | ✅ | `providers/llm/`；OpenAI 兼容接口，换厂商只需新增一个 Provider |
| M3-3 | 搜索：Tavily（credit 计价）+ seed_only 降级（零外部调用） | ✅ | `providers/search/`；无 Key 时只读本地知识库 |
| M3-4 | 地图：高德 / OSRM / haversine 估算三档 | ✅ | `providers/map/`；**每档都带 `distance_source` / `duration_source`**，估算值不会被冒充成真实路网 |
| M3-5 | 天气：open-meteo（免费无 Key）+ null | ✅ | `providers/weather/`；阈值来自 `config/ttl.yaml` |
| M3-6 | 装配工厂 `build_providers()` | ✅ | `providers/registry.py`；业务层只依赖一个 `Providers` 值对象；缺 Key 自动选"次优但可用"的实现 |
| M3-7 | 缓存三件套：键构造（纯函数）+ 进程内 TTL/LRU + Postgres 持久缓存 | ✅ | `services/cache.py`；`llm_cache_key` 含 `prompt_version`/`schema_hash`，改 prompt 自动失效 |
| M3-8 | 成本：单价簿 + 账本 + 四级熔断 + 落库 | ✅ | `services/cost.py`；**`null` 单价 ⇒ 金额记 0 但 `calibrated=False`**，报表显式标注"不代表真实支出" |
| M3-9 | 检索链：8 级瀑布引擎 + L5–L8 实现 | ✅ | `services/retrieval_chain.py` / `retrieval_layers.py`；命中即短路并记 `resolved_by`，单层失败继续降级 |
| M3-10 | 限流器（`rate_limit_counters` 固定窗口） | ✅ | `services/rate_limit.py`；`limit<=0` 视为关闭限制，多实例天然一致 |
| M3-11 | 补 `scripts/cost_report.py` | ✅ | `make cost` 此前指向一个不存在的脚本（死目标）；现在可真实出报表 |
| M3-12 | 测试（unit / contract / integration 三层） | ✅ | 新增 **154** 个测试：unit 104 + contract 40（respx 录制真实响应形状）+ integration 10 |

### M3 的静态与回归门槛

| 项 | 结果 |
| --- | --- |
| `make check` | ✅ 全绿 |
| 后端测试 | 678 → **854** passed（含审查轮新增的 11 个回归） |
| 后端覆盖率 | 95.64% → **97.27%**（闸门 93%；M3 新增文件 100%） |
| 前端测试 | 93 → **123** passed（第五轮审查新增 30） |
| ruff | 0 问题 |
| mypy strict | `app` + `scripts` + `tests` 0 错 |

### M3 期间发现并修复的问题

32. **`config/pricing.yaml` 的 `map.*` 单价被当成 CNY 用了**：金额按 `单价 × 汇率` 算，
    但写进 `cost_logs.unit_price` 的却是未换算的原值 —— 同一行记录里并存两种币种口径。
    改为金额与单价同币种，并加测试钉住"单次调用的金额 == 单价"（天条：两处口径必须一致）。
33. `make cost` 指向 `scripts/cost_report.py`，而该文件**不存在** —— 一个只会报错的死目标。
    已补上脚本（同时输出"未校准单价的占比"，而不是只打印一个漂亮的总额）。
34. 各 Provider 构造函数里留了一个从未被使用的 `client` 注入参数（"以后可能要用"）——
    实际测试用 respx 拦网络即可，于是删掉：预留参数会让调用方以为可以控制连接生命周期，其实不能。

### M3 代码审查（第四轮：对抗性审查）

> 范围：逐行读 M3 新增的 providers / services / scripts，对每个可疑点**先复现再修**。
> 结果：后端 843 → **854** 测试（unit 694 / contract 43 / integration 117），
> **M3 新增文件覆盖率全部 100%**，总覆盖率 97.27%。

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| 35 | **`LlmUsage.usage_known` 是个死字段** —— 类型上写着"有些厂商不返回 token 数，此时不能写 0 假装没花钱"，但 `_parse_completion` 恒置 `True`，`LlmLayer` 也从不读它。于是 OpenAI 兼容网关省略 `usage` 时，会生成一条 **`calibrated=True` 的 0 元记录** —— 等于向用户声称"这次调用没花钱"。这是 `pricing.yaml` 里那条铁律（null ≠ 0）在另一个字段上的重现 | 🔴 高（诚实性） | 缺 `usage` 时置 `usage_known=False`；`LlmLayer` 据此记 `calibrated=False`。与"单价未校准"走同一条路径 |
| 36 | **`weather_from_daily` 接受 `str` 作为日期数组** —— `str` 也是 `Sequence`，`enumerate` 把它逐字符拆开，于是 `"2026-09-12"` 配上**真实的**温度，产出日期为 `"2"`/`"0"` 的快照。静默错位比报错危险得多，而 docstring 里恰好写着"宁可少给一天也不要错位" | 🟡 中（数据正确性） | 四个 daily 字段都加"真数组才接受"的门；签名从 `dict[str, Sequence]` 改为 `Mapping[str, object]`（它来自 `response.json()`，是不可信输入，不该在类型上假装已校验） |
| 37 | **成本报表的"未校准记录数"没带时间窗** —— 按类别的聚合带 `created_at >= since`，未校准计数却是全量历史。表头写"近 7 天"、数字却是历史总和，两者互相矛盾 —— 报表的全部价值就在这两个数能被相信 | 🟡 中（报表可信度） | 补同一个 `since`；用"插入一条 30 天前的记录，断言 1 天窗口不变、90 天窗口 +1"钉住 |
| 38 | **同一个 `duration_min` 在两家 Provider 上取整政策不同** —— 高德用 ceil（并注释"宁可多算 1 分钟"），`domain.geo.travel_minutes` 也是 ceil，而 OSRM 用 `round()`。89 秒 → OSRM 报 1 分钟，少算的那一分钟是用户的时间 | 🟢 低（一致性） | OSRM 改 ceil，与既有政策对齐 |
| 39 | **`isinstance(x, Sequence)` 放过了 `str`** —— `messages="你好"` 被拆成两条单字消息、`domains="example.com"` 被拆成 11 个域名约束，**且不报错**，只会静默发出一个荒谬的请求（已实测复现） | 🟡 中（防御纵深） | 新增 `_items()`：`str`/`bytes` 一律视为"本层不处理" |
| 40 | **`int(None)` / `float(None)` 会把检索链打成 500** —— 链只捕 `ProviderError` 与 `CostBreakerOpen`，而 `max_output_tokens: None`、`temperature: "x"`、`max_results: "很多"` 会抛 `TypeError`/`ValueError` 穿过整条链 | 🟡 中 | 载荷里的数值一律走 `_int_or` / `_int_or_none` / `_float_or` 容错转换（与已有的 `_point` / `_message` 一致：外部输入先判形状） |
| 41 | **计费单位在两处定义且注释与代码矛盾** —— `pricing.yaml` 的 `search.tavily.costs` 与 `tavily.py` 的 `_CREDITS_PER_OP` 各写一份，而该文件 docstring 声称"不写死在这里"。两边"看起来都对"，一旦漂移估算与账单就不一致 | 🟢 低（配置一致性） | 分清职责：Provider 定义"一次调用几个 credit"、配置定义"一个 credit 多少钱"；新增交叉断言把两者钉在一起（同 R14/R12 的思路：**没有断言的地方一定会漂移**） |

#### 审查方法上的一条教训

第 35 条的回归测试第一版**通过了一个它本该抓错的变异** —— 因为测试用的是真实 `pricing.yaml`，
而其中 DeepSeek 单价是 `null`，于是"成本不可信"的结论来自价格簿而不是被测代码，
`calibrated is False` 恒成立。改用**单价已校准的合成定价**并加对照组（同样定价下
有 token 数时 `calibrated is True`）后，变异才被抓住。

结论：这种 bug 不能用真实配置测。**断言一条"不可信"结论时，必须确定它来自被测代码，
而不是来自另一处恰好也返回不可信的配置。** 本轮 5 个修复都做了变异验证（把修复改回去，
确认对应测试真的会失败）。

### M3 刻意没做的事（如实记录）

- **未做真实 Key 调用**：DeepSeek / 高德 Key 均未提供，因此这两个 Provider 只经过 respx 契约测试。
  已在文件头显式标注"未在真实 Key 下验证"，任何结构不符都会抛 `ProviderError` 并降级，
  不会把错误数据当真实数据用。
- **`config/pricing.yaml` 的 DeepSeek 单价仍是 `null`**：官方价目页是 JS 渲染，静态抓不到。
  首次真实调用前必须回填，否则成本报表里 LLM 一栏只有 token 数、没有金额。
  → **已于 2026-09-13 回填并校准**（M4-16，见 B8）：本条是 M3 当时的事实，不是现状。
- **L1–L4（城市知识库 / 热门地点 / 路线模板 / 关系图）未在本轮落地**：它们是数据库查询，
  需要规划编排（M4）才能确定查询形态。引擎已按同一 `resolve()` 契约设计，M4 装配时注入即可。

### 第五轮：前端对抗性审查

> 范围：用与后端相同的方式审查 `frontend/`（lib / components / app），
> 每个可疑点先复现再修，修完做变异验证。
> 结果：前端测试 **93 → 123**，覆盖率从"装了提供商但没跑过"变成**真实可测且带闸门**，
> 语句 **98.56%** / 分支 85.80% / 函数 91.66%（`lib/` 100%）。

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| F1 | **原型链查表** —— `TABLE[key] ?? key` 看起来完全正确，但 `TABLE["constructor"]` 拿到的是函数、`["__proto__"]` 拿到的是对象，而 `??` 只拦 `null`/`undefined`，这些值会**穿过回退分支**。`formatTransport("__proto__")` 于是返回一个对象（签名却写着 `string`），React 拿到 function/object 当子节点直接抛错 —— 后端枚举属不可信输入 | 🟡 中 | 新增 `lookupLabel()`（`hasOwnProperty` 只认自有属性），`formatTransport` / `formatArchetype` / 降级文案全部改走它 |
| F2 | **票价字段取回来了却从不上屏** —— `PlaceCard` 只在 `unknown_fields` 含 `price_min` 时显示"⚠ 票价未知"，`price_min`/`price_max` 有值时反而不显示。于是两个接口字段永远不可见：只知道缺什么，永远看不到有什么 | 🟡 中（信息丢失） | 改由数据本身决定显示：`formatPrice(min, max)` 直接上屏，且**不再依赖后端自己的缺口记账**（两者不一致时以数据为准） |
| F3 | **`/about/data` 未署名 OpenStreetMap** —— 页面上写着"数据来自本项目的本地知识库"，而地点实际来自 OSM（ODbL 要求署名）；同时写死了"约 200+ 地点、30+ 路线"这种每次建库就过时的数字 | 🟡 中（合规 + 诚实性） | 拆成三条来源（OSM/ODbL、人工整理、距离估算）并显式署名 **© OpenStreetMap contributors, ODbL**；**删掉写死的规模数字**，改为指向浏览页与 `/health`（数字只有一个出处，才不会两边漂移） |
| F4 | **取消被当成"后端未启动"** —— `request()` 把 `fetch` 的一切异常都包成 `NetworkError`，而 `AbortController` 取消（用户离开页面、未来加超时）也走同一条路。排障会被引向完全错误的方向 | 🟢 低 | 新增 `isAbortError()`（按 `name` 判，跨运行时稳定）；取消原样抛出，不伪装成连接故障。组件侧已统一用 `kind: "unreachable"` 区分 |
| F5 | **`EMPTY_RESPONSE` 分支丢掉 `X-Request-Id`** —— 错误文案让用户"把 X-Request-Id 反馈给我们"，但该分支只读 `meta.request_id`，而后端在 200 但 `data:null` 时不带 `meta`。用户于是被要求提供一个已经丢掉的值 | 🟢 低（诚实性） | 与另一条错误分支取法一致：`meta.request_id` 优先，回退响应头 |
| F6 | **文案把"接口未实现"与"输入被拒"混为一谈**（同 R10 的思路，但代码里还剩一处） —— 400/422 被归进"引擎开发中"并附一句"需求已被前端完整校验"，而 422 恰恰意味着后端认为需求不合法 | 🟢 低（诚实性） | 拆成 `NOT_IMPLEMENTED_STATUSES`（404/405/501）与 `REJECTED_STATUSES`（400/422）两组，文案分别指向"等引擎上线"与"改输入" |
| F7 | **`city` 未编码就拼进路径** —— 同类查询函数都用了 `encodeURIComponent`，只有类别筛选的 `buildHref` 漏了；`a/b` 会被拼成两个路径段，链到另一个页面 | 🟢 低 | 补编码（与 `lib/api.ts` 一致） |
| F8 | **`listRoutes` 缺类型参数与默认值契约** —— 与 `listPlaces` 不对称，且 `with_stops` 的真实默认值（后端返回站点明细）没有在客户端体现 | 🟢 低 | 补齐参数类型与 `with_stops=true` 默认值，并加"筛选为空时不写空参数"的断言 |
| F9 | **前端覆盖率的 `include` 漏了 `app/**`** —— 页面里的真实判断逻辑（空结果 / 故障分支 / 诚实性标注）不计入统计，数字看着高却没覆盖最难测的那部分；且 `@vitest/coverage-v8` 未安装，`vitest --coverage` 直接报 MISSING DEPENDENCY（配置是死的） | 🟡 中（度量失真） | `include` 补 `app/**`；装上与 vitest 同大版本的 `@vitest/coverage-v8@3.2.4`；**按实测数字**设 `thresholds` 并把 `test-frontend-cov` 接进 `make check` |

#### 本轮新增/扩充的测试（+30）

| 文件 | 数量 | 覆盖内容 |
| --- | --- | --- |
| `frontend/lib/__tests__/format.test.ts` | 17 → 32 | `lookupLabel` 对 `constructor`/`__proto__`/`toString` 的防护；`formatPrice` 的"只说知道的那一端"（只有下限→"X 元起"、只有上限→"最多 X 元"、上下限反了→"未知"，**不以任何一端猜一个区间**）；负金额与非有限数 |
| `frontend/lib/__tests__/api.test.ts` | 23 → 28 | 新增 `planTrip` 三例（必须是 POST、payload 完整序列化、`budget: null` 不被丢键）；取消不报 `NetworkError`（F4）；`EMPTY_RESPONSE` 保留响应头里的 requestId（F5） |
| `frontend/components/__tests__/degraded-banner.test.tsx` | +1 | `describeDegradedMode("constructor")` 不得渲染出函数（F1b） |
| `frontend/app/explore/[city]/__tests__/page.test.tsx` | 18 → 21 | 票价有值时必须上屏（F2）；`a/b` 这类城市名在筛选链接里被编码（F7） |
| `frontend/app/about/data/__tests__/page.test.tsx` | 新增 6 | OSM/ODbL 署名存在；**页面不得出现写死的规模数字**（F3） |
| `frontend/app/__tests__/layout.test.tsx` | 新增 5 | 根布局此前是唯一 0% 的源文件。断言 `lang="zh-CN"`、`title.template`、`openGraph`、`maximumScale: 5`（无障碍） |

#### 又一条方法上的教训：**测试自己产生的警告也会变成噪音**

`layout.test.tsx` 第一版用 testing-library 渲染根布局，于是 `<html>` 被塞进 RTL 的 `<div>` 容器，
React 每次都会打 `In HTML, <html> cannot be a child of <div>` —— 测试**照样通过**，
但 CI 日志里从此多一条刺眼的 stderr。长期看，这会训练人忽略警告，而忽略警告的团队
最终会漏掉真正的 warning。第二版改为**直接调用组件函数并断言元素树**（`RootLayout` 无 hooks，安全），
警告消失、断言不变。

本轮 10 处修复全部做了变异验证（后端那条教训在这里同样适用）：把修复逐一改回去，
确认对应测试真的失败 —— 包括**覆盖率闸门本身**（把 `statements` 阈值抬到 99.9，确认 `vitest` 非零退出）。
否则得到的是一套"永远会通过"的测试，而它们的存在恰恰让你以为这些路径已经被守住了。

---

## 前端视觉重构：Cinematic Editorial（2026-09-12）


> 需求：把现有前端整体升级为「电影感 / 编辑式 / 克制高级」的视觉语言，
> **但在保留原项目主题、品牌定位、核心功能与全部文案的前提下**完成，不得改变业务定位。
> 本轮**只改视觉层与交互质感**：业务逻辑、文案、DOM 语义、角色与既有测试契约一律未动。

### 交付

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| V1 | 字体系统两层化（展示体衬线 + UI 体无衬线） | ✅ | `app/styles/fonts.css`：`--font-display` / `--font-sans`；h1/h2 走展示体，h3 与数据列表保持无衬线 |
| V2 | 统一入场动效 | ✅ | `app/styles/theme.css`：`fade-rise` / `-delay` / `-delay-2` / `fade-in`，只动 transform+opacity；`prefers-reduced-motion` 直接呈现终态 |
| V3 | 电影感 Hero（背景视频 + 自定义循环 + 渐变纱幕） | ✅ | `components/hero-video.tsx`：RAF 读 `currentTime`/`duration` 算透明度，0.5s 淡入 / 0.5s 淡出 / 片尾黑场 100ms 后回起点；`heroVeilOpacity` 抽成纯函数可单测 |
| V4 | 导航与页脚统一 | ✅ | `site-header.tsx`（沙色玻璃 + 发丝线 + 当前页 `aria-current`，由 `current` prop 传入而非 `usePathname`）、`site-footer.tsx`（三页共用） |
| V5 | 设计原语统一 | ✅ | Button / Card / Chip / Segmented 重做视觉：胶囊只给「动作」，表单控件收紧到 `--radius-btn`；卡片默认只留发丝线，不再靠阴影堆层次 |
| V6 | 其余页面同步重构 | ✅ | `/explore/[city]`、`/about/data` 改为发丝线 + 编辑式节奏；语义、文案、角色与断言字符串全部保留 |
| V7 | 测试与覆盖率闸门 | ✅ | 新增 `components/__tests__/hero-video.test.tsx`（16 例）；前端 **123 → 139** 测试；语句 98.60% / 分支 86.30% / 函数 93.33%（均高于闸门，且比重构前略高） |
| V8 | 静态检查与构建 | ✅ | `pnpm lint` / `pnpm typecheck` / `pnpm test:coverage` / `pnpm build` 四条全绿；并核对产物 CSS 确实生成了 `font-display`、`animate-fade-rise`、`@keyframes fade-rise`、`text-quiet` 等 |
| V9 | 端到端冒烟（无后端） | ✅ | `next start` + curl：首页 h1 唯一、Hero 类名与 `<video>` 属性正确；`/about/data` 含 OSM 署名；`/explore/guangzhou` 在无后端时**如实**显示「未连接到后端服务」而不是占位数据 |
| V10 | 验收反馈修正：表单居中 + 背景山树可见度 | ✅ | 规划卡改为居中栏（`mx-auto max-w-4xl`，状态条与表单同宽），锚点 `scroll-mt` 加高到 32/28 以免被吸顶头部盖住；Hero 纱幕改为「两端实心 + 中间透明 + 中心柔光晕」，`.hero-veil` 去压暗（`saturate(.95)`），并移除**从未生效**的 `opacity-70`（行内样式优先级更高） |

### 设计决策（都可以被推翻，但先说清楚为什么）

1. **不引入 Google Fonts**：`Instrument Serif` / `Inter` 写成**系统字体栈的首选名**。
   项目既有决定是本机与 CI 对部分境外域名不稳定、构建与首屏不得依赖外部字体请求
   （见 `globals.css` 注释）。如今本地装了这两个字体就自动生效，没装则逐字回退
   （中文回退 Songti SC / Noto Serif SC → serif），**既拿到设计想要的两层字体，又不引入构建期外部依赖**。
2. **Hero 视频不用原生 `loop`，也不写 `autoPlay`**：`loop` 的片尾是硬切；`autoPlay` 会在
   DOM 插入时立刻播放，等 effect 里再拦 `prefers-reduced-motion` 就晚了。
   改成「元数据到达才 `play()`」——顺带让 jsdom 里永不触发 not-implemented 警告（测试日志保持干净）。
3. **主 CTA 用墨色实底，品牌青绿留给链接与选中态**：一页里只该有一个最重的动作。
4. **圆角收紧 + 卡片去阴影**：卡片 16→14px、控件 12→10px；层次改由发丝线与留白建立，
   阴影只保留两档（`shadow-subtle` / `shadow-lift`）。
5. **`:focus-visible` 不再写死 `border-radius`**：它会把胶囊按钮的圆角在获得焦点的一瞬间
   压成 6px（看得见的跳动）。`outline` 本身就会跟随元素圆角。
6. **Hero 的信任文案从 `ink-faint` 提到 `ink-soft`**：`#8a93a8` 在沙色底上对比度仅约 2.9:1，
   而这一行承载的是「数据有来源」这个卖点。

### 刻意没做 / 已知取舍（如实记录）

- **未做真机截图级视觉验证**（本环境无浏览器）：响应式结论来自断点推演
  （375 / 768 / 1440 / 1920）+ 服务端产物核对，未发现横向溢出；
  长接口路径补了 `break-all`。视觉「高级感」这一条最终仍需人眼确认。
- **Windows 上展示体会回退到默认衬线**（宋体），中文标题观感弱于 macOS/iOS 的 Songti SC。
  要完全可控得自托管字体（`next/font/local` + 两个 woff2，约 60KB），本轮没做。
- **背景视频走外部 CDN，且移动端同样加载**（`preload="metadata"`、元数据到达后才播放）。
  要省移动流量，把 `<HeroVideo />` 用 `min-width` 媒体查询包一层即可 ——
  这是**已知的、一行可改的**取舍（`NEXT_PUBLIC_HERO_VIDEO_URL` 可换成自有资源）。
- **未动任何业务逻辑与文案**：`planner-form`、`degraded-banner`、`lib/api.ts` 的行为逐条
  由既有测试守着（139 条全绿），重构只换了它们的呈现层。

---

## M4 规划编排与 API（2026-09-12）

> 范围：`plan_service` 编排、SSE 进度、trips API 全套、修改/撤销、分享、幂等、错误码，
> 并把 L1/L3/L4 注入检索链。
> 结果：后端测试 **854 → 913**（unit 731 / contract 43 / integration 139）；
> 覆盖率 **97.27% → 95.46%**（分母里新增了近 800 行服务与端点代码，闸门 93% 仍然通过）；
> `make check` 全绿。

### 交付

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M4-1 | 游客会话（签名 cookie，无账号） | ✅ | `services/session.py`；HMAC + `compare_digest`，伪造/篡改一律当作"没有"并签发新的；11 个单测 |
| M4-2 | L1/L3/L4 注入检索链 + ORM→领域映射 | ✅ | `services/kb_layers.py`；映射只在这一处发生，算法永远看不到 SQLAlchemy |
| M4-3 | 规划编排 `plan_service` | ✅ | 意图 → 候选 → 束搜索 → 模板复用 → 校验 → 评分 → 落库；**无 Key 时**一次真实规划约 230ms、外部成本 0；配 Key 后走 L5→L7（约 2.7s，见 M4-14） |
| M4-4 | 幂等（同 session 同参数 10s） | ✅ | 集成测试断言第二次 `meta.cached === true` 且 `trip_id` 相同 |
| M4-5 | 跨会话 `plan_cache` 复用 | ✅ | 命中别人的缓存时**复制一份**给当前会话，绝不把别人的 `trip_id` 交出去（有测试） |
| M4-6 | SSE 进度流（`plan.started` / `progress` / `completed` / `failed`） | ✅ | `services/plan_events.py` + `GET /trips/{id}/stream`；缓冲重放解决"客户端后到"，TTL/上限解决"通道泄漏" |
| M4-7 | `POST /trips:plan`（202 + `?sync=true`） | ✅ | 异步走 FastAPI 后台任务并**自己开 session**（请求级会话在响应结束时已关闭） |
| M4-8 | `GET /trips/{id}` | ✅ | 含全部方案、站点快照、可行性报告、评分细项、来源署名 |
| M4-9 | 自然语言修改 `/revise` | ✅ | 规则引擎解析成约束后**本地重算**（不触发联网搜索）；产出可读 Diff；理解不了就回问澄清 |
| M4-10 | 撤销 `/undo` | ✅ | 每次修改都是一条新 trip（`parent_trip_id`），撤销即沿链回退，历史天然可回看 |
| M4-11 | 分享 `/share` + `GET /public/trips/{slug}` + 复制 | ✅ | slug 为 12 位 base62；取消分享**立刻 404**；复制得到当前访客可编辑的新行程 |
| M4-12 | 限流（按 IP/按 session 的固定窗口） | ✅ | 装在"确认是冷规划之后"：缓存命中与幂等重放不消耗配额 |
| M4-13 | 测试（unit + 集成） | ✅ | 新增 59 个：`test_session.py` 11 · `test_plan_events.py` 10 · `test_plan_service.py` 15 · `test_trips_api.py` 22（真实库 + 真实知识库）；含 archetype 差异化与 Jaccard 上限的回归 |
| M4-14 | 接入 L7（LLM）：意图补全 + 方案叙事，含降级与可观测 | ✅ | `services/llm_planner.py` + `plan_service` 装配 L5 缓存/L7 模型；无 Key / 超时 / 非 JSON / schema 不符 / 熔断一律回到规则与模板。实测：配 Key 后一次规划 **2749ms**、`meta.llm.used=true`、`calls=2`、`tasks={intent_patch: llm, route_narrative: llm}`；未配 Key 时 `enabled=false, used=false` 且 `degraded_modes` 带 `llm:` 条目 |
| M4-15 | LLM 可观测性（响应 + 日志 + 成本落库） | ✅ | 响应 `meta.llm`（是否配置/是否真调/模型/token/金额与是否校准/每个任务的归属/降级原因）· 日志 `plan.completed.llm` 与 `plan.llm_*` 事件 · `cost_logs` 逐条落库 + `trips.total_cost_cny`。新增 25 个单测 + 3 个集成断言 + 1 个活体测试（有 Key 才跑） |
| M4-16 | 前端接入 SSE 进度流 + 展示 `meta.llm`；校准 DeepSeek 单价 | ✅ | `lib/plan-stream.ts`（订阅 `plan.started/progress/completed/failed`，断流与失败都如实报告）+ `components/llm-summary.tsx`（在哪个任务上用了模型 / 是否命中缓存 / token / 金额与是否已校准 / 降级原因）+ 表单展示进度；`config/pricing.yaml` 按官方价目页回填 `deepseek-flash`/`deepseek-v4-pro` 并写 `calibrated_at: 2026-09-13`，`meta.llm.cost_calibrated` 因此从 `false` 变 `true`。新增 22 个前端测试（plan-stream 6 · llm-summary 8 · planner-llm-status 6 · 取值契约 2） |
| M4-17 | 开发设置面板（`/dev`，**仅开发环境**） | ✅ | 后端 `api/v1/dev.py` 只在 `ENV=development` 注册 + `services/dev_config.py`（白名单 / Secret 只写不读 / YAML 先校验后落盘 / 改完热生效且失败回滚）；前端 `app/dev/`（入口只在非 production 构建渲染，页面 `noindex`，Token 只存 sessionStorage）。新增 26 后端 + 28 前端测试 |

### M4 期间发现并修复的问题

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| 42 | **排除规则不剥动词**：`"不要去广州塔"` 解析出的排除项是 **`"去广州塔"`** —— 一个永远匹配不上任何地点的名字。用户的要求被静默忽略，而 Diff 还会报告"移除了 广州塔"（`别去/不去` 不触发该问题，因为 trigger 自带"去"） | 🔴 高（诚实性） | 新增 `_strip_leading_verb`：剥掉 `去/到/来/逛…`，且只在剩余长度仍 ≥2 时剥（`东山` 不会被削成单字） |
| 43 | **候选不在空白处截断**：`"不要去广州塔 想去沙面"` 得到 `"广州塔 想去沙面"` —— 同一个 bug 的另一种触发方式（空格是分句符，不是地名的一部分） | 🟡 中 | `_tail_candidate` 把空白也当终止符；两条回归用例钉住 |
| 44 | **`plan_cache` 裸 INSERT 撞唯一约束 → 500**：同一组参数出现两次是完全正常的（修改刻意绕过缓存；两个会话同时提交），第二次插入直接抛 IntegrityError，用户看到"正常的请求莫名失败" | 🔴 高 | 改 `ON CONFLICT (params_hash, kb_version) DO UPDATE`（更新 trip_id 并续期）。**修的是一次真实 500**，不是理论问题 |
| 45 | **`plan.started` 事件名与载荷不符**：stage 0 也被当成 `plan.progress`，且不带 `request_id`（PRD §7.3 明确要求），前端无法把"这条流"对上"那次请求" | 🟡 中 | stage 0 发 `plan.started` 且带 `request_id` |
| 46 | **`/trips/abc123` 被 FastAPI 判成 422**（参数标注为 `uuid.UUID`）—— 与既有错误码契约冲突：行程路径上的 404 必须是 `TRIP_NOT_FOUND`。一个拼错的链接不该被描述成"你提交的内容不合法" | 🟡 中 | 路径参数按 `str` 收、自己转 uuid，失败即 `TRIP_NOT_FOUND` |
| 47 | **业务异常与框架级 404 的 `context` 结构不一致**：框架那侧带 `path`，业务这侧不带，"出错路径"能不能看到取决于错误由谁抛出 | 🟢 低 | `AppError` 处理器统一补 `path` |
| 48 | **L2（热门地点库）没有独立数据可查**：`popularity_score` 就在 `places` 表里，L1 已经查过它。为了"层数完整"再装一层、再查一遍同一张表，只会多一次往返并让报表虚增覆盖率 | 🟢 架构诚实性 | **不实现 L2**，并在 `kb_layers.py` 顶部写明原因；热度仍在 `0.4 × popularity` 里真实参与打分 |
| 49 | **三套方案可能全是"经典"**：纯按总分取前三名时（实测）输出 `经典路线 × 3` —— 标签、`best_for`、推荐理由全部雷同，三个 Tab 其实是同一条路线的三种摆法；而 PRD FR-06 承诺的是 A 轻松 / B 经典 / C 主题三套**定位不同**的方案，AC-6.3 还要求 `best_for` 差异化 | 🟡 中（产品承诺） | `_select` 改为**每个 archetype 先占一个名额**（名额内仍是该 archetype 的最优），再按总分补满；`best_for` 改由该路线自己的指标（站数/步行/时长/是否模板）推导。顺带把标签与 PRD 的 A/B/C 对齐（A=relaxed / B=classic / C=themed） |

#### 又一条方法上的教训：**随机后缀 ≠ 用例互相独立**

集成测试第一版给每个用例生成一个随机 `free_text` 后缀，以为这样各自的请求参数就不同了。
结果用例之间仍然互相命中缓存 —— **缓存/幂等键是 `params_hash`，由解析后的 intent 与约束构成**，
而 "想轻松一点 abc12345" 里没有任何可解析的量，解析结果与另一个随机后缀完全一样。
修法：让 `free_text` 里带一个**能被解析成预算的数字**，参数才真的不同。

> 教训：判断两条请求"是否相同"不能靠"看起来不一样"，
> 要靠它们**被归一化之后的键**。这一点在写断言时同样成立。

### M4 刻意没做的事（如实记录）

- **天气未接入评分**：`plan_routes(weather=...)` 的接入口留着，但 M4 不拉天气，
  于是天气乘数恒为 1.0。**"没拉天气"不等于"天气是晴"** —— 所以不编造一个条件填进去。
- **L6（地图）/ L8（搜索）仍不在规划路径上**：本地知识库足够出方案（这正是 §15.1 的铁律）。
  M4-14 只接入了 L5（LLM 缓存）与 L7（LLM），而且仅用于**意图补全与方案叙事** ——
  没有任何数字由模型产生。未配 LLM Key 时外部成本仍为 0（`cost_logs` 无记录）。
- **模型不参与排序与打分**：`_compose` / `_score_plan` 是纯函数，输入里根本没有模型输出；
  叙事只改 `name` 与 `recommendation_reason` 两个文案字段，且含数字的文案会被整条丢弃。
- **SSE 通道是进程内的**：单实例假设下成立；多实例部署需要换成 Redis Pub/Sub，已写在模块注释里。
- **未做 p95 压测**：实测单次规划约 230ms（远低于 AC-8.3 的 3s），但这是开发机的单次观测，
  不是 p95 统计。性能门槛属 M7。

### M4 代码审查（第六轮：对抗性审查 `plan_service` / `trip_service` / `plan_events`）

方法：先**复现**再下结论 —— 每个疑点都写一段真实 HTTP / 真实库的探针，只把能复现的记成缺陷。
本轮 8 个可疑点里 **6 个被复现为真缺陷**，2 个被证伪（见下）。

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| 50 | **`/revise` 静默丢弃"只改意图"的指令**：规则引擎有两类输出 —— 约束（排除地点/步行上限）与意图字段（天数/人数/节奏/预算/偏好）。`revise_trip` 只把**约束**传下去、拿原 payload 重算，于是 `"改成 3 天"`/`"预算改成 900 元"`/`"走慢一点"` 既不报错也不改任何东西，却返回**新版本 + 一句"什么都没变"的 Diff**（`总步行 0.2km → 0.2km · 人均 ¥29 → ¥29`）。用户的要求被静默吞掉 —— 与 #42 同一类错误，只是入口在修改而不是首次规划 | 🔴 高（诚实性） | 用解析后的 `delta.intent` 重建规划入参（`plan_request_from_trip({}, intent_to_dict(delta.intent))`，`free_text` 留空以免原文覆盖本次修改）；Diff 补上 `days_before/days_after` |
| 51 | **`/undo` 会走进别人的行程**：复制出来的 trip（`plan_cache` / `copied`）的 `parent_trip_id` 是**来源（fork）**而不是上一版。原实现把它当上一版，并试图"跳过 `plan_cache` 父节点继续往上找"，于是"复制别人的缓存 → 修改 → 撤销"沿父链访问到**原作者那条 trip**，被归属校验拦下，用户拿到 403「这个行程不属于当前会话」；副本自身撤销同样 403，而不是"已经是最早的版本" | 🔴 高（越权边界 + 核心功能） | 区分两种语义：副本是版本链的**起点**（`source ∈ {plan_cache, copied}` → 无更早版本）；删除错误的"跳过"逻辑 |
| 52 | **命中缓存后的幂等失效**：`_copy_trip` 用意图快照重算了一个（还漏掉约束的）`params_hash`，且**没写 `elapsed_ms`** —— 而 `_replay` 靠 `elapsed_ms is not None` 区分"跑完的规划"。副本因此对幂等重放完全不可见：同一会话重复提交同一份需求，每次都重新复制一份，库里堆出重复行程（AC-13.4 失效） | 🟡 中 | 复制时直接沿用调用方的 `fingerprint`，并补 `elapsed_ms=source.generation_ms` |
| 53 | **零权重偏好会被"复活"**：用户说"不拍照"时意图里存的是 `photo: 0.0`。从快照重建请求（复制行程、修改时回退）会把**所有权重键**写回 `preferences`，`build_intent` 再把它们赋成 `1.0` —— 副本反过来开始"优先推荐"用户明确不要的东西 | 🟡 中 | 只带正权重（`float(weight) > 0`，兼容 jsonb 里的字符串），与 `Intent.active_preferences` 的口径对齐 |
| 54 | **`budget_estimated` 写死 `True`**：同一份响应里 `budget_estimated` 与 `feasibility.budget_estimated`（落库的真值）会互相矛盾 | 🟢 低 | 出参以落库报告为准，缺键时保守取 `True` |
| 55 | **Diff 不报告天数变化**：`DiffSummary` 只比较首要路线与预算。"改成 3 天"生效后（#50）Diff 仍然输出"总步行 0.2km → 0.2km"，等于宣称什么都没变 | 🟢 低 | 加 `days_before/days_after` 并在句子里体现（天数/人数是**行程**级属性，不是路线签名） |
| 56 | **模型补丁的排除项静默失效**（接入 L7 时发现）：领域层 `select_candidates` 只消费 `exclude_place` / `exclude_place_id`，而 LLM 补丁新造了一个 `type="exclude"` —— 用户说"广州塔已经去过了"，界面照旧安排广州塔，**所有测试仍然是绿的**；同一次请求里还会与规则引擎的 `exclude_place` 重复成两条约束 | 🔴 高（诚实性） | 改用 `exclude_place`，并按值对**所有** `exclude*` 约束去重（单测钉住类型名，不是只钉值） |
| 57 | **单测隐式读了开发机的 `.env`**：本机配上 `DEEPSEEK_API_KEY` 后，"无 Key 应降级到 `NullLlmProvider`"两个用例立刻变红 —— 它们断言的是"没有 Key 的行为"，却把环境里的 Key 当成了输入 | 🟡 中 | `test_providers._settings()` 显式清空全部 Key；同时 `tests/integration/conftest.py` 把 `LLM_PROVIDER` 压成 `disabled`（否则集成测试会真的调模型：慢、花钱、结果随模型漂移），真实调用改由 `test_llm_live.py` 专测 |

被**证伪**的两个疑点（如实记录，避免下次重复怀疑）：

- ~~`total_distance_m = metrics.transit_distance_m` 是字段错配~~：`RouteMetrics.transit_distance_m`
  名字有歧义，但它的计算是**所有段**距离之和（含步行），正是"总距离"。不是缺陷。
- ~~SSE 进度事件 `{"checked": N, "kept": N}` 在撒谎~~：前端并不消费 `detail`，且已改为只报
  `{"feasible": N}`（`_score_plan` 早于计数就丢掉了不可行方案，原键无从取值）。

#### 变异验证（每条修复都要"改回去"证明测试真的在守它）

逐条把修复还原成修复前的实现，确认新增测试**确实失败**：

| 变异 | 被哪些测试抓住 |
| --- | --- |
| `revise` 用原 payload 重算 | 3 个集成用例（天数/预算/不重放原文） |
| `undo` 还原成修复前实现（含"跳过 plan_cache"） | 2 个单测 + 3 个集成用例 |
| 副本用重算的 `params_hash` 且不写 `elapsed_ms` | 1 个集成用例 |
| 重建入参时带上零权重偏好 | 2 个单测 |
| `budget_estimated` 写死 | 1 个单测 |
| Diff 不报天数 | 1 个单测 |

> 教训：**变异验证能揭穿"测试写得很像在守护某件事，其实没守护"**。
> 第一次做的 `undo` 变异只删了"起点判断"、保留了旧的"跳过"分支，
> 集成用例照样通过 —— 因为那条 bug 需要**完整的旧实现**才会触发。
> 如果当时收工，就会留下一句"已验证"的错觉。

本轮门槛：后端测试 **913 → 934**（+14 单测 `test_trip_service.py`、+7 集成）；
覆盖率 **95.38% → 95.46%**（闸门 93%）；ruff 0 问题；mypy strict 108 文件 0 错；
前端 139 用例与覆盖率闸门不变；`make check` 全绿。

---

## M4 补充（2026-09-13）：单价校准 · 前端可观测 · 开发设置面板

范围：把 DeepSeek 单价校准到官方价目、把 `meta.llm` 接到前端、
再给维护者一个只在开发环境存在的配置面板（改 Key / Provider / 阈值 / `config/*.yaml`）；
过程中发现首页表单**根本提交不上去**（#60），因此一并修掉 —— 否则前两项在 UI 上无从可见。

结果：后端 **934 → 991**（unit 791 / contract 43 / integration 157）；前端 **139 → 189**；
覆盖率：后端 **95.47%**（闸门 93%）、前端 statements **98.44%** / branches **87.19%**
（闸门 96% / 84%）；ruff 0 问题；mypy strict 117 文件 0 错；`make check` 全绿。

已上传到 `github.com/Yugitan/ai-trip-decider`：`dev`（默认）/ `test` / `prod` 三条分支，
首次上传三者同点（`413c9fc`）；分支约定见 `RUNNING.md` §11。

### 交付

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M4-16 | 前端 SSE 进度流 + `meta.llm` 展示 | ✅ | 见上表；`planner-form` 从「假装提交成功」改为真实订阅 `stream_url` |
| M4-17 | 开发设置面板（`/dev`） | ✅ | 三道门：路由只在 `ENV=development` 注册 · `ADMIN_TOKEN`（未设时只允许本机）· 白名单（`DATABASE_URL`/`SESSION_SECRET`/`ENV` 刻意排除） |
| M4-18 | 成本实测报告 `docs/COST_REPORT.md`（`make cost` 首次跑通） | ✅ | 11 次真实 LLM 调用 · 缓存命中 1 · 0.004140 元；8 条「校准前落库」的旧记录如实标为未校准，不美化 |

### 本轮发现并修复的问题

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| 58 | **面板的「保存成功」提示会被随后的刷新抹掉**：保存后要重新拉一次生效配置，而 `load()` 开头就把提示清空了 —— 用户点完保存看不到任何反馈，和保存失败长得一样。同一份代码里 `handleSaveEnv` / `handleSaveFile` 都中了 | 🟡 中（诚实性） | `load()` 增加 `keepNote` 选项并返回是否成功；保存路径改为「先刷新、再报结果」，且刷新失败时保留它的错误提示而不是用成功覆盖 |
| 59 | **Secret 被描述成可以清空，实际做不到**：后端把空值定义为「清空该项」（`_validate_value`），而前端为了区分「没碰过」与「想清空」一律跳过空值 —— 于是界面只能写不能删，说明文字却写着「填入一个空格后再删掉」（照做也不生效） | 🟡 中（诚实性） | 清空改为**显式动作**：「清空该项」按钮 → 提交空值（`changedEnvValues(..., cleared)`），留空仍是「不修改」；错误文案换成真话 |
| 60 | **首页表单稳定 422**（M5 起步时暴露）：表单把界面上的中文标签直接当请求体发出去（`city="广州"`、`preferences=["美食"]`），而后端只认枚举值（`guangzhou` / `food`）—— 点「开始规划」永远失败。它把 M4-16 的成果（前端展示 `meta.llm`）完全挡在 UI 之外：**写了一个用户永远看不到的界面**；而且两端单测/lint/类型检查**全是绿的** | 🔴 高（功能不可用 + 诚实性） | ① 换算显式写在 `planner-form.buildPayload()` 一处（`CITY_OPTIONS[*].api` / `PREFERENCE_OPTIONS[*].api`），**不改后端** —— 接口只认枚举值是应该的，放宽等于让接口语义跟着 UI 文案漂移；② `lib/api.ts` 的 `PlanRequest` 写明这是线上格式；③ 新增**跨语言契约测试**：前端用例直接读 `../config/scoring.yaml` 比对 `preference_dimensions` 键，漏配一个偏好就会红 |
| 61 | **404 的文案在 M4 交付后变成误导**：规划接口已经存在，此时 404/405/501 的真实原因几乎一定是「后端没启动 / 版本过旧 / 地址配错」，而界面仍在说「规划引擎正在开发中（M4 里程碑）」—— 把排障引向"等一个已上线的功能" | 🟢 低 | 改为「后端没有这个接口（HTTP xxx）：通常是后端没启动、版本过旧，或前端配的地址不对」，并把测试名一起改正 |
| 62 | **`make check` 会偷偷改写一份被跟踪的文档**：集成测试 `conftest` 每次都重建测试库，复用 `scripts/seed_guangzhou.py`，而脚本**无条件**把 `docs/DATA_REPORT.md` 写回磁盘 —— 于是每跑一次测试，工作区就凭空多出一个「已修改」的文件；更糟的是这份报告里的数字来自**测试库**，提交上去等于拿测试数据冒充数据现状 | 🟡 中（测试污染工作区 + 诚实性） | 建库脚本新增 `write_report: bool = True`，集成测试显式传 `False`；文档只由人工 `make seed` / `make report` 落盘。修完实测：`make check` 后 `git status` 里 `docs/` 干净 |
| 63 | **成本报表把两种「未校准」混为一谈**：`pricing_calibrated=false` 的提示固定在说「单价在 `pricing.yaml` 里是 `null`」，而真实原因多是**窗口内混着校准前落库的旧记录**（本机 11 条里 8 条如此）—— 照着提示去 `pricing.yaml` 找 null 会一无所获 | 🟢 低（诚实性） | 报表与 `CostStore.summary()` 的说明改为同时点明两种来源，并补一条「换个干净库重跑就会变成 ✅」 |

### 单元测试的同步更新（不是"改测试让它变绿"）

DeepSeek 单价校准后，两个测试的依据变了，因此**改的是断言，不是实现**：

- `test_real_config_marks_deepseek_as_uncalibrated` → 改为 `test_real_config_deepseek_is_calibrated_to_the_written_unit_price`，
  并把金额写死为 `0.00648`（1000 入 + 500 出）：**有人偷偷改 `pricing.yaml` 会立刻红**。
- 新增 `test_real_config_marks_unknown_llm_price_as_unknown_not_free`（用仍未校准的 `openai` 档），
  让「null 单价 → 金额 0 且 `calibrated=False`」这条铁律继续有人守。
- `test_ledger_flags_uncalibrated_entries` 改用仍未校准的 **`map.amap`** 构造反例；
  继续拿 DeepSeek 当反例会让这个测试名不副实。

> 教训：**测试里的"未校准"是一个会过期的前提**。校准完成后如果只把 YAML 改了、测试不管，
> 那条铁律就会变成没人守的注释。

### 开发面板刻意没做的事

- **不在主导航里**：入口只在非 production 构建渲染（`SHOW_DEV_ENTRY = process.env.NODE_ENV !== "production"`），
  且页面 `robots: { index: false }`。用户看不见它，也不需要它。
- **不碰 `DATABASE_URL` / `SESSION_SECRET` / `ENV`**：改前两者必须重启进程（且面板自己就用着库），
  改 `ENV` 等于当场关掉面板并切到生产校验规则。
- **不把 `.env` 交给浏览器**：只列白名单内的键，Secret 读取时一律打码，没有"把 Key 复制到前端"的路径。
- **不新增用户功能**：它写在 `docs`/`RUNNING.md` 的"开发工具"里，不占产品验收项。

---

## CI 接入（2026-09-14）

`.github/workflows/ci.yml`：push 到 `dev` / `test` / `prod` 或向它们提 PR 时跑 **`make check`**。

| # | 事 | 状态 | 说明 |
| --- | --- | --- | --- |
| CI-1 | GitHub Actions 跑 `make check` | ✅ | `services: postgres:16` + `make test-setup`；CI 的 `.env` 从 `.env.example` 生成，只改两行数据库连接。**首跑通过，4.2 分钟**（[run #2](https://github.com/Yugitan/ai-trip-decider/actions/runs/34800434537)） |
| CI-2 | OSM 原始数据（约 3.8MB）入库 | ✅ | 集成测试缺数据是 **fail 而不是 skip**；不这么做就只能把集成测试排除在 CI 之外。CI 里知识库是**真建出来的**（日志里能看到 seed 与关系计算） |

CI 实测与本地的一处差异（不是故障，是设计）：

```
CI    ： 989 passed, 2 skipped     ← 两条 test_llm_live.py 真调模型用例（CI 无 DEEPSEEK_API_KEY）
本地 ： 991 passed                 ← 本机 .env 里配了 Key，所以那两条真跑
```

首跑还撞了一个坑，已修并写进工作流注释：`astral-sh/setup-uv` 的移动 major 标签只到 `v7`，
release 已到 `v10.1.0` —— 写 `@v10` 会让 job 在 “Set up job” 就报 `unable to find version v10`，
一个测试都跑不到。**教训**：第三方 action 的 `@vN` 别名不是平台保证的，别按 release 号猜。

### 决策与代价

- **CI 里不重复造闸门**：只有 `make check`，不再单独跑 lint / mypy / 覆盖率 ——
  写两遍迟早分叉成「本地绿、CI 红」，或者更糟：CI 绿而本地那条真闸门没人跑。
- **用环境变量覆盖 `PG_USER`，而不是改 Makefile**：`PG_USER ?= $(shell whoami)` 是**本机开发**假设，
  而环境变量在 make 里优先于 `?=`；为 CI 改默认值等于让本地也多一处要维护的分支。
- **代价写进文档，不藏起来**：`make fetch-osm` 重跑会改动两份已跟踪的 JSON
  （数据变了应当被看见，而不是被 gitignore 藏起来）；CI 与本地唯一有意的差异就是那三行准备步骤。

---

## M5 起步补充（2026-09-14）：结果页地图 · 搜索 Provider 诚实性 · 两个隐性输入

> 触发自一次真实配置：用户提供了**高德 Web端(JS API) Key** 与 **Tavily Key**。
> 「配 Key」本身没什么可写，但它把两类东西照出来了：一个是**配置说一套、实现跑一套**，
> 另一个是**测试偷偷读了开发机的环境**。

### 一、搜索结果页的站点示意图

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M5-1 | 坐标纠偏 `lib/amap-coords.ts`（WGS-84 → GCJ-02，纯函数） | ✅ | 库里的坐标全来自 OSM（WGS-84），高德瓦片是 GCJ-02；不换则每点偏 100–700 米 |
| M5-2 | 装载器 `lib/amap.ts`（**先设 `serviceHost` 再插脚本**） | ✅ | 顺序错了安全配置无效（官方文档明写）—— 有测试在 `appendChild` 那一刻取快照钉住顺序 |
| M5-3 | 安全密钥不走浏览器：`app/amap-proxy` + `next.config.ts` rewrite | ✅ | 客户端自带 `jscode=ATTACKER` 被服务端覆盖：`sec_code` 与「用真密钥直连高德」逐字符相同；生产构建里密钥出现 **0** 次 |
| M5-4 | 结果页组件 `components/route-map.tsx` 接入每套方案卡片 | ✅ | 编号标记 + 虚线（图注写明**不是实际行车路线**）；无 Key / 加载失败 / 坐标不全各有真话 |
| M5-5 | 真实浏览器验证 | ✅ | 无头 Chrome：`canvasCount=1`、三个标记、`plaintextCodeInPage=false`、服务端日志出现 `GET /_AMapService/...`（无失败请求） |

坐标纠偏的正确性**不是靠单测自证**的：用 `AMap.convertFrom(..., "gps")` 取官方值做基准，
四个点最大偏差 2.8×10⁻⁶ 度（≈0.31 米）；官方向量已写进测试注释与断言。
那些先写下的「广州是向东北偏」的断言在第一次运行就红了 —— 实际是**东南**（纬度变小、经度变大），
**靠印象断言数字比不写断言更危险**。

### 二、搜索 Provider：「配置说 serper，实际在跑 seed_only」

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| 64 | **`search_provider_effective` 能返回代码里不存在的实现**：`serper` / `bing` 的 Key 在 `.env` 与 dev 面板里都存在（预留），`auto` 下配了它们就会返回该名字，而 `registry._build_search()` 只会构造 `TavilyProvider` 或 `SeedOnlyProvider` —— 于是 `/health`、面板的生效快照、`degraded_modes` 都在说「搜索：serper」，进程实际跑的是 seed_only | 🟡 中（诚实性） | `SEARCH_KEY_FIELDS`（可配）与 `IMPLEMENTED_SEARCH_PROVIDERS`（已实现）拆成两份名单；effective 只允许返回已实现的名字；新增 `ignored_search_keys()` 与一条单独的降级提示 `search:serper(已预留、尚未实现：Key 不生效)` |
| 65 | **同一种降级、两种原因被写成一句**：`SeedOnlyProvider` 的健康说明写死「未配置搜索 API Key」。而已实现名单之外的情况是「**配了但没实现**」——沿用原话就是在说假话 | 🟢 低（诚实性） | `SeedOnlyProvider(reason=...)`，由 registry 按 `ignored_search_keys()` 传入原因 |

防漂移的做法与 R14/R12 一致：**没有断言的地方一定会漂移**。
新增的用例对 `IMPLEMENTED_SEARCH_PROVIDERS` 里每个名字同时断言两件事 ——
配置层说它生效、工厂层真的构造出了它（只断言其中一条的话，另一处漏掉分支照样绿）。

### 三、测试隐式读了开发机的环境

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| 66 | **`test_config_validators.make_settings()` 挡不住已导出的环境变量**：它用 `Settings(_env_file=None, ...)`，而 `_env_file=None` 只挡住 `.env` **文件** —— pydantic-settings 总会读 `os.environ`，且环境变量优先。本机 shell 里 export 了 35 字符的 `DEEPSEEK_API_KEY` 后，「配了 OpenAI 的 Key 但 provider 指向 deepseek 应判定为未配置」立刻变红。与 #57 是同一个坑，只不过 #57 修的是 `test_providers._settings()`，这个文件没跟上 | 🟡 中 | `make_settings` 显式清空七个 Key 字段，用例的输入完全由自己决定 |

同一根因的另一半写在 `RUNNING.md` §8.8：**shell 里一个空值的 `TAVILY_API_KEY=`
会把 `.env` 里的真值遮住**（空串被归一化成 `None`，而「已设置但为空」优先于文件里的真值）。
现象是「明明填了 Key 却还是 seed_only」，排查手法是 `printenv` + `env -u`。

> 教训：**「配置没生效」与「测试环境不干净」可以是同一个原因。**
> 遇到「本地绿、CI 红」或反之，先看环境变量，而不是先怀疑代码。

### 四、Tavily 真实调用验证（B7 部分解除）

新增 `tests/integration/test_search_live.py`（有 Key 则跑、无 Key 自动 skip，与 `test_llm_live.py` 同款），
**一次运行花 2 credits**：

| 检查 | 结果 |
| --- | --- |
| 真实搜索能拿到结果，且我们的解析层吃下真实响应（`title`/`content`/`url`/`score` 字段名未变） | ✅ |
| `api_key` 放在请求 body 里的旧用法仍被接受（不必改成 Bearer 头） | ✅ 未报 401 |
| `recency_days` → `days` 参数在 basic 检索下不被拒 | ✅ |
| 响应里**没有** `credits` 字段，而记账走的是 `pricing.yaml` 的 credits 口径 | ✅ 不依赖响应字段（已核代码） |
| 「能真实调用」与「单价已校准」被钉在一起 | ✅ `PriceBook.search("tavily", "search_basic")` 已校准且金额 > 0 |

**但「配了搜索 Key」目前还不会影响方案**：规划链注册的层是 L5 / L1 / L3 / L4 / L7，
**L8（联网搜索）不在链上**（`plan_service._build_chain`）——
`RUNNING.md` 的降级矩阵里写的是「具备能力」，不是「已经在用」。
**没拉搜索 ≠ 搜索没结果**，所以不把它当成已有数据填进方案里。

### 本轮门槛

| 项 | 结果 |
| --- | --- |
| 后端测试 | **999 passed**（含 3 条 Tavily 活体；不配 Key 时那 3 条如实 skip） |
| 前端测试 | 233 → **280 passed**（新增 47：坐标 15 · 装载器 11 · 代理逻辑 6 · 代理路由 4 · 地图组件 11） |
| 前端覆盖率 | statements **98.79%** / branches **88.43%** / functions 95.54%（闸门 96 / 84 / 89） |
| 新增文件覆盖率 | `amap-coords.ts` / `amap.ts` / `amap-proxy.ts` 均 **100%**；`route-map.tsx` 100% / 92.3% |
| 静态检查 | ruff 0 问题；mypy strict **118 文件** 0 错；`pnpm lint` / `typecheck` / `build` 全结 |
| 生产产物 | 安全密钥在 `.next` 里出现 **0** 次；JS API Key 按设计出现在客户端 1 处 |

### 刻意没做的事

- **未把 L8 接进规划链**：接它等于让「本地库不足」时自动联网，属于策略改动（要配配额、缓存与降级路径），
  不该顺手做；现在只是让能力真的可用，并有活体测试守着。
- **未改 `_build_search` 支持 serper / bing**：先让「说的」与「做的」一致，再谈扩厂商。
- **未给地图做方案对比视图、`/trip/{id}` 路由、分享页「复制这套路线」**：属 M5 剩余部分。
- **未把三个 WebGL 地图改成共享一张**：目前最多 3 张，改动收益不划。

---

## 配置漂移守卫（2026-09-15）：模板 ↔ 代码 ↔ 面板

> 补「配置测试」时发现的真问题：**两份 env 模板都没有人守**。
> 它们是使用者唯一会看的配置清单（README / RUNNING 都把它当权威引用），
> 却和代码之间没有任何约束 —— 于是往两个方向漂移。

### 新增的两道守卫

| # | 测试 | 守什么 | 当场抓到的漂移 |
| --- | --- | --- | --- |
| 67 | `backend/tests/unit/test_env_hygiene.py`（4 条） | 根 `.env.example` 的**活动键** ↔ `Settings.model_fields` ↔ 开发面板 `ENV_FIELDS`：字段必须都写进模板；模板里不许有没人读的活键；`NEXT_PUBLIC_*` 不许在根模板里是活的（Next 读不到根 `.env`） | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 是 `Settings` 字段、面板也能改，模板里**一个字都没有** —— 而模板自称「所有环境变量」 |
| 68 | `frontend/lib/__tests__/env-example.test.ts`（4 条） | `frontend/.env.example` ↔ 源码里真正读 `process.env` 的地方，**两个方向都查**（读了必须写、写了必须有人读）；**允许浏览器看见的 `NEXT_PUBLIC_*` 清单钉死**；`AMAP_SECURITY_CODE` 只允许在服务端代理路由里读，任何文件都不许出现 `NEXT_PUBLIC_AMAP_SECURITY` | `API_BASE_URL`（服务端用：rewrites 目标 + SSR 绝对地址）只写在根模板里，而 Next **读不到根 `.env`** |

### 结论：守卫必须自己先被验证过

两条守卫都做了**反向验证** —— 把 `SEARCH_PROVIDER` 改成 `SEARCH_PROVIDR`、
把前端模板里的 `AMAP_SECURITY_CODE` 改名，测试分别变红
（`这些 Settings 字段在 .env.example 里一个字都没写` / `这些变量代码在读，但模板里没有`），
改回即绿。**不会失败的测试等于没有测试**，所以这道守卫自己也留了一次"红过"的证据。

### 铁律的例外被"钉住"，而不是"记着"

`NEXT_PUBLIC_*` 白名单（`NEXT_PUBLIC_API_BASE_URL` / `NEXT_PUBLIC_HERO_VIDEO_URL` /
`NEXT_PUBLIC_AMAP_JS_KEY`）写死在测试里：**新增一个就要改测试**，
于是「再放一个 Key 进浏览器产物」必须是一次显式决定，而不是顺手加个前缀。

---

## 开发面板的诚实性（2026-09-15）：两把 Key 分家 · 字段提示 · 11 个空旋钮

> 触发自一次真实误判：高德 JS API Key 已经配好、地图也实测渲染出来了，
> 但看到 `/dev` 面板上「高德 Web Key（未配置）」，就得出「高德地图没配置」。
> 顺着写提示文字的过程，又查出白名单里**有 11 个键没有任何代码读**。

### 一、两把高德 Key 分家

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| 69 | 面板新增「前端专用配置（在这里只读）」 | ✅ | 列出 `frontend/.env.local` 里那几个变量**配没配**、从哪个文件读到的（`.env.local` 优先）；后端只报存在性、**不返回值** |
| 70 | `AMAP_WEB_KEY` / `MAP_PROVIDER` 的说明改成明确分工 | ✅ | 一个说「只有后端路由用它，另一把在 `frontend/.env.local`」，一个说「与结果页那张地图无关」；两条都有断言钉住 |

**误读是怎么发生的**：面板只写仓库根的 `.env`（后端进程读它），
而 Next **只读 `frontend/.env*`** —— 前端那两个变量在面板上从来没地方显示，
「未配置」三个字属于**另一把 Key**。结论：**「看不见」被当成了「没配」。**

前端侧解析快照时**逐字段重建**，不做对象展开：后端若多回一个 `value`，也不会被带进界面
（测试验证：往条目里塞一把假 Key，断言它不出现在结果里）。

### 二、每个字段一个圈感叹号

| # | 任务 | 状态 | 说明 |
| --- | --- | --- | --- |
| 71 | ⓘ 悬停/聚焦/点击显示用途 | ✅ | 提示常驻 DOM（`role="tooltip"` + 控件的 `aria-describedby`）：鼠标、键盘、屏幕阅读器、Ctrl+F 都是同一段字 |
| 72 | ⓘ **放在 `<label>` 外面** | ✅ | `<label>` 里的可交互元素会被算进控件的无障碍名字 —— 屏幕阅读器会把输入框读成「…DEEPSEEK_API_KEY 字段说明」，同一个字段也会查出两个 label（先写错的版本被现有用例拓出来了） |

文字只有一个出处：后端 `ENV_FIELDS` 的 `hint`，界面不另抄一份。

### 三、★ 11 个改了不会改变任何行为的旋钮 ★

写提示字时被迫逐个回答「它到底影响什么」，于是发现，并**逐个处置**（这一轮做完了）：

| 旋钮 | 处置 | 理由 |
| --- | --- | --- |
| `PLAN_` / `SEARCH_COST_CIRCUIT_BREAKER_CNY` | **移除** | 熔断器读 `limits.yaml`；而且其中一个曾经在 `/health` 上说谎（#73） |
| `RATE_LIMIT_COLD_PLANS_PER_DAY` | **移除** | 真正的限流值在 `limits.yaml` |
| `MAP_MAX_CALLS_PER_PLAN` | **移除** | 真正的按次数熔断在 `limits.yaml` |
| `BACKEND_PORT` / `API_BASE_URL` | **移除** | 实际端口由 `uvicorn --port`；CORS 用 `FRONTEND_URL` |
| `GLOBAL_DAILY_BUDGET_CNY` | **接上** | 以前没人读，现在规划链真的按它熔断（#75） |
| `SERPER` / `BING` 的 Key、`ENABLE_LOCAL_FETCH`、`NOMINATIM_USER_AGENT` | **保留并标注** | 已规划未实现：ⓘ 写「改了不生效」，`/health` 还会点出被忽略的搜索 Key |

> 移除的理由不是“嫌它们没用”，而是**它们的事务所在处本来就在别处**：
> `config/limits.yaml`，而面板本来就能编辑它（同样先校验后落盘）。
> 同一个数字有两个入口，早晚会漂。

| # | 问题 | 严重度 | 修法 |
| --- | --- | --- | --- |
| 73 | **`/health` 报的熔断阈值不是真正在生效的那个**：它读 `Settings` 的镜像变量，而熔断器读 `config/limits.yaml`（`plan_service` 里 `breaker=limits.cost.circuit_breaker`）。把 `PLAN_COST_CIRCUIT_BREAKER_CNY` 改成 99，`/health` 会显示「99 元熔断」而请求仍在 **1 元**处被拦下。两边默认值恰好相等（1.0 / 0.30）才一直没被发现 | 🔴 高（诚实性：可观测界面上的谎） | `/health` 改报 `limits.yaml` 里真正生效的两个值；回归测试把环境变量改成 99，断言 health 报的仍是 limits.yaml |
| 74 | **面板上能改、但没人读的键没有任何标记**：以前只盯「Secret 会不会泄到浏览器」，没人问过「改了到底有没有用」 | 🟡 中（诚实性） | `INEFFECTIVE_ENV_KEYS` 显式登记；三道测试卡住（见下），另加一条锁住“镜像键不许回到面板” |
| 75 | **全局日成本是一句空话**：`limits.yaml` 写着 `cost.global_daily_cny: 20.00`（PRD §15.4 第四级熔断：超限 ⇒ 进缓存优先模式），但**没有任何代码读它**。`CostLedger` 的熔断器一次规划一份，只看本次 | 🟡 中（成本兜底缺失） | `CostStore.daily_budget()` 按 UTC 日历日聚合；判定放在**建链之前**（链一跑钱就花了）；超限时不调模型、只走本地与缓存；降级理由带上“花了多少/上限多少”；`/health` 新增 `cost` 区块；`limit <= 0` = 不限制；边界取“到线即用完” |

> 方法上的老规矩再验证一次：四条新守卫都做了**变异验证** ——
> 把 `/health` 改回镜像变量、从声明表里拿掉 `BACKEND_PORT`、删掉某句提示、
> 关掉规划链里那行预算判定，四种情况下对应用例都真的变红。

**关于“查不到”的边界**：库读不出来时 `/health` 报的 `budget_exceeded` 是 **`null` 而不是 `false`**。
报 `false` 等于向运维保证“还没超预算”，而那是一句没有根据的话 —— 同「null ≠ 0」。
有一条用例专门用会抛错的 `CostStore` 钉子这个行为。

### 本轮门槛

| 项 | 结果 |
| --- | --- |
| 后端测试 | 1013 → **1021 passed**（+8：日预算纯函数 3 · 日均聚合 1 · `/health` 成本区块 2 · 规划链阀门 1 · 镜像键不许回归面板 1；含 3 条 Tavily 活体，不配 Key 时如实 skip） |
| 前端测试 | 284 → **292 passed**（新增 8：`dev-settings` 5 · `dev-api` 3） |
| 前端覆盖率 | statements **98.86%** / branches **88.95%** / functions 95.67%（闸门 96 / 84 / 89） |
| 静态检查 | ruff 0 问题；mypy strict 119 文件 0 错；`pnpm lint` / `typecheck` / `test` 全绿 |
| 真实运行 | 起前后端 + 无头 Chrome 打 `/dev`：字段的 ⓘ 与 tooltip 一一对应（说明全部常驻 DOM）、「前端专用配置」栏里 `NEXT_PUBLIC_AMAP_JS_KEY = 已配置 · frontend/.env.local`、页面与接口响应里都没有明文 Key；`/health` 报的 `cost_breakers` 与 `limits.yaml` 逐字符一致（1.0 / 0.3） |

---

## M5 补充（2026-09-15）：对比视图 · 行程自己的地址 · 「复制这套路线」

> 这一轮把 M5 剩下的三个缺口一次补完（剩下的是分享页的 OG 图 / JSON-LD）：
> 三套方案只有并列卡片（没法交叉对比）、行程只在首页表单下方渲染（刷新即空白）、
> 后端的复制接口没有入口（一个有接口但没人能点的功能等于没有）。

### 交付

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M5-6 | 对比表 `components/route-compare.tsx`（纯函数 `routeCompare` + 渲染） | ✅ | 9 个对比项 × N 套方案（名称/地点/总时长/步行/交通/预算/适合/需要留意/校验结果） |
| M5-7 | 接入 `TripResultView`（卡片列表上方） | ✅ | 首页结果面板与分享页 `/t/{slug}` 同时生效 —— 两处共用一个渲染层，不会其中一个忘了 |
| M5-8 | 格式化口径合并进 `lib/format.ts` | ✅ | `formatAmount` / `formatBudget` 从 `trip-result.tsx` 移入；新增 `formatTransit`，卡片与对比表**调用同一批函数**（同一个数字不会在两处长成两个样子） |
| M5-9 | 行程自己的页面 `/trip/{id}` | ✅ | `app/trip/[id]/page.tsx`：刷新 / 收藏 / 复制地址都停在同一版；旧版地址仍可打开（版本链在后端） |
| M5-10 | 分享页的「复制这套路线」 | ✅ | `components/copy-trip-button.tsx` → `copyPublicTrip()`（后端 `POST /public/trips/{slug}/copy`，早就可用但前端没有入口）；复制成功直接带去 `/trip/{id}` |
| M5-11 | 两种挂载方式一份实现（`TripWorkspace` 的 `mode`） | ✅ | `inline`（首页内嵌）不碰地址栏、只给「在新页面打开 / 复制地址」；`page`（`/trip/{id}`）改路线/撤销后同步地址 |
| M5-12 | 测试 | ✅ | 新增 33 条（对比表 14 · `formatTransit` 4 · 复制按钮 5 · 行程页 4 · 地址同步与复制 `trip-workspace` +6）前端 292 → **329** passed |

### 四条纪律（写在组件头部，这里只记理由）

1. **只对齐，不推荐**：不排名、不出现 `recommend_score` —— 分数来自 7 维权重 + 乘数，
   一张表解释不了它，放出来只会退化成「按分数选」；
2. **不知道就说不知道**：未知 / 未给出 / 未列出三种说法分开，并有一条测试**显式禁止**
   `—`、`-`、`无` 与空白单元格 —— 占位符比「未知」更容易被当成正常值；
3. **没校验过不许说通过**：`feasible === false` 但 `violations` 为空时仍报「未通过校验」；
   `feasible` 不是 `true` 时报「校验结论未给出」（这两种形态最容易被顺手糊过去）；
4. **少于 2 套方案就不画表**：一张只有一列的表会让人以为其余方案被排除了。

列的配对不靠下标：每个单元格自带 `routeId`，渲染时按 id 做 key ——
「第几列是哪套方案」由构造保证，而不是靠注释提醒「别搞错顺序」。

### 顺手修掉的一个真问题：交通那一格会把有数据的一半也丢掉

卡片原来的写法是 `transit_time_min === null ? "未知" : …` ——「有时长没距离」或
「有距离没时长」时整格都变成「未知」，其实数据有一半。抽成 `formatTransit(minutes, meters)`，
两端各自判断（`null, 1200` → 「时长未知 · 1.2 km」），卡片与对比表都改用它。

### 行程自己的地址：三个选择与它们的代价

1. **首页不自动跳转，保持内嵌 + 两个入口**。代价是首页刷新仍然回到空白表单，
   换来的是：提交后立刻看到结果与「这次模型干了什么」（`meta.llm` 只在
   `POST /trips:plan` 的响应里，`GET /trips/{id}` 的 meta 没有它 —— 一跳转就丢）。
2. **`/trip/{id}` 用 `history.replaceState` 同步地址，不用 `router.replace`**。
   后者会把这一页整个重新挂载（再拉一次行程，并把刚写下的「已生成第 N 版」清掉），
   而我们要的只是「刷新时落在同一版」。Next 官方支持用原生 History API 改地址而不触发导航。
3. **头部默认不再高亮「首页」**（`SiteHeader` 的 `current` 默认值从 `"home"` 改成 `null`）。
   `aria-current="page"` 是给屏幕阅读器说的「你现在就在这里」，分享页与行程页都不是首页 ——
   默认指向首页等于说了句假话；除分享页外，其余页面本来就都显式传了 `current`。

### 本轮门槛

| 项 | 结果 |
| --- | --- |
| 前端测试 | 292 → **329 passed**（28 个文件全绿） |
| 前端覆盖率 | statements **98.94%** / branches **89.31%** / functions **96.15%**（闸门 96 / 84 / 89）；新文件 `route-compare.tsx` 100% / 89.13%、`copy-trip-button.tsx` 100% / 90.9%、`app/trip/[id]/page.tsx` 100% |
| 静态检查 | `pnpm lint` / `typecheck` / `build` 全绿（多出 `/trip/[id]` 一条动态路由） |
| 后端 | 未改动（本轮只动前端；复制接口本来就在） |

### 刻意没做的事

- **不显示评分、不做排名**（理由见纪律 1）；也没有「一键选最优」这类按钮 ——
  替用户做决定的依据不在这一层；
- **没做移动端专属布局**：对比表在窄屏是横向滚动（`min-w-[560px]` + `overflow-x-auto`），
  4 列在 375px 下要滑一下才能看全；
- **首页不自动跳 `/trip/{id}`**（理由见上）；
- **没把 `meta.llm` 搬进 `GET /trips/{id}`**：那需要后端存下每次规划的模型用量并回传，
  属另一个改动；这也是「首页内嵌」这条路必须保留的原因；
- **没做分享页的 OG 图 / JSON-LD**：M5 最后一项。
### M5 收尾（2026-09-15）：分享页 OG 图 + JSON-LD（AC-9.5）· 冒烟捞出一个后端真 bug

> M5 的最后一项。与前几轮同一套做法：探针先行（`next/og` + `sharp` 在本机
> 能否真出图、SVG 内联、CJK 字形），再实现，再真实冒烟 ——
> 而冒烟照例捞出了单测抓不到的东西：一个后端会话污染 bug 和三个 satori 渲染规则。

| # | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| M5-13 | 动态 OG 图 `app/t/[slug]/opengraph-image.tsx`（Next 文件约定） | ✅ | 1200×630 PNG；行程卡（品牌行 + 标题 + 站序示意图 + 方案摘要）与失效空卡两种形态；`force-dynamic` 与页面同一选择（取消分享必须立刻失效） |
| M5-14 | JSON-LD `schema.org/TouristTrip` | ✅ | `lib/og.ts` 纯函数：没拿到的字段不写（无 itinerary 时整键不出现、时间缺的站只写站名、>8 站只列前 8）；读不到行程时整个 `<script>` 不输出 |
| M5-15 | `metadataBase`（`lib/site.ts` 唯一出处 + `SITE_URL`） | ✅ | 服务端专用 env（不占浏览器可见白名单）；缺省落 `localhost:3000`；生产不设则社交预览拿到的是 localhost 链接 —— 模板里写明了这个后果 |
| M5-16 | 冒烟捞出的后端真 bug：**`put_llm` 撞唯一键污染整个会话** | ✅ 已修 | 两个并发规划算出同一个 prompt 时，后写方撞 `llm_cache.cache_key` 唯一约束；上层 `except` 吞得掉异常，**吞不掉已污染的 session**，本次规划随后必然 500 —— 与注释"缓存写失败不影响本次结果"直接矛盾。修法：先查再插（撞键保留原行刷新 `last_hit_at`），配集成回归测试 |
| M5-17 | 三个 satori 渲染规则（都是冒烟报错教会我们的） | ✅ | ① SVG 内 `<text>` 不支持（"convert them to <path>"）→ 站点编号改 HTML 绝对定位叠加；② 多子节点的 `<div>` 必须显式 `display: flex`（含单文本子节点也要求显式）；③ 页面 metadata 里手写 `images` 数组会**顶掉** opengraph-image 文件约定的自动填充（og:image 从 head 里消失，冒烟抓到过这个回归）→ 不写 images，让文件约定自己说话 |

**探针先行**（本机 macOS）：`ImageResponse` 不带自定义字体时 CJK 交由系统字体解析，
真实出图验证过（两种字形渲染出不同 PNG，同输入幂等）；`sharp` 由 devDep 移入 dependencies
（`next start` 生产链路真用它渲染 OG 图）。

**真实冒烟（前后端 + 真实知识库 1622 地点）**：规划 → 分享 → 抓分享页 HTML：

| 检查 | 结果 |
| --- | --- |
| `og:image` / `og:image:width` / `og:image:height` / `og:image:alt` | 全部出现，指向 `/t/{slug}/opengraph-image` |
| `twitter:card` / `twitter:image` | `summary_large_image`（文件约定自动带出）+ 同一图片地址 |
| JSON-LD | `@type: TouristTrip`，itinerary 三站带时间窗，`url` 指向分享页 |
| OG 图本体 | 行程卡 44717 bytes / 空卡 38626 bytes，**两图像素差异 14.5%**（不是同一张图缓存串了） |
| 失效 slug 页面 | `share-unavailable` + `<meta name="robots" content="noindex, nofollow"/>`，OG 图是空卡 |
| 未知 slug 的 OG 图 | 200 PNG（真话空卡），绝不画编造的行程 |

### 本轮门槛

| 项 | 结果 |
| --- | --- |
| 后端测试 | 1021 → **1022** passed（+1：重复写 llm_cache 不污染会话的回归）；覆盖率 95.52%（闸门 93%）；mypy strict 119 文件 0 错 |
| 前端测试 | 329 → **367** passed（+38：og 纯函数 26 · og-card 渲染 5 · og-image 路由 3 · 分享页 JSON-LD/社交 metadata 4） |
| 前端覆盖率 | statements **99.03%** / branches 89.99% / functions 96.42%；新文件 `og.ts` / `site.ts` 100%，`og-card.tsx` 100%/96.3%，`opengraph-image.tsx` 100% |
| 静态检查 | ruff 0；mypy 0；`pnpm lint` / `typecheck` / `build` 全绿（多出 `/t/[slug]/opengraph-image` 一条动态路由） |

### 刻意没做的事

- **OG 卡不放 `recommend_score`**：一张图解释不了 7 维权重 + 乘数（同对比表纪律 1）；
- **没做自定义字体加载**：缺字体环境的 CJK 会退到系统字体；兜底措辞是数字与 ASCII，仍可读；
  要完全可控得在 `ImageResponse` 里挂 Noto Sans SC 子集，留待有真实部署需求时再做；
- **JSON-LD 只写拿得到的字段**：`offers`（预算）没写 —— `budget_estimated` 的估算值
  进结构化数据等于冒充官方票价，宁可缺。

---

## 下一步（M5 起）

| 里程碑 | 内容 | 前置条件 |
| --- | --- | --- |
| M5 | ~~前端结果页：生成页（SSE 进度）/ 方案 Tab / 地图 / 时间线 / 对比 / Diff / 分享页（SSR+OG+JSON-LD）~~ | **✅ 全部完成（2026-09-15）** |
| M6 | 补齐 B1/B2 数据缺口、关系图扩量、知识库扩至全类别达标 | Overpass 恢复 |
| M7 | E2E（Playwright 16 场景）、异常注入 25 项、性能与安全检查（含 p95） | M5 ✅ |
| M8 | 交付报告（PRD 第四十节 12 小节） | 全部 |
