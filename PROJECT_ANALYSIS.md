# PROJECT_ANALYSIS.md — 现状分析与决策记录

> 项目代号：**TripDecider（中文名：路线决策器）**
> 分析时间：2026-09-10 12:23 CST
> 分析人：AI Agent（产品经理 / 全栈工程师 / 测试工程师 视角）
> 目标：判断这是「在已有项目上迭代」还是「从 0 开始」，并锁定会影响架构的环境事实。

---

## 1. 工作目录扫描结果

工作目录：`/Users/tanjiaxi/coding`

```
/Users/tanjiaxi/coding
├── .DS_Store
├── CareerPulse/     Python 项目（pyproject.toml + uv.lock + Dockerfile + docker-compose.yml + tests + docs）
├── githubstar/      Python 单文件服务（server.py + static/ + snapshots.db，SQLite）
└── mr-jobs/         Python 项目（main.py + service/ + adapters/ + scheduler.py + pytest.ini + tests）
```

**结论：不存在任何旅游/路线规划相关项目。**

| 判断项 | 结果 |
| --- | --- |
| 是否已有可迭代的相关项目 | ❌ 无 |
| 是否需要「先理解现有项目再决定改/重构」 | ⚠️ 已做技术栈级探测，无需逐文件审阅（与本次目标无关） |
| 是否为空项目 | ✅ 是，**从 MVP 架构直接开始** |
| 新项目落位 | `/Users/tanjiaxi/coding/ai-trip-decider/`（新建，不污染现有目录） |

### 1.1 现有项目的「风格指纹」（仅作技术偏好参考，不复制代码）

- 三个项目全部是 **Python 后端**，其中两个是完整工程（`pyproject.toml`/`pytest.ini`/`docs/`/`tests/`）。
- `CareerPulse` 使用 **uv** 管理依赖（存在 `uv.lock`）→ 说明你已习惯 uv 工作流，新项目应沿用。
- 三个项目都带 `docs/` 与测试目录 → 说明你重视文档与测试，本项目的文档/测试要求与之一致。
- 无 Node 项目先例，但有 `*.mjs` 脚本（Playwright 截图/录屏）→ 说明 Playwright 你是用过的。

---

## 2. 环境事实探测（这些直接决定技术方案能不能跑）

| 项目 | 探测结果 | 对架构的影响 |
| --- | --- | --- |
| Node.js | `v26.5.0` | ✅ Next.js 15 可用 |
| pnpm | `11.21.0` | ✅ 前端包管理用 pnpm |
| npm | `11.17.0` | ✅ 备用 |
| Python | `python3` = **3.9.6**（系统版）<br>`python3.10` = 3.10.20 | ⚠️ 系统 python3 **过旧**，不能直接用 |
| uv | `0.11.28`（`~/.local/bin/uv`） | ✅ **用 uv 固定 Python 3.12**，彻底绕开 3.9 问题 |
| Docker | CLI 存在，但 **daemon 未运行**（`docker info` 失败） | 🔴 **docker-compose 起 Postgres 目前不可行** |
| PostgreSQL | **Homebrew postgresql@16 已安装且服务已启动**<br>`PostgreSQL 16.14`，`127.0.0.1:5432` 可连，本地 trust 认证 | ✅ **直接用本机实例开发**，同时仍提供 `docker-compose.yml` 作为可复现路径 |
| 可用 PG 扩展 | `pg_trgm`, `unaccent`, `btree_gist`, `cube` | ⚠️ **无 PostGIS**（地理计算改用 bbox + haversine SQL）<br>⚠️ **无 pgvector / pg_bigm / zhparser**（实体去重改用别名+规则+模糊匹配+LLM 裁判） |
| Playwright 浏览器 | `chromium-1234`、`chromium_headless_shell-1234`、`ffmpeg-1011` 已缓存 | ✅ E2E 无需重新下载浏览器 |
| Git | 工作目录**不是** git 仓库 | ⚠️ 新项目需 `git init` |
| 现有数据库 | 已有 14 个无关业务库（ksd_*/codex_* 等） | ⚠️ 必须用独立库 `tripdecider_dev`，禁止动其他库 |
| 环境变量里的 API Key | `LLM / 搜索 / 地图` 相关环境变量**全部为空** | 按你的选择：LLM Key 你会提供；搜索/地图走**降级实现**（见 D-4 / D-5） |

