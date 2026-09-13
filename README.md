# TripDecider

> **AI 旅行路线决策器** —— 我替你研究、筛选、组合并**验证**旅行路线，而不是丢给你一篇攻略。

用户只回答 6 个问题（目的地 / 天数 / 人数 / 偏好 / 预算 / 节奏），产品输出 **2–3 套经过可行性校验、可比价、可继续自然语言修改**的路线方案。

首个城市：**广州**。

---

## 当前状态

| 里程碑 | 状态 |
| --- | --- |
| M0 脚手架（仓库/配置/前后端骨架/迁移） | ✅ 完成 |
| M1 广州知识库（≥200 地点 / ≥30 路线 + 质检） | ✅ 完成（1 622 地点 / 44 路线模板） |
| M2 领域内核（意图/候选/评分/可行性/组合） | ✅ 完成 |
| M3 Provider 与检索链（LLM/搜索/地图降级） | ✅ 完成（无 Key 也能跑，全部自动降级） |
| M4 规划编排与 API（含 LLM 接入与可观测） | ✅ 完成 |
| M5 前端（首页/结果页/地图/修改/分享） | 🚧 进行中（首页表单已能真实提交并显示模型使用情况；结果页/地图/分享页未做） |
| M6 知识库扩量与质检 | ⏳ 待开始 |
| M7 测试强化与迭代（含 E2E、异常注入、p95） | ⏳ 待开始 |

完整需求见 [`PRD.md`](./PRD.md)，架构决策见 [`PROJECT_ANALYSIS.md`](./PROJECT_ANALYSIS.md)，
**跑起来 / 排障 / 当前能干什么**见 [`RUNNING.md`](./RUNNING.md)，进度明细与问题记录见 [`TASKS.md`](./TASKS.md)。

---

## 快速开始

### 前置条件

- **Python 3.12**（用 `uv python install 3.12`，不要用系统 python3）
- **Node.js ≥ 20** + **pnpm**
- **PostgreSQL 16**（本机：`brew services start postgresql@16`；⚠️ `make db-up` 需要 Docker，但仓库里**还没有 `docker-compose.yml`**，目前请用本机 Postgres）

### 一键初始化

```bash
make setup     # 安装依赖 + 建库 + 迁移 + 生成 .env
make dev       # 后端 http://127.0.0.1:8000/docs   前端 http://localhost:3000
```

### 常用命令

```bash
make help          # 查看全部命令
make seed          # 灌入广州知识库（幂等）
make validate      # 知识库质检（不达标非零退出）
make test          # 单元 + 契约 + 集成 + 前端单测
make check         # 提交前必跑：lint + 类型 + 前后端覆盖率闸门 + 测试
make test-cov      # 后端带覆盖率（低于 COV_MIN=93 会非零退出）
make test-frontend-cov  # 前端带覆盖率（阈值见 frontend/vitest.config.ts）
make todo          # 扫描 TODO/FIXME/console.error
make health        # 查看后端健康状态与降级模式
```

---

## 怎么用（当前阶段 = M0–M4 + M5 起步）

已经能端到端跑通的是**后端**：`POST /api/v1/trips:plan` 会真的产出 2–3 套经过可行性校验、
可比价、可继续修改的路线方案（配了 Key 时还会真的调模型写文案）。
前端目前只能到"提交 + 看进度 + 看到模型到底用了没有"，**结果页（时段表/地图/Diff/分享）仍在 M5**。

### 一、启动

```bash
make setup     # 首次：装依赖 + 建库 + 迁移 + 生成 .env（约 2–3 分钟）
make dev       # 启动后端(:8000) 与前端(:3000)
```

前置条件只有两个：**Postgres 在跑**（`brew services start postgresql@16`）与 **依赖已安装**。
不需要任何 API Key —— 缺 Key 时所有能力自动降级，并在界面上如实标注（见下）。

启动后打开 **http://localhost:3000**。

### 二、五个入口

#### 1. 首页 `http://localhost:3000`

