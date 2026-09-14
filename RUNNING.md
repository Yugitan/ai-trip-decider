# TripDecider 运行与使用文档

> 面向「把项目跑起来、知道现在能用什么、出问题怎么查」。
> 产品需求见 [`PRD.md`](./PRD.md)，进度与里程碑见 [`TASKS.md`](./TASKS.md)。
>
> **本文档中的命令都在 2026-09-13 于本机（macOS + Homebrew postgresql@16）实跑过**，
> 输出数字为实测值。真实跑过的：`make help`、`make dev-backend`、`/api/v1/health`、
> `POST /api/v1/trips:plan?sync=true`、`make test-unit|contract|integration|frontend`、
> `make validate`（2026-09-13 修复 Makefile 后实跑，exit=0）、
> `make check`（2026-09-13 实跑，exit=0）、`/api/v1/dev/*` 与 `/dev` 页面（开发面板）。
>
> **2026-09-14 追加实跑**：`make cost`（首次跑通，产物 `docs/COST_REPORT.md`）、`make check`（exit=0）、
> `git push` 三条分支（见 §11）、**GitHub Actions 首跑通过**（4.2 分钟，见 §12）。
>
> **未实测**（文中数字来自 `README.md`/`Makefile`）：`make setup`（会装依赖，没有重跑）、
> `make fetch-osm`（需网络，约 9 分钟）、
> `make relations` / `make todo` / `make security`。

---

## 0. 一句话

TripDecider 是 **Next.js 15 前端 + FastAPI 后端 + PostgreSQL 16** 的单城市（广州）旅行路线决策器。
后端要求 **Python 3.12**、前端要求 **Node ≥ 20 + pnpm**，数据库要求 **PostgreSQL 16**。

**不需要任何第三方 API Key 也能完整跑起来** —— 缺 Key 时 LLM / 搜索 / 地图全部自动降级，
并在界面与 `/api/v1/health` 里如实标注降级模式（PRD §15.5）。

---

## 1. 前置条件