---

## 3. 架构决策记录（ADR）

> 你已确认的方向用 ✅ 标注；因环境事实必须偏离的用 ⚠️ 标注，并给出理由与回退方案。

### D-1 ✅ 技术栈：Next.js 前端 + Python FastAPI 后端

- 前端：**Next.js 15（App Router）+ TypeScript + Tailwind CSS + shadcn/ui**
- 后端：**Python 3.12 + FastAPI + Pydantic v2**
- 理由：你明确选择；且你现有全部项目都是 Python 系，长期维护成本最低；AI/搜索/抓取生态在 Python 侧最成熟。
- 代价（必须管理）：两个进程、两套部署、类型不共享 → 用 **OpenAPI 自动生成 TS 客户端**（`openapi-typescript`）消除前后端类型漂移。

### D-2 ⚠️ ORM：Prisma → **SQLAlchemy 2.0 + Alembic**

- 你选择的是「Postgres + Prisma」。但 Prisma 是 **Node/TypeScript ORM**，而 D-1 确定后端是 Python。
- 决策：**保留 Postgres + 迁移文件即事实源的理念**，把 Prisma 换成 Python 生态的等价物：**SQLAlchemy 2.0（typed ORM）+ Alembic（迁移）+ Pydantic v2（schema）**。
- 不选「Prisma 作为独立 schema 层 + Python 复用」的原因：会引入 Node 运行时做数据库迁移，双语言 schema 同步成本高，违反「简单、可长期维护」原则。
- 回退方案：后续若要上 Supabase，Alembic 迁移可直接对其 Postgres 执行，无需改造（**Supabase 兼容层保留**）。

### D-3 ⚠️ 数据库运行方式：Docker Compose → **本机 Postgres 16 为主 + compose 为辅**

- 事实：Docker daemon **当前未运行**，无法 `docker compose up`。
- 决策：
  - **主路径（开发/测试）**：使用本机已运行的 Homebrew `postgresql@16`，独立库 `tripdecider_dev`。
  - **辅助路径（可复现/交付）**：仍提供 `docker-compose.yml`（Postgres 16 + 同版本扩展），`Makefile` 里 `make db-up` 一键切换；CI 用 compose。
- 影响：所有连接串只从 `DATABASE_URL` 读，两条路径完全等价。
- 不选 SQLite 的原因：全文检索/并发/JSONB 能力弱，且与你已选的 Postgres 方向冲突。

### D-4 ⚠️ 地图：高德 → **MapProvider 抽象 + OSM/OSRM 无 Key 降级**（高德可热插拔）

- 事实：你未提供地图 Key；但「距离/交通时间」是路线可行性的**硬依赖**，不能没有。
- 决策（三级降级，全部走统一 `MapProvider` 接口）：
  1. **`OsrmProvider`（默认，无需 Key）**：`OSRM` 开放路由服务算真实路网距离/步行/骑行时间 + `Nominatim` 地理编码 + `OSM` 瓦片渲染地图。
  2. **`AmapProvider`（Key 到位即启用）**：国内路网/公交时间更准，替换只需改环境变量 `MAP_PROVIDER=amap`。
  3. **`HaversineProvider`（离线兜底）**：纯球面距离 × 绕路系数 1.3，标记 `estimated`，UI 明确提示「交通时间为估算」。
- **红线**：任何非真实来源的距离/时间，必须带 `source=estimated` 标记并在 UI 显示，**不允许伪装成精确值**。
- 渲染：MVP 用 **MapLibre GL JS（无 Key）**；高德 JS API 作为可选渲染器。