- 顶部状态条会显示**真实的后端连接状态与知识库规模**（地点数、路线数、版本号），
  以及当前处于哪些降级模式（例如「未配置 LLM：将使用规则引擎」）。
  后端没启动时显示「未连接到后端服务 · 运行 make dev-backend」，而**不是**空白或假数据。
- 「规划卡」可以完整填写旅行需求（目的地/天数/人数/偏好/节奏/预算/补充要求），
  全部有默认值，**不填也能提交**。
- 点「开始规划」会真的调用 `POST /api/v1/trips:plan`，然后**订阅 SSE 进度流**并把阶段进度显示出来（不是假进度条），
  完成后显示方案套数与**这次到底谁是执行者**：模型做了哪些任务（意图补全/方案文案）、是否命中缓存、用了多少 token、
  金额与**这个金额是否已校准**、以及任何降级原因。
- 失败时不会假装成功：错误码、`message`、`hint`、`request_id` 与真实请求体都会展示出来。
  （提交曾因「发中文标签」而稳定 422 —— 已于 2026-09-13 修好，并加了一条读后端配置来对照的契约测试；
  见 [`RUNNING.md` §5.2](./RUNNING.md)。）

#### 2. 知识库浏览页 `http://localhost:3000/explore/guangzhou` ← 当前最值得看的一页

这是"知识库到底有什么"的可视化入口，也是 M1 交付物的验收面：

- **概况条**：地点总数、身份可交叉核验数、营业时间已知比例。每一格下面都解释了怎么读它。
- **类别筛选**：按餐饮/历史人文/景点/博物馆/自然… 过滤，带每类数量。
- **搜索**：支持**别名** —— 输入「小蛮腰」能找到广州塔，输入「太古仓」能找到 OSM 里叫「太古仓码头」的地点。
- **地点卡片**：建议停留、营业时间、标签、热度分值（并标注该分值是「人工校准」还是「规则推导」）、
  来源链接（可点回 OpenStreetMap 原始条目）。
- **⚠ 标记**：营业时间未知 / 票价未知的地点会明确标出 —— 这是产品的诚实性要求，
  数据缺口不隐藏。
- **路线模板区**：44 条模板，展示站点顺序、停留时长、站间交通方式，并标注
  「时长与距离为估算值」「预算未估算」。

#### 3. API（也想给程序用）

```bash
# 城市与数据规模
curl -s localhost:8000/api/v1/cities | python3 -m json.tool

# 类别分布与可信度分布
curl -s localhost:8000/api/v1/cities/guangzhou/stats | python3 -m json.tool

# 搜索（支持别名）
curl -s "localhost:8000/api/v1/cities/guangzhou/places?q=小蛮腰" | python3 -m json.tool

# 按类别筛选 + 分页（单页上限 100）
curl -s "localhost:8000/api/v1/cities/guangzhou/places?category=museum&limit=5"

# 地点详情（含来源、别名、未知字段、质量标记）
curl -s localhost:8000/api/v1/places/<place_id> | python3 -m json.tool

# 路线模板（带站点明细）
curl -s "localhost:8000/api/v1/cities/guangzhou/routes?archetype=relaxed"

# 评分权重与偏好维度（排序规则不是黑箱）
curl -s localhost:8000/api/v1/meta/scoring-config | python3 -m json.tool

# 健康状态与降级模式
curl -s localhost:8000/api/v1/health | python3 -m json.tool
```

交互式文档：**http://127.0.0.1:8000/docs**

#### 4. 数据来源与免责 `http://localhost:3000/about/data`

每类数据从哪来、哪些不保证、输入去了哪里、怎么纠错。

#### 5. 开发设置 `http://localhost:3000/dev`（**不是用户功能**）

只在开发环境存在：一个页面改 LLM/搜索/地图的 Provider 与 Key、成本阈值、以及 `config/*.yaml`，
改完立即生效（YAML 先校验后落盘，失败一个字节都不写）。三道门把它挡在用户外面：
路由只在 `ENV=development` 注册、`ADMIN_TOKEN`（未设时仅本机）、白名单（`DATABASE_URL`/`SESSION_SECRET`/`ENV` 刻意不含）。
入口在 production 构建里**不渲染**，页面 `noindex`。细节见 [`RUNNING.md` §5.5](./RUNNING.md)。