| 依赖 | 版本 | 检查命令 | 本项目实测环境 |
| --- | --- | --- | --- |
| Python | **3.12**（不要用系统 python3） | `uv python list` | `uv python install 3.12` 由 `make setup` 代劳 |
| [uv](https://docs.astral.sh/uv/) | 任意较新版本 | `uv --version` | ✅ 已装 |
| Node.js | **≥ 20** | `node -v` | v26.5.0 |
| pnpm | 与 lockfile 同源 | `pnpm -v` | ✅ 已装 |
| PostgreSQL | **16** | `psql --version` | postgresql@16（Homebrew 服务） |
| Docker | 可选 | `docker info` | 未使用（见下方说明） |

启动数据库（本项目开发默认走**本机 Postgres**）：

```bash
brew services start postgresql@16   # macOS + Homebrew
psql -h 127.0.0.1 -U "$(whoami)" -d postgres -c 'select version();'
```

> ⚠️ **`make db-up`（用 Docker 起 Postgres）当前不可用**：该目标执行 `docker compose up -d db`，
> 但仓库里**没有 `docker-compose.yml`**（已确认）。要么用上面的 brew 方式，
> 要么自己写一份 compose 文件后再 `docker compose up -d db`。

---

## 2. 快速开始（首次）

```bash
cd ai-trip-decider

make setup     # 装 Python/Node 依赖 + 建库 + 迁移 + 生成 .env（约 2–3 分钟）
# ⚠️ 打开 .env，把 DATABASE_URL / TEST_DATABASE_URL 里的用户名改成你自己的：
#    改前： postgresql+asyncpg://tanjiaxi@127.0.0.1:5432/tripdecider_dev
#    改后： postgresql+asyncpg://$(whoami)@127.0.0.1:5432/tripdecider_dev
#    （.env.example 里写死的是作者机器的用户名；照抄会连不上库）
make dev       # 并行启动：后端 http://127.0.0.1:8000   前端 http://localhost:3000
```

打开 **http://localhost:3000**。

### `make setup` 到底做了什么

1. `uv python install 3.12`
2. `cd backend && uv sync`（后端依赖，虚拟环境在 `backend/.venv`）
3. `cd frontend && pnpm install`
4. 若 `.env` 不存在，从 `.env.example` 复制一份
5. `make db-create` —— 创建 `tripdecider_dev` 与 `tripdecider_test` 两个库（已存在则跳过）
6. `make migrate` —— `alembic upgrade head`

**注意：`make setup` 不灌知识库数据**。数据库建好后是空的，需要额外跑一次 `make seed`（见 §4）。
当前本机开发库的实测状态：`places = 1622`、`routes = 44`、迁移版本 `217165a7a13b (head)`。

---

## 3. 分开启动 / 重启

```bash
make dev            # 后端 + 前端一起（内部就是 make -j2 dev-backend dev-frontend）
make dev-backend    # 只起后端：127.0.0.1:8000，带 --reload
make dev-frontend   # 只起前端：localhost:3000（next dev）
```

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 前端 | http://localhost:3000 | 首页 / 知识库浏览 / 数据来源页 |
| **开发设置**（仅开发环境） | http://localhost:3000/dev | 改 Key/Provider/阈值/`config/*.yaml`，见 §5.5；生产构建里入口不渲染 |
| 后端 API | http://127.0.0.1:8000 | 统一 envelope：`{ ok, data, error, meta }` |
| 开发配置 API（仅开发环境） | http://127.0.0.1:8000/api/v1/dev/config | `ENV!=development` 时路由**根本不注册**（404） |
| Swagger UI | http://127.0.0.1:8000/docs | 非生产环境才有（生产会关掉 `/docs`） |
| 健康检查 | http://127.0.0.1:8000/api/v1/health | 见下方示例 |

后端正常启动的实测输出（节选）：

```json
{
  "ok": true,
  "data": {
    "status": "ok",
    "version": "0.1.0",
    "env": "development",
    "database": { "ok": true, "latency_ms": 31, "active_places": 1622, "routes": 44, "cities": 1,
                  "kb_version": "gz-2026.09.10-8a77da52e4", "schema_ready": true },
    "degraded_modes": [
      "llm:disabled(规则引擎+模板兜底)",
      "search:seed_only(不联网，仅本地知识库)",
      "map:osrm(距离/耗时为路网或估算值)"
    ],
    "providers": { "llm": "disabled", "search": "seed_only", "map": "osrm", "weather": "open_meteo" },
    "config": { "scoring_version": "2026.09.1", "pricing_calibrated": false }
  }
}
```

`degraded_modes` 非空是**正常状态**，不是报错：它只是如实告诉你哪些能力在用兜底实现。
想消掉 `llm:disabled` → 在 `.env` 里填 `DEEPSEEK_API_KEY`。

快捷查看：

```bash
make health    # 等价于 curl 健康检查并格式化输出
```

---

## 4. 知识库数据（建库后必做一次）

`places` / `routes` 是规划引擎的输入，空库会让规划直接失败。

> 2026-09-13 修复：这批目标原先缺 `PYTHONPATH=.`，跑任何脚本都报 `ModuleNotFoundError: No module named 'app'`；
> 现在 Makefile 的脚本类目标都带上了它（见 §8.1），直接用 `make` 即可。

```bash
make seed           # 灌入广州知识库（幂等）
make validate       # 质检闸门（实测 exit=0）
make relations      # 重算地点关系图
make cost           # 成本报告（产物 docs/COST_REPORT.md）
```

各目标用途与实测：

| Makefile 目标 | 用途 | 实测 |
| --- | --- | --- |
| `make seed` | 灌入地点 + 关系 + 路线 | 约 12 秒，幂等 |
| `make validate` | 知识库质检闸门 | **exit=0**（0 个阻断项失败；告警：营业时间未知 89.8%、身份可交叉核验 9.8%、district 5/10） |
| `make relations` | 重算地点关系图 | 离线估算；`OSRM=1` 走真实路网 |
| `make cost` | 成本报告 | 产物 `docs/COST_REPORT.md` |
| `make report` | 依赖 `validate` | 产物 `docs/DATA_REPORT.md` |

`make validate` 是**发布闸门**：门槛不达标会非零退出，不要用跳过它的方式"让 CI 变绿"。

原始 OSM 数据（`backend/data/raw/osm_guangzhou_*.json`，约 3.8MB）**已入库**
（`.gitignore` 里对这两份 JSON 做了例外），所以 clone 下来 seed 与集成测试都能直接跑，
不需要先跑 9 分钟的抓取。代价：`make fetch-osm` 重跑会改动这两份已跟踪的文件。
想重建原始数据时（约 9 分钟，需要网络、且依赖 Overpass 可用）：

```bash
make fetch-osm
```

改完 `config/*.yaml`（评分权重、过滤规则、单价）或 `backend/data/curated/*.yaml`（人工数据）后的标准动作：

```bash
make seed && make validate
```

### 数据库相关

```bash
make migrate           # 应用迁移到开发库
make migrate-check     # 校验 models 与迁移是否一致（不一致会失败）
make migration M="add xxx"   # 自动生成迁移
make db-status         # 查看连通性与库信息
make db-reset          # ⚠️ 删库重建 + seed + relations（需 CONFIRM=yes，等价于丢数据）
make db-drop CONFIRM=yes
```

`make db-reset` 里有一个刻意的顺序：**关系图必须在地点之后算**（`place_relations` 对 `places` 是级联删除）。

---

## 5. 现在能用什么（截至 2026-09-13）

进度：**M0 脚手架 + M1 广州知识库 + M2 领域内核 + M3 Provider + M4 规划编排与 API 已完成；
M5 前端结果页未开始**（以 `TASKS.md` 为准；`README.md` 表格里的 M4「进行中」是过期描述）。

### 5.1 可以打开的前端页面

| 页面 | 地址 | 内容 |
| --- | --- | --- |
| 首页 | http://localhost:3000 | 后端连接状态与知识库规模（真实数据，连不上会明说而不是显示假数据）、规划表单 |
| 知识库浏览 | http://localhost:3000/explore/guangzhou | 概况条、类别筛选、**别名搜索**（「小蛮腰」→ 广州塔）、地点卡片、44 条路线模板 |
| 数据来源与免责 | http://localhost:3000/about/data | 每类数据从哪来、哪些不保证 |
| **开发设置**（不是用户功能） | http://localhost:3000/dev | 只在开发构建可见；见 §5.5 |

### 5.2 ✅ 已修：首页表单曾因「发中文标签」而稳定 422

**修复前**的实测症状（现在已经跑通了，这一段留作记录）：

| 字段 | 修复前前端发送 | 后端要求 |
| --- | --- | --- |
| `city` | `"广州"` | `"guangzhou"` |
| `preferences` | `"美食"`、`"拍照"`…（中文标签） | `"food"`、`"photo"`…（枚举 key） |

后果是点「开始规划」稳定 **HTTP 422**，`UNSUPPORTED_CITY` + `INVALID_INPUT` 一起返回。

**修法**：把「界面标签 → 接口取值」的换算显式写在 `components/planner-form.tsx` 里
（`CITY_OPTIONS[*].api` 与 `PREFERENCE_OPTIONS[*].api`，汇总在 `buildPayload()` 一处），
**而不是**让后端去猜中文标签 —— 接口的枚举语义不应该跟着 UI 文案漂移。
`lib/api.ts` 的 `PlanRequest` 上也写明了这是线上格式。

**防回归**：前后端不再靠"人对得齐"。`planner-form-submit.test.tsx` 里有一条用例
**直接读后端的 `config/scoring.yaml`**，把 `preference_dimensions` 的键与前端选项逐个对照；
新增一个偏好却忘了在另一头登记，测试就会红，而不是等用户点出一个 422。

**实测（修复后）**：前端默认值对应的请求体（`city=guangzhou`、`preferences=[]`）
→ **HTTP 200 / 3 套方案**；同一份请求换成修复前的中文值 → **HTTP 422 `UNSUPPORTED_CITY`**。

### 5.3 直接用 API（推荐当前阶段这样验证）

```bash
# 1) 健康状态与降级模式
curl -s localhost:8000/api/v1/health | python3 -m json.tool

# 2) 城市与数据规模
curl -s localhost:8000/api/v1/cities | python3 -m json.tool

# 3) 类别分布与可信度分布
curl -s localhost:8000/api/v1/cities/guangzhou/stats | python3 -m json.tool

# 4) 搜索（支持别名）：小蛮腰 → 广州塔
curl -s "localhost:8000/api/v1/cities/guangzhou/places?q=小蛮腰" | python3 -m json.tool

# 5) 按类别筛选 + 分页（单页上限 100）
curl -s "localhost:8000/api/v1/cities/guangzhou/places?category=museum&limit=5"

# 6) 地点详情（含来源、别名、unknown_fields、质量标记）
curl -s localhost:8000/api/v1/places/<place_id> | python3 -m json.tool

# 7) 路线模板（带站点明细）
curl -s "localhost:8000/api/v1/cities/guangzhou/routes?archetype=relaxed"

# 8) 评分权重与偏好维度（排序规则不是黑箱）
curl -s localhost:8000/api/v1/meta/scoring-config | python3 -m json.tool
```

**真正的规划请求**（实测 HTTP 200，耗时 252ms，返回 3 套路线）：

```bash
curl -s -c /tmp/td-cookies.txt -X POST "localhost:8000/api/v1/trips:plan?sync=true" \
  -H 'Content-Type: application/json' \
  -d '{
        "city": "guangzhou",
        "days": 2,
        "people": 2,
        "preferences": ["food", "photo"],
        "pace": "relaxed",
        "budget": { "amount": 300, "scope": "per_person" },
        "free_text": ""
      }' | python3 -m json.tool
```

实测返回结构（节选，2026-09-13 在**配了 `DEEPSEEK_API_KEY`** 的本机上实跑，HTTP 200 / 2.18s）：

```
ok=True   routes=3   elapsed_ms=2115
 - relaxed | 老城步行寻味 | 3 站 · 约 5.8 小时 · 步行 0.5 km
 - classic | 西关甜味慢行 | 5 站 · 约 8.4 小时 · 步行 1.9 km
 - themed  | 园林茶点线 | 3 站 · 约 5.4 小时 · 步行 0.5 km
meta.degraded_modes = ["search:未配置搜索 API Key（只读本地知识库，不联网）"]
meta.llm = {
  "enabled": true, "used": true, "provider": "deepseek", "model": "deepseek-flash",
  "prompt_version": "2026.09.2",
  "calls": 1, "cache_hits": 0, "tokens_in": 368, "tokens_out": 146,
  "cost_cny": "0.002056", "cost_calibrated": true,
  "tasks": {"route_narrative": "llm"}, "fallback_reasons": []
}
```

（路线名是模型写的，`one_liner` 里的站数/时长/步行距离是代码算的 —— 两者永远不混。）

关于 `meta.llm`（M4 起）：

- 它告诉你模型**真的**做了什么：`tasks` 里 `intent_patch` / `route_narrative`
  分别取值 `llm`（调了模型）/ `cache`（命中 LLM 缓存）/ `rule`（降级到规则与模板）。
- 路线名与推荐理由由模型写，但**不接受任何数字**：站数/时长/步行距离仍由代码算
  （见 `one_liner`）。文案里出现数字或计量单位会被整条丢弃并退回模板。
- `cost_calibrated` 是**真实支出与估算的分界线**：DeepSeek 单价已于 2026-09-13
  按官方价目页回填（`config/pricing.yaml`，含 `calibrated_at`），所以现在是 `true`；
  凡是没有可靠价目的供应商（如高德）成本仍记 0 且标记未校准 —— 金额旁边必须能看到这个标记。
- 同一个请求第二次提交会命中 LLM 缓存与幂等重放：`calls: 0`、`cache_hits: 1`，
  不再花一次模型的钱。
- 没有 Key 时是 `enabled: false, used: false`，且 `degraded_modes` 里会出现 `llm:` 开头的条目。

要点：

- **`?sync=true` 是调试/脚本用的同步模式**，直接返回完整行程。
- 不加 `sync` 时返回 **202 + `{ request_id, stream_url }`**，方案通过 SSE 推送
  （`GET /api/v1/trips/{request_id}/stream`，事件序列 `plan.started` → `plan.progress` →
  `plan.completed` / `plan.failed`）。不要用 curl 抓默认模式的第一行就以为拿到了方案。
- 行程按**浏览器会话**（cookie）隔离，所以 curl 要带 `-c/-b` cookie jar，
  后续 `GET /trips/{id}`、`POST /trips/{id}/revise`、`/undo`、`/share` 才认得出你。
- 其余规划接口：`POST /api/v1/trips/{trip_id}/revise`（自然语言改路线）、
  `/undo`、`/share`；公开分享页 `GET /api/v1/public/trips/{slug}`。

### 5.5 开发设置面板（`/dev`，**只给维护者**）

地址：**http://localhost:3000/dev**（开发构建下页头右上角也有入口）。

它解决的问题只有一个：把「改 `.env` → 重启后端 → 再试」变成「在页面上改 → 立即生效」。
**它不是用户功能**，三道门把它挡在用户外面：

1. **路由只在 `ENV=development` 时注册**：`ENV=production` 下 `/api/v1/dev/*` 完全不存在，
   打开页面只会看到接口报错（页面本身也是 `noindex`，且入口在 production 构建里**不渲染**）。
2. **`ADMIN_TOKEN`**：`.env` 里设了就必须要，没设时只允许本机访问。
   Token 存在 `sessionStorage`（关标签页即失效），不会写进 localStorage。
3. **白名单**：能改哪些键写在 `services/dev_config.py` 的 `ENV_FIELDS` 里。
   刻意**不含** `DATABASE_URL` / `TEST_DATABASE_URL`（改了必须重启，且面板自己就在用库）、
   `SESSION_SECRET`（会话签名密钥）、`ENV`（改它等于当场关掉面板）。

能做什么：

| 区域 | 能改什么 | 注意事项 |
| --- | --- | --- |
| 环境变量 | LLM Provider / 各家 Key / 模型名 / 超时重试 · 搜索与地图 Provider 与 Key · 成本与限流阈值 · 端口与 CORS | **Secret 只写不读**（只显示 `sk-***abcd`）；**留空 = 不修改**；要清空得点那一行的「清空该项」 |
| 配置文件 | `config/*.yaml` 五份（scoring / limits / ttl / pricing / seed）全文编辑 | **先校验后落盘**：不通过就一个字节都不写（提示里会带 `hint`） |
| 生效快照 | 只读展示当前进程真正在按什么跑（provider 降级、阈值、版本号、`degraded_modes`） | 改完这里没变 → 说明改动没生效，而不是"面板在骗你" |

改完会**清空配置缓存并把值写进 `os.environ`**，所以大多数改动不用重启；
`.env` 里的值会保留（只改对应行，注释与顺序都不动），下次启动仍然生效。

> 如果这个面板改坏了配置：`config/*.yaml` 有硬校验拦着（写不进去）；
> `.env` 的值一旦通过了 `Settings()` 校验就会被保留 —— 两个都改乱了就 `git checkout .env config/`。

### 5.4 当前仍然做不到的事

| 还不能 | 原因 |
| --- | --- |
| 在网页上看到路线方案 | M5 结果页未实现（后端已能返回数据） |
| 把方案拿去做验证 | 后端已可用（`/api/v1/trips:plan`）；结果页属 M5 |
| 地图展示、路线分享页（前端） | M5 |
| `make e2e` | `frontend/` 下**没有** Playwright 依赖与配置（已确认目录里没有 `e2e/`、没有 playwright 配置），该目标会直接失败 |

> 另：首页表单那个稳定 422 已于 2026-09-13 修好（§5.2），
> 所以「点提交 → 看进度 → 看这次模型做了什么」现在是通的；
> 不做的只是「把方案渲染成时段表/地图」那一段（M5）。

---

## 6. 测试与质量闸门

```bash
make test-unit         # 后端单元：实测 791 passed，约 2 秒，无需数据库/网络
make test-contract     # 后端契约：实测 43 passed，约 1 秒，respx 拦网络，无需 Key
make test-integration  # 后端集成：实测 157 passed，约 18 秒，需要测试库
make test-frontend     # 前端单测：实测 189 passed，约 3 秒
make test              # 上面四层依次跑
make test-cov          # 后端覆盖率闸门（实测 95.47%，低于 COV_MIN=93 直接非零退出）
make test-frontend-cov # 前端覆盖率闸门（实测 statements 98.44% / branches 87.19%，阈值见 vitest.config.ts）
make check             # ★ 提交前必跑：lint + typecheck + 前后端覆盖率闸门 + 测试（实测约 90 秒，exit=0）
```

单项筛选（改哪测哪）：

```bash
cd backend && PYTHONPATH=. uv run pytest -m unit -k "similarity or normalize" -q
cd backend && PYTHONPATH=. uv run pytest tests/unit/test_config.py -q
cd frontend && pnpm test --run components/__tests__/planner-form.test.tsx
```

> ⚠️ **不要在仓库根目录直接跑 `pytest`**：`app` 包靠 `backend/` 作为工作目录解析，
> 根目录跑会 collection error。用 `make` 目标，或先 `cd backend && PYTHONPATH=. uv run pytest`。

集成测试的测试库是**每次重建**的（约 12 秒），这样测的一定是当前数据；
如果测试库或 `backend/data/raw/osm_guangzhou_raw.json` 缺失，它会**直接失败并打印修复指令**，
而不是悄悄 skip（跳过会让「集成测试通过」变成假象）。

两个补充说明（都与“LLM 到底跑没跑”有关）：

- **集成测试默认不碰真实 LLM**：`tests/integration/conftest.py` 在导入应用前就把
  `LLM_PROVIDER` 压成 `disabled`，否则本机配了 Key 之后，凡是带 `free_text` 的用例
  都会真的调模型（慢、花钱、结果随模型漂移）。要跑真实模型用下面的活体测试。
- **`tests/integration/test_llm_live.py`**：有 `DEEPSEEK_API_KEY` 才跑，没有则 skip。
  它直接构造 Provider 发两个极短请求，专门用来“让 Key 失效在测试阶段被发现”——
  契约测试用 respx 拦网，证明不了真 Key 是否还能用。

两个不是测试但同样是闸门的检查：

```bash
make validate   # 知识库质检：不达标非零退出（2026-09-13 修好，原先报 No module named 'app'）
make todo       # 扫 TODO / FIXME / console.error / 未实现标记
make security   # 密钥泄露模式扫描 + domain 层纯净性（AST 强制不得 import httpx/sqlalchemy/fastapi）
```

代码风格与类型：

```bash
make lint       # ruff（后端）+ eslint（前端）
make typecheck  # mypy strict（app/scripts/tests）+ tsc --noEmit
make format     # 自动格式化
```

---

## 7. 配置与降级

- **唯一的环境文件是项目根目录的 `.env`**（后端通过 `backend/app/core/paths.py` 的 `env_file()` 读它）。
- ⚠️ **Next.js 只读 `frontend/` 目录下的 `.env*`**（如 `frontend/.env.local`）。
  把前端变量写在根 `.env` 里**不会被前端进程读到**。
- 铁律：任何 Key 只存在于后端环境变量，禁止进入前端产物。

关键变量（完整清单见 `.env.example`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://<你的用户名>@127.0.0.1:5432/tripdecider_dev` | **必须改成 `whoami` 的结果** |
| `TEST_DATABASE_URL` | 同上的 `tripdecider_test` | 集成测试用 |
| `BACKEND_PORT` / `FRONTEND_URL` | `8000` / `http://localhost:3000` | CORS 白名单按 `FRONTEND_URL` 放行 |
| `LLM_PROVIDER` | `deepseek` | 未配 Key 时自动降级为规则引擎 + 模板 |
| `DEEPSEEK_API_KEY` | 空 | 填上才会真正调 LLM |
| `SEARCH_PROVIDER` / `TAVILY_API_KEY` 等 | 空 | 无 Key = `seed_only`，只读本地知识库 |
| `MAP_PROVIDER` / `AMAP_WEB_KEY` | 空 | 无 Key 依次降级 `osrm` → `haversine` 离线估算 |
| `GLOBAL_DAILY_BUDGET_CNY` 等 | 见文件 | 成本熔断与限流 |
| `ADMIN_TOKEN` | 空 | 开发设置面板的口令（空 = 只允许本机访问）；生产环境该路由不注册 |
| `FAULT_INJECTION` | 空 | **仅测试用**，生产禁止设置 |

单价校准状态（`config/pricing.yaml`，面板可直接改）：

| 供应商 | 状态 |
| --- | --- |
| `llm.deepseek`（fast/strong） | ✅ 已从官方价目页回填（`calibrated_at: 2026-09-13`，按**高峰价**填，低峰为半价） |
| `search.tavily` | ✅ 已回填 credits 口径 |
| `search.seed_only` / `map.osrm` / `map.haversine` / `weather` | ✅ 免费，金额 0 且 `calibrated=true`（0 元是已知事实） |
| `map.amap` / `search.serper` / `search.bing` / `llm.openai` / `llm.anthropic` | ❌ 仍为 `null` + `needs_calibration: true`：金额记 0 且标记未校准，**不是"免费"** |

`/health` 的 `config.pricing_calibrated` 是"**全部**供应商都已校准"的含义，
所以只要 `map.amap` 还没取到价目，它就一直是 `false` —— 这与"单次规划用的 DeepSeek 已校准"不矛盾，
两者口径不同（前者是全局、后者是这次调用）。

降级矩阵：

| 能力 | 有 Key | 无 Key 降级 |
| --- | --- | --- |
| LLM（DeepSeek） | 意图补全（规则没解析到的自由文本）+ 方案叙事（路线名/推荐理由） | 规则引擎 + 模板文案 |
| 搜索 | 发现库外新地点 | `seed_only`：只读本地知识库 |
| 地图（高德） | 精确路网与公交时间 | `OSRM` 真实路网 → `haversine` 离线估算 |
| 天气（open-meteo，免 Key） | 雨天/高温路线适配 | 跳过天气适配 |

红线：任何非真实来源的距离/时间都带 `source=estimated` 标记并在 UI 显示；
缺失字段写 `NULL` 并记入 `unknown_fields`，**绝不填猜测值**。

---

## 8. 已知问题（全部为 2026-09-13 实测）

### 8.1 ✅ 已修复：「跑 `scripts/` 脚本」的 Make 目标报 `ModuleNotFoundError: No module named 'app'`

原先受影响：`make validate` / `make seed` / `make fetch-osm` / `make relations` / `make cost` / `make report`
（以及依赖它们的 `make db-reset`）。

原因：这些目标写的是 `cd backend && uv run python scripts/xxx.py`。
直接跑脚本时 `sys.path[0]` 是**脚本所在目录**（`backend/scripts/`），而 `app` 包在 `backend/app/`，
所以 import 不到。`uvicorn` 能跑是因为它自己会把 cwd 插到 `sys.path`；`pytest` 能跑是因为
`pyproject.toml` 里有 `pythonpath = ["."]`；裸 `python scripts/...` 两者都不占。

修法：Makefile 里引入 `SCRIPT_ENV := PYTHONPATH=.`，所有脚本类目标统一带上，
例如 `cd $(BACKEND) && $(SCRIPT_ENV) $(UV) run python scripts/validate_seed.py`。
（`relations` 目标里 `SCRIPT_ENV` 放在 `OSRM=$(OSRM)` 之前，避免 env 赋值被 `cd` 吃掉。）

2026-09-13 实测：`make validate` 正常跑完并 **exit=0**；5 个脚本均能正确 import `app`。
**不再需要**手工写 `cd backend && PYTHONPATH=. uv run python scripts/...` 的绕过写法。

### 8.2 `make db-up` 找不到 `docker-compose.yml`

仓库里没有这个文件，该目标执行 `docker compose up -d db` 必然失败。
开发就用 `brew services start postgresql@16`（或自己补一份 compose 文件）。

### 8.3 ✅ 已修复：首页表单与后端契约不一致（提交稳定 422）

详见 §5.2。表单改为发送接口枚举值，并加了一条**跨语言契约测试**（前端测试直接读
`config/scoring.yaml` 比对偏好键）；后端一字未改 —— 接口本来就应该只认枚举值。

### 8.4 `make e2e` 会直接失败

`frontend/` 下没有 Playwright 依赖、没有配置文件、也没有 `e2e/` 目录（已确认），
端到端测试属于尚未开始的里程碑。

### 8.5 文档口径不一致

`README.md` 的状态表仍写着 M4「进行中」、并描述首页提交会「打印真实 JSON 请求体」；
`TASKS.md`（最后更新 2026-09-12）已记录 M4 完成、提交会真实调用 `POST /trips:plan`。
以 `TASKS.md` 和实际代码为准。

---

## 9. 排障

| 症状 | 原因 / 处理 |
| --- | --- |
| 前端首页显示「未连接到后端服务」 | 后端没起：`make dev-backend`；或 `NEXT_PUBLIC_API_BASE_URL`（写在 `frontend/.env.local`）指错了地址 |
| `psql: FATAL: role "..." does not exist` | `.env` 里的用户名还是作者的 `tanjiaxi`，改成 `whoami` |
| `make db-up` 报 compose 文件找不到 | 仓库无 `docker-compose.yml`，改用 `brew services start postgresql@16` |
| 首页点「开始规划」报 404/NOT_FOUND | 后端未启动或端口/地址不对（M4 已完成，这个接口是存在的） |
| 首页点「开始规划」报 422 `INVALID_INPUT` | 后端认为输入不合法：看 `hint` 里的合法取值。表单正常情况下不会触发它 |
| 想反馈后端错误 | 每个响应都带 `X-Request-Id`（响应体 `meta.request_id`），带上它去查后端日志 |
| 后端日志出现「降级模式生效」warning | 正常，说明缺 Key；填 Key 后重启即可 |
| `make validate` / `make seed` 报 `No module named 'app'` | 2026-09-13 已在 Makefile 里修好（§8.1）；若仍报错，说明你的 Makefile 是旧版 |
| `make validate` 非零退出 | 这是**刻意的发布闸门**，按输出里的阻断项修数据 |

---

## 10. 目录速查

```
ai-trip-decider/
├── Makefile                 所有开发命令的入口（make help 实测列 37 行）
├── .env.example             环境变量模板（复制为 .env）
├── PRD.md / PROJECT_ANALYSIS.md / TASKS.md
├── config/                  五份集中配置：scoring / limits / ttl / pricing / seed
├── backend/
│   ├── app/core/            配置、日志、错误码、路径
│   ├── app/db/              引擎、会话、SQLAlchemy 模型
│   ├── app/domain/          ★ 纯业务逻辑（零 IO，禁止 import httpx/sqlalchemy/fastapi）
│   ├── app/providers/       LLM / 搜索 / 地图 / 天气（均含降级实现）
│   ├── app/services/        编排层（检索链、缓存、成本熔断、限流、规划）
│   │                         另含 llm_planner（L7 接入）与 dev_config（开发面板后端）
│   ├── app/api/v1/          catalog（只读目录）/ health / trips（规划）/ public（分享）/ dev（仅开发）
│   ├── alembic/             迁移（当前 head: 217165a7a13b）
│   ├── scripts/             fetch_osm / seed / validate / relations / cost_report
│   └── tests/               unit / contract / integration 三层
└── frontend/                Next.js App Router（app / components / lib），Vitest
    ├── app/dev/             开发设置页（仅开发构建；导航入口在生产构建里不渲染）
    └── lib/dev-api.ts       开发面板的网络客户端（Token 只存 sessionStorage）
```

---

## 11. 分支与远端

远端：`origin` = `https://github.com/Yugitan/ai-trip-decider`（**public**）。

| 分支 | 用途 | 现状 |
| --- | --- | --- |
| `dev` | **主开发分支**（默认分支，所有提交先进这里） | 最新 |
| `test` | 联调 / 验收分支：从 `dev` 提升，供测试环境拉取 | 落后 `dev`（首次上传时三者同点） |
| `prod` | 发布分支：只在验收通过后从 `test` 快进 | 落后 `dev`（首次上传时三者同点） |

首次上传时三条分支指向同一个提交，没有版本差；之后只在需要验收 / 发布时才提升
（下面表格不写死 SHA，避免每推一次就变成一句过期的假话）。
日常只推 `dev`：

```bash
git push origin dev                     # 只推开发分支
git push origin dev:test                # 验收：把 dev 快进到 test
git push origin test:prod               # 发布：把 test 快进到 prod
```

约定：

- **`test` / `prod` 只接受快进**（`--ff-only`），保证线上代码一定是验过的那个提交；
  要回滚就用 `git push --force-with-lease` 把分支指回上一个提交，而不是在 `prod` 上打补丁。
- **凭证不进仓库**：`.env` 已在 `.gitignore`（第 30–32 行），推送用本机钥匙串（macOS `osxkeychain`）里的凭证；
  `.git/config` 里不带任何 token（`git remote -v` 里只有 https 地址）。
- 提交信息用中文、写「为什么」；每个提交应当能让 `make check` 从 `exit=0` 开始。

---

## 12. CI（GitHub Actions）

`.github/workflows/ci.yml`。触发：push 到 `dev` / `test` / `prod`，以及面向这三条分支的 PR
（同一分支连续推送会取消旧运行）。整份工作流只做一件事：

```bash
make check
```

不另写一套命令是刻意的 —— 两套命令迟早分叉，然后就是「本地绿、CI 红」，
或者更糟：CI 绿而本地那条真闸门没人跑。

| 准备 | 做法 | 为什么 |
| --- | --- | --- |
| 数据库 | `services: postgres:16` + `make test-setup` | 集成测试跑的是真实 PostgreSQL，不是 mock |
| 环境 | `cp .env.example .env` + 两行 `sed` 改数据库连接 | 跑的就是「照 §2 做一遍」得到的环境，不是 CI 特供配置 |
| 账号 | job 级 `PG_USER=postgres` `PGPASSWORD=postgres` | `Makefile` 的 `PG_USER ?= $(shell whoami)` 是**本机开发**假设；环境变量在 make 里优先于 `?=`，所以不必为 CI 改 Makefile |

CI 与本地唯一有意的差异就是上面那三行；其余（Python 3.12 / Node 22 / pnpm 11.21.0）都对齐开发机
（pnpm 大版本会改 lockfile 解释方式，两边不一致时 `--frozen-lockfile` 会把「环境差异」报成「依赖冲突」）。

首次通过实测（[run #2](https://github.com/Yugitan/ai-trip-decider/actions/runs/34800434537)，**4.2 分钟**）：

```
All checks passed!                              ← ruff
Success: no issues found in 117 source files   ← mypy strict
TOTAL    5896 stmts   267 miss   95%           ← 覆盖率闸门
989 passed, 2 skipped, 7 warnings in 143.37s    ← 后端
Tests  189 passed (189)                         ← 前端
```

**CI 是 989 + 2 skipped，本机（配了 Key）是 991**：差的就是 `tests/integration/test_llm_live.py`
那两条真实模型调用，CI 里没有 `DEEPSEEK_API_KEY`，它们按设计跳过并打印原因（§6）。
看到 “2 skipped” 不是故障，是「这批用例本来就不该在无 Key 环境下跑」的如实交代。

两处刻意的取舍：

- **OSM 原始数据（约 3.8MB）入库**：集成测试缺数据是 fail 而不是 skip（§6），所以 CI 要么真能重建知识库，
  要么就得把集成测试排除掉 —— 后者等于给「线上是验过的提交」打折扣。代价：`make fetch-osm` 重跑会改动已跟踪文件。
- **不在 CI 里重复跑 lint / mypy / 覆盖率**：它们本来就是 `make check` 的一部分。

想让 CI 与本地完全一致（含 Python / Node / pnpm 版本），就本地跑同一串命令：

```bash
cp .env.example .env   # 然后把 DATABASE_URL / TEST_DATABASE_URL 改成你的账号
make test-setup && make check
```