### D-5 ⚠️ 搜索：Tavily/Serper → **SearchProvider 抽象 + 无 Key 降级链路**

- 事实：你未提供搜索 Key。
- 决策：
  - 抽象 `SearchProvider`（`search / extract / summarize / verify`），实现：`TavilyProvider`、`SerperProvider`、`BingProvider`（Key 到位即用）。
  - 无 Key 时启用 **`SeedOnlyProvider`**：不联网，只从本地知识库 + 已导入的 `travel_sources` 取事实。
  - 可选开发用 **`LocalFetchProvider`**：仅抓取**域名白名单**（政府/官方文旅/维基）的公开页面，遵守 robots.txt，默认 **关闭**，需显式 `ENABLE_LOCAL_FETCH=true`。
- 因此：**MVP 在没有搜索 Key 的情况下也能完整跑通全链路**，只是「兜底发现新地点」的能力受限 —— 与你的成本控制原则一致（本地优先，联网是例外）。

### D-6 ✅ 数据初始化：AI 构建结构化真实广州地点集

- 方式：AI 生成 **结构化候选 → 每条带来源 URL + 置信度 + 校验状态**，不可靠字段写 `unknown`，**禁止编造营业时间/价格**。
- 规模目标：`≥200` 地点、`≥30` 路线；采用**分批 + 自检 + 抽样人工复核**流程（详见 PRD 第 9 章）。
- 质量红线：宁可 `unknown`，不可猜测。任何 `verified` 状态必须有可访问的来源 URL。

### D-7 ⚠️ 向量检索：pgvector 不可用 → **别名 + 规则 + 模糊匹配 + LLM 裁判**

- 事实：本机 PG 无 `pgvector`、无 `pg_bigm/zhparser`（中文分词不可用）。
- 决策：实体匹配采用**多级流水线**（normalize → 精确/别名 → pg_trgm + rapidfuzz 相似度 → 地理围栏 → LLM 裁判），而非 embedding 相似度。
- 好处：**更便宜**（LLM 判定只在边界 case 触发）、可解释、可测试。

### D-8 ⚠️ LLM 供应商：需你最终拍板（默认 DeepSeek）

- 你选择了「LLM API 任一」，但未指定具体厂商。
- 默认实现：**DeepSeek**（成本最低，中文旅游场景表现足够），通过 `LLMProvider` 抽象支持 OpenAI / Anthropic 热切换。
- 待确认项见 PRD 第 29 章 Open Questions。

---

## 4. 由此产出的架构约束清单（写代码时必须遵守）

1. **一切外部能力（LLM / 搜索 / 地图 / 天气）都必须是接口 + 多实现 + 可降级**，业务逻辑禁止 import 具体 SDK。
2. **本机 Postgres 已运行** → 开发脚本不得假设能起 Docker；但必须同时维护 compose 文件。
3. **API Key 只从环境变量读**，前端只能通过后端代理访问第三方；`.env` 必须进 `.gitignore`。
4. **无 Key 也必须能端到端跑通** → 服务启动时检测 Key 并打印「降级模式清单」，UI 显示数据可信度等级。
5. **距离/时间必须有来源标记**（`amap | osrm | estimated`），`estimated` 必须在 UI 可见。
6. **Python 版本用 uv 固定为 3.12**，禁止依赖系统 `python3`。
7. **独立数据库 `tripdecider_dev` / `tripdecider_test`**，不得触碰现有 14 个业务库。
8. **中文分词/向量检索不可用** → 搜索与去重方案必须避开这两个依赖。

---

## 5. 下一份文档

本文件只做「现状 + 决策」。产品需求、数据模型、算法、成本模型、测试计划见：

- `PRD.md`（含：信息架构、功能需求、数据模型、评分算法、成本模型 COST_MODEL、测试计划 TEST_PLAN、安全、里程碑 TASKS）