### 三、重新生成数据（改规则后）

```bash
make fetch-osm      # 抓取 OSM 原始 POI（约 9 分钟，需网络）
make seed           # 幂等重建知识库（约 12 秒）
make validate       # 质检：门槛不达标直接非零退出
make relations      # 重算地点关系图（离线估算；OSRM=1 走真实路网）
make report         # 打印数据质量报告
```

改完 `config/seed.yaml`（过滤/评分规则）或 `backend/data/curated/*.yaml`（人工数据）后，
跑 `make seed && make validate` 即可看到影响。人工数据有 21 条秒级单测守着
（`make test-unit`），不必等 12 秒建库才发现 YAML 写错。

### 四、当前**不能**做什么（明确边界）

| 还不能 | 原因 | 计划 |
| --- | --- | --- |
| 在网页上看到路线方案（时段表/地图/对比/Diff） | 结果页未实现 | M5 |
| 在界面上用自然语言改路线 | 后端 `/revise` 已可用，但需要结果页来承载 Diff | M5 |
| 分享路线、看地图 | 同上 | M5 |
| 用大模型做**排序/打分** | ✅ 已接入意图补全与方案叙事（M4）；但**数字永远由代码算**，模型不参与排序与打分 | — |
| 联网搜索新地点 | L6/L8 不在规划路径上；且未配置搜索 Key 时是 `seed_only` | M6 |

**前端与后端已经能真实对话**：表单提交 → SSE 进度 → 方案数与模型使用情况。
缺的是把方案渲染出来（M5）。接口不会因为前端没跟上而返回假成功。

---

## 测试怎么跑（当前阶段）

当前是 **M0–M4**：后端有单元 / 契约 / 集成三层测试、前端有组件与页面测试，**E2E 尚未开始**（属 M7）。

### 一、四层测试与用途

| 层 | 命令 | 数量 | 单次耗时 | 依赖 | 用途 |
| --- | --- | --- | --- | --- | --- |
| 后端单元 | `make test-unit` | **791** | **~2s** | 无 | 纯逻辑：几何计算、丰富化管线、配置/Schema 校验、类别一致性、名称归一化、路径定位、意图/实体匹配、缓存键、成本计算、检索链、`llm_planner`（含降级路径） |
| 后端契约 | `make test-contract` | **43** | **~1s** | 无（respx 拦网络） | 四个 Provider 的 HTTP 契约：URL/参数/认证头、响应解析、超时与错误映射、**不把畸形响应当真实数据** |
| 后端集成 | `make test-integration` | **157** | **~10–20s**（每次重建测试库） | 测试库 + 原始数据 | schema 约束、API 契约、错误与故障路径、**查询次数（N+1 回归）**、知识库质量门槛、缓存与幂等、成本落库、限流窗口、LLM 可观测与真实模型活体测试（有 Key 才跑） |
| 前端 | `make test-frontend` | **189** | ~3s | 无 | 网络层 envelope/错误映射、格式化边界值、服务端组件渲染、表单默认值/校验/提交、SSE 进度流解析、`meta.llm` 展示、开发面板、`planTrip` 序列化 |
| 前端覆盖率 | `make test-frontend-cov` | **189** | ~5s | 无 | 同一批测试 + v8 覆盖率闸门（阈值见 `frontend/vitest.config.ts`） |
| 类型与风格 | `make typecheck` `make lint` | — | ~6s | 无 | mypy strict（117 文件）+ tsc + ruff + eslint |
| **全部** | **`make check`** | **991 + 189 + 前后端覆盖率闸门** | **~90s** | 测试库 | **提交前必跑（实测 exit=0）** |

**没有 E2E**：`make e2e` 目前会失败（`frontend/` 下还没有 Playwright 配置与依赖，属 M7）。
规划接口本身已经可用，但结果页未做，所以端到端流程暂时无从测起。

### 二、第一次跑集成测试会自动准备环境

`make test-integration` 会**自动**：
1. 把 `tripdecider_test` 迁移到 head
2. 若该库的地点少于 200 条，就用真实建库脚本灌一次（复用 `scripts/seed_guangzhou.py`，幂等）

所以**不需要**手工准备测试库。前提是两个：

```bash
brew services start postgresql@16   # 本机 Postgres 在跑
ls backend/data/raw/osm_guangzhou_raw.json   # 原始数据在（没有就 make fetch-osm）
```

现在**每次运行都会重建**（约 12 秒）。早期用的是「地点少于 200 才建库」，结果是数据规则改动后，集成测试继续跑在**过期数据**上并通过 ——
边界 bug 修完之后，测试库仍留着 2000 条邻市地点而测试全绿。用 12 秒换「测的一定是当前数据」是划算的。

若这两步不满足，会**直接失败并打印修复指令** —— 刻意的：跳过会让"集成测试通过"变成假象（早期版本就是在空库上全部 skip，看着很绿其实什么都没测）。

### 三、按需筛选（改哪测哪）

```bash
# 只跑某类逻辑（秒级反馈）
cd backend && PYTHONPATH=. uv run pytest -m unit -k "similarity or normalize" -q

# 只跑某个文件
cd backend && PYTHONPATH=. uv run pytest tests/unit/test_curated_data.py -q

# 只跑某个用例，并看详细输出
cd backend && PYTHONPATH=. uv run pytest tests/unit/test_config.py::test_weights_not_summing_to_one_fails_fast -vv

# 只跑知识库质量门槛
cd backend && PYTHONPATH=. uv run pytest -m integration -k knowledge -q

# 前端只跑某个文件
cd frontend && pnpm test --run components/__tests__/planner-form.test.tsx
```

⚠️ **不要在仓库根目录直接跑 `pytest`**：`app` 包靠 `backend/` 作为工作目录解析，根目录跑会 collection error。用 `make` 目标，或先 `cd backend && PYTHONPATH=. uv run pytest`。

### 四、改不同东西该跑什么

| 你改了什么 | 最少要跑 |
| --- | --- |
| `config/*.yaml`（权重/阈值/定价/TTL/种子规则） | `make test-unit`（配置 fail-fast 与人工数据校验都在这层） |
| `backend/app/domain/**`（纯逻辑） | `make test-unit`（`test_geo.py` / `test_enrichment.py` / `test_naming.py` 秒级反馈） |
| `backend/app/db/models.py` 或迁移 | `make test-integration` + `make migrate-check` |
| `backend/data/curated/*.yaml` | `make test-unit`（秒级验证 Schema 与自洽性），再 `make seed && make validate` |
| 建库/过滤规则 | `make seed` → `make validate` → `make test-integration` |
| `backend/app/main.py`（中间件/异常处理） | `make test-integration -k error_handling` |
| `backend/app/api/v1/catalog.py`（只读目录 API） | `make test-integration -k catalog`（含查询次数回归） |
| `backend/app/domain/categories.py` | `make test-unit -k categories` |
| `frontend/lib/api.ts`（网络层） | `make test-frontend`（`lib/__tests__/api.test.ts` 覆盖全部错误分支） |
| `frontend/lib/format.ts`（展示格式化） | `make test-frontend` |
| `frontend/app/explore/[city]/page.tsx` | `make test-frontend`（服务端组件直接 await 渲染） |
| 前端组件 | `make test-frontend` + `make typecheck` |
| 准备提交 | `make check` |

### 五、两个"不是测试但必须一起跑"的验收闸门

```bash
make validate   # 知识库质检：门槛不达标直接非零退出（当前 0 阻断项失败、4 项告警）
make todo       # 扫 TODO / FIXME / console.error / raise NotImplementedError
```

`make validate` 是**发布闸门**：它把"活跃地点 ≥200、路线 ≥30、坐标越界 =0、无来源未标记 =0、伪路线 =0"当作硬条件。它和测试的分工是：测试守"代码行为对不对"，`validate` 守"数据能不能对用户开放"。

### 六、当前覆盖率与它的诚实解读

```
TOTAL   5896 stmts   267 miss   95.47%（闸门 COV_MIN=93）
```

前端（`make test-frontend-cov`，v8）走同一套口径，但闸门单独设在 `frontend/vitest.config.ts`：

```
Statements   : 98.44% ( 2407/2445 )   Branches : 87.19%
Functions    : 93.75% ( 105/112 )     Lines    : 98.44%
```

`lib/`（纯逻辑：格式化、网络出口）是 **100%**。分支阈值明显低于语句阈值是**如实反映现状**
而不是放水：组件里的 JSX 分支（加载中 / 降级 / 空结果）很难穷尽。与其把阈值压低到刚好能过，
不如把差距写在明面上。

**但 95.5%（后端）/ 98.4%（前端）不等于"产品逻辑都测过了"**，三点必须说清楚：

1. **未覆盖的 267 行集中在领域层的分支细节与被刻意留空的路径**
   （`domain/feasibility.py`、`scoring.py`、`candidates.py`、`intent.py` 的边角分支），
   主体逻辑都测过；PRD 对 `domain/feasibility.py`、`domain/scoring.py` 要求
   ≥95% 行覆盖 / ≥90% 分支覆盖 —— 注意**这两份文件目前并不在 100% 名单里**，
   `feasibility.py` 实测 69%（未覆盖面主要是未启用开关组合下的早退路径），
   这是当前真实水平，不附会。
2. **Provider 层主要靠 respx 契约测试**："能正确地把错误降级"被测过了。
   真实调用则有两条额外的活体用例（`tests/integration/test_llm_live.py`，
   **有 `DEEPSEEK_API_KEY` 才跑、没 Key 自动 skip**）—— 它们就是用来防止
   "Key 失效无人发现"的；未配 Key 时这部分仍是空白，不是"已测过"。
3. **覆盖率数字曾经是失真的，现在修好了**（见下）。修之前 `make test-cov` 报 90%，
   而 `app/api/v1/catalog.py` 被报成 **54%** —— 实际是 98%。原因是覆盖率配置缺了
   `concurrency`，SQLAlchemy 的 async 引擎内部用 greenlet 切换上下文，
   而 coverage 默认只跟踪 thread，于是**所有 async 端点只统计到第一个 await 之前**。

   危害不在于"数字低了 5 个点"，而在于**真实未覆盖的分支会淹没在大量假阴性里**，
   覆盖率因此失去闸门意义。修复是在 `backend/pyproject.toml` 里加一行：

   ```toml
   [tool.coverage.run]
   concurrency = ["thread", "greenlet"]
   ```

   现在 `make test-cov` 带 `--cov-fail-under=93` 闸门（`COV_MIN` 可覆盖）。
   实际水平见上方（后端 95.47%），要收紧只需改 `Makefile` 里的 `COV_MIN`（例如 `make check COV_MIN=97`）。

前端覆盖率也踩过同一个坑：`vitest.config.ts` 的 `include` 原本漏了 `app/**`，于是页面里的
真实判断逻辑（空结果、故障分支、诚实性标注）**不计入统计**，数字看着很高却没有覆盖最难测的那部分。
现在 `app` 与 `components`、`lib` 一起计入，并带 `thresholds` 闸门。

---

## 无 Key 也能跑

这是本项目的硬性架构要求（PRD §15.5）：**缺少任何第三方 Key 都必须能端到端运行**。

| 能力 | 有 Key | 无 Key 降级 |
| --- | --- | --- |
| LLM（DeepSeek） | 意图补全（自由文本里规则没解析到的部分）+ 方案叙事（路线名与推荐理由） | 规则引擎 + 模板文案 |
| 搜索（Tavily 等） | 发现库外新地点、复验时效信息 | `seed_only`：只读本地知识库，不联网 |
| 地图（高德） | 精确路网与公交时间 | `OSRM` 真实路网（免 Key）→ `haversine` 离线估算 |
| 天气（open-meteo） | 雨天/高温路线适配 | 跳过天气适配 |

**红线**：任何非真实来源的距离/时间都会带 `source=estimated` 标记并在 UI 显示；任何缺失字段写 `NULL` 并记入 `unknown_fields`，**绝不填猜测值、绝不伪造来源**。

成本同理：单价只有回填了官方价目才算"已校准"（`config/pricing.yaml` 里逐个供应商标 `calibrated_at`），
未校准的金额**记 0 并标记未知**，绝不当成"免费"写进报表。DeepSeek 与 Tavily 已于 2026-09-13 校准，
高德仍未取到价目（见 `RUNNING.md` §7）。

### 大模型到底参与什么（M4 起已接入）

配了 `DEEPSEEK_API_KEY` 时，模型只做两件事：

1. **意图补全** —— 自由文本里规则引擎覆盖不到的部分（`ParseResult.unparsed`）经强校验 JSON schema 后合并进意图；
2. **方案叙事** —— 路线的名字与推荐理由。

三条边界写在 [`backend/app/services/llm_planner.py`](./backend/app/services/llm_planner.py) 的模块 docstring 里，每条都有测试守着：

- **模型不产生数字**：站数 / 时长 / 距离 / 预算全部由 `app/domain` 的纯函数算；文案里一旦出现数字或计量单位就**整条丢弃**并退回模板文案。
- **失败一律降级**：无 Key、超时、5xx、非 JSON、schema 不符、成本熔断 → 回到规则引擎与模板文案，原因写进 `meta.llm.fallback_reasons` 与 `degraded_modes`。
- **可核验**：每个规划响应的 `meta.llm` 会写明是否配置、是否真的调用、模型名、token、金额（含是否已校准）以及每个任务走了模型还是规则 —— 不靠文档里的一句声明。

---

## 技术栈

**前端** Next.js 15（App Router）+ TypeScript + Tailwind CSS + shadcn/ui + MapLibre GL
**后端** Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2.0 + Alembic
**数据库** PostgreSQL 16（本机 / Docker / Supabase 三选一，连接串统一走 `DATABASE_URL`）
**测试** pytest + Vitest（1000 + 个用例）；Playwright 属 M7，尚未引入

---

## 目录结构

```
config/            集中配置（评分权重 / 单价 / TTL / 阈值 / 种子规则）
backend/app/       FastAPI 应用
  core/            配置、日志、错误码、路径
  db/              引擎、会话、模型
  domain/          ★ 纯业务逻辑（零 IO，可脱离数据库与网络单测）
  providers/       ★ 外部能力抽象（LLM / 搜索 / 地图 / 天气，均含降级实现）
  services/        编排层（检索链、缓存、成本与熔断、限流、规划、分享、L7 模型接入）
  schemas/         请求/响应 Pydantic 模型（含开发面板）
  api/v1/          catalog / health / trips / public / dev（最后一项仅开发环境注册）
  # 注：prompt 模板与它的 JSON Schema **版本号一起写在 llm_planner.py 里**
  #     （prompt_version 是 LLM 缓存键的一部分，拆文件会让"改 prompt 要同时改缓存键"变难）
backend/scripts/   抓取、种子、质检、报告脚本
frontend/app/      Next.js 页面（app/dev/ 是仅开发环境的设置页）
frontend/e2e/      Playwright 测试（M7 才引入，目前不存在）
docs/              生成的数据质量报告、成本报告、安全清单
```

**架构铁律**：`app/domain/**` 不得 import `httpx` / `sqlalchemy` / `fastapi`（有 AST 静态测试强制），
因为核心算法必须能被纯净单测覆盖，且不因外部依赖故障而失效。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [`PRD.md`](./PRD.md) | 产品需求、数据模型、评分算法、成本模型、测试计划、安全、验收标准 |
| [`PROJECT_ANALYSIS.md`](./PROJECT_ANALYSIS.md) | 现状探测与 8 条架构决策记录（ADR） |
| [`RUNNING.md`](./RUNNING.md) | 怎么跑起来、当前能干什么、降级与配置、排障 |
| [`TASKS.md`](./TASKS.md) | 进度明细、每轮发现的问题与修法、刻意没做的事 |
| `docs/DATA_REPORT.md` | 知识库数据质量报告（脚本生成） |
| `docs/COST_REPORT.md` | 单次规划成本报告（脚本生成，需先跑 `make cost`） |
