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
> **2026-09-14 追加实跑（M5 起步）**：用无头 Chrome 真跑了一遍「首页默认值 → 点开始规划 → 看到 A/B/C 三套方案与站点明细 → 改路线（改成 2 天）→ 撤销 → 生成公开链接 → 打开分享页」，
> 并因此定位并修好两个真实缺陷：跨站页面下游客会话 cookie 被浏览器丢弃（§8.6）、
> `plan.completed` 只有元信息而界面从不回查行程（§5.4 已更新）。
> 还顺手修了一个只有真跑才会暴露的 HTML 问题：结果区里的「改路线」表单一度嵌在规划表单内（`form` 套 `form`，浏览器报 hydration 错误）。
> 前端闸门实测：`pnpm test --run` 233 passed / typecheck / lint 全绿。
>
> **2026-09-15 追加实跑（M5 对比视图 / 行程地址 / 复制）**：结果页多了一张逐项对比表，
> 新增行程自己的地址 `/trip/{id}`，分享页可以「复制这套路线」。
> 前端闸门实测：`pnpm test --run` **329 passed** / `typecheck` / `lint` / `build` 全绿；
> 覆盖率 statements **98.94%** / branches 89.31%（闸门 96 / 84）。
>
> **2026-09-16 追加实跑（M7 性能与安全检查）**：`make security`（密钥模式扫描 clean +
> domain 纯度 20 passed）、**`make perf`**（一条命令跑完压测与前端 LCP，产物见 §8.11）、
> `make check`（exit=0）。后端 **1095 passed** / 覆盖率 **95.61%**；
> 前端 **394 passed** / statements **99.06%**。分享页 LCP 392ms、首屏 JS 127 KB、
> **首页 TTI 569ms ✅**（长任务分析，不再是"未测"；分享页 TTI 作为观察值另记 7.7s）。
>
> **2026-09-16 追加实跑（关系图与文档对齐）**：`seed_guangzhou.py --force`（先 `pg_dump` 备份，
> 443 条本地行程被删）+ `make relations OSRM=1` + `make validate`（0 阻断项 / 2 告警）
> + `make perf`。发现与读法见 §8.12。
>
> **未实测**（文中数字来自 `README.md`/`Makefile`）：`make setup`（会装依赖，没有重跑）、
> `make fetch-osm`（需网络，约 9 分钟）、`make todo`。

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
当前本机开发库的实测状态（2026-09-16 重跑建库后）：`places = 3166`、`routes = 44`、
知识库版本 `gz-2026.09.16-1a12399485`、迁移版本 `217165a7a13b (head)`。
（`places` 从 M1 时代的 1622 涨到 3166，是 2026-09-15 补齐 B1/B2 两类数据的结果；
本行写的是**日期 + 实测值**，不写“约 3000”这种会慢慢变成假话的写法。）

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

后端正常启动的实测输出（节选，2026-09-16）：

```json
{
  "ok": true,
  "data": {
    "status": "ok",
    "version": "0.1.0",
    "env": "development",
    "database": { "ok": true, "latency_ms": 31, "active_places": 3166, "routes": 44, "cities": 1,
                  "kb_version": "gz-2026.09.16-1a12399485", "schema_ready": true },
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
| `make validate` | 知识库质检闸门 | **exit=0**（0 个阻断项失败；告警：营业时间未知 83.9%、长期目标身份可交叉核验 17.1%/30%）。❊ 那个 83.9% 数的是**原文**（`opening_hours_raw` 有无值，509 条）；能解成机器可读时段的是 267/3166 ≈ 8.4%。两个数不是同一件事，别看混 |
| `make relations` | 重算地点关系图 | 离线估算；`OSRM=1` 走真实路网 |
| `make cost` | 成本报告 | 产物 `docs/COST_REPORT.md` |
| `make report` | 依赖 `validate` | 产物 `docs/DATA_REPORT.md` |

`make validate` 是**发布闸门**：门槛不达标会非零退出，不要用跳过它的方式"让 CI 变绿"。

原始 OSM 数据（`backend/data/raw/osm_guangzhou_*.json`，三份共约 4.4MB）**已入库**
（`.gitignore` 里对这三份 JSON 做了例外），所以 clone 下来 seed 与集成测试都能直接跑，
不需要先跑 9 分钟的抓取。代价：重跑抓取会改动这些已跟踪的文件。

三份各自负责什么（`seed_guangzhou.load_raw_records` 按 `osm_guangzhou_*.json` 通配读取，
所以新增一份不需要改建库代码）：

| 文件 | 内容 | 重跑方式 |
| --- | --- | --- |
| `osm_guangzhou_raw.json` | 主体 5 个分组（景点/自然/餐饮购物/公共/交通） | `make fetch-osm`（约 9 分钟） |
| `osm_guangzhou_curated_extras.json` | 按人工清单定向补抓的街区/岛屿/村落（B1） | `cd backend && PYTHONPATH=. uv run python scripts/fetch_osm_curated_extras.py` |
| `osm_guangzhou_transport.json` | 交通枢纽（B2），逐取值查询 | `cd backend && PYTHONPATH=. uv run python scripts/fetch_osm_transport.py` |

抓取依赖 Overpass 镜像。2026-09-15 实测：`overpass-api.de` 对本机稳定返回 **HTTP 406**、
`overpass.kumi.systems` 连接超时，只有 **`maps.mail.ru`** 可用（全部 8 个候选的实测结果
记在 `backend/scripts/fetch_osm_guangzhou.py` 的 `MIRRORS` 上方注释里）。
镜像全挂时脚本会**拒绝返回空数据**并报错 —— 把「镜像故障」当成「该类别没有数据」会污染整个知识库。

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

⚠️ **数据变更后重建开发库会撞上一道刻意设计的拦阻**：`make seed` 在「已有行程引用本地点的数据」时
会直接拒绝（`trip_route_stops.place_id → places.id` 是 **`RESTRICT`** 外键 —— 重建会重新编号地点，
旧引用就会指向别的地点）。正确做法是先留退路再重建：

```bash
pg_dump -h 127.0.0.1 -U "$(whoami)" -d tripdecider_dev > /tmp/tripdecider_dev_$(date +%Y%m%d_%H%M%S).sql
cd backend && PYTHONPATH=. uv run python scripts/seed_guangzhou.py --force   # 会先删该城市的行程
make relations OSRM=1
make validate
```

想恢复：`psql -h 127.0.0.1 -U "$(whoami)" -d tripdecider_dev -f /tmp/tripdecider_dev_*.sql`。

---

## 5. 现在能用什么（截至 2026-09-16）

进度：**M0–M7 全部完成**（首页提交 → 三套方案时间线 / 逐项对比表 → `/trip/{id}` 独立结果页 →
改路线 / 撤销 / 分享 / 分享页「复制这套路线」/ 地图（需配 `NEXT_PUBLIC_AMAP_JS_KEY`，见 §8.7）/
分享页 OG 图 + JSON-LD（见 §8.10）；验收侧：E2E 16 场景、异常注入 25 项、
性能与安全检查（`make perf` / `make security`，见 §8.11））。下一步：M8（交付报告）。

### 5.1 可以打开的前端页面

| 页面 | 地址 | 内容 |
| --- | --- | --- |
| 首页 | http://localhost:3000 | 后端连接状态与知识库规模（真实数据，连不上会明说而不是显示假数据）、规划表单；提交后在同一页看到 A/B/C 三套方案、**方案的逐项对比表**与各方案站点明细（含站点位置示意图），并可改路线 / 撤销 / 分享 |
| 分享页 | http://localhost:3000/t/{slug} | 公开链接的落地页（读公开接口 + 服务端渲染，无需登录、只读）；取消分享后**立刻 404**（后端层）；页上有「复制这套路线」—— 复制成访客自己的一份副本（后端 `POST /public/trips/{slug}/copy`），改副本不影响原作者；社交平台抓取时有大图预览（动态 OG 图）与 `TouristTrip` 结构化数据（见 §8.10） |
| **你的行程**（自己的地址） | http://localhost:3000/trip/{trip_id} | 首页提交后拿到的那份行程的**固定地址**：刷新 / 收藏 / 复制地址都停在同一版；改路线与撤销会换地址（每一版都保留，旧地址仍可打开）。它只在你这个浏览器里能打开（按会话隔离，别人打开是 403），给别人看要用分享链接 |
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
| 环境变量 | LLM Provider / 各家 Key / 模型名 / 超时重试 · 搜索与地图 Provider 与 Key · 成本与限流阈值 · 端口与 CORS | **Secret 只写不读**（只显示 `sk-***abcd`）；**留空 = 不修改**；要清空得点那一行的「清空该项」；每个字段右侧的 **ⓘ** 悬停/点击就是它的用途说明 |
| 配置文件 | `config/*.yaml` 五份（scoring / limits / ttl / pricing / seed）全文编辑 | **先校验后落盘**：不通过就一个字节都不写（提示里会带 `hint`） |
| 前端专用配置 | 只读：`frontend/.env.local` 里那几个变量当前配没配、从哪个文件读到的 | 面板**改不到**它们（Next 只读 `frontend/.env*`），但必须看得见 —— 否则「高德 Web 服务 Key 未配置」会被读成「地图整体没配」（§8.9） |
| 生效快照 | 只读展示当前进程真正在按什么跑（provider 降级、阈值、版本号、`degraded_modes`） | 改完这里没变 → 说明改动没生效，而不是"面板在骗你" |

> ⚠️ **成本与限流阈值不在这张表里**：它们的事务所在处是 `config/limits.yaml`
> （下一张卡片可直接改，同样是先校验后落盘）。`.env` 里的同名变量只是镜像，
> 摆在面板上只会让人改一个不生效的数 —— 2026-09-15 已从白名单移除（§8.9）。
>
> 剩下 4 个键是**已规划但尚未实现**的开关（Serper / Bing 的 Key、
> `ENABLE_LOCAL_FETCH`、`NOMINATIM_USER_AGENT`）：它们的 ⓘ 里写着「改了不生效」，
> 两个方向都有测试钉住。

改完会**清空配置缓存并把值写进 `os.environ`**，所以大多数改动不用重启；
`.env` 里的值会保留（只改对应行，注释与顺序都不动），下次启动仍然生效。

> 如果这个面板改坏了配置：`config/*.yaml` 有硬校验拦着（写不进去）；
> `.env` 的值一旦通过了 `Settings()` 校验就会被保留 —— 两个都改乱了就 `git checkout .env config/`。

### 5.4 当前仍然做不到的事

| 还不能 | 原因 |
| --- | --- |
| 可交互的路线导航图 | 结果页现在用的是**站点位置示意图**（编号标记 + 按顺序连线），不是能点开导航的路线图；方案之间的交叉对比已可用（卡片上方的对比表） |

| 分享页的 OG 图 / JSON-LD | ✅ 已实现（2026-09-15，见 §8.10）：动态 OG 图 + `TouristTrip` JSON-LD |
| `make e2e` | `frontend/` 下**没有** Playwright 依赖与配置（已确认目录里没有 `e2e/`、没有 playwright 配置），该目标会直接失败 |

> 首页那条「稳定 422」已于 2026-09-13 修好（§5.2），会话 cookie 被丢弃的问题已于 2026-09-14 修好（§8.6），
> 所以「点提交 → 看进度 → 看这次模型做了什么 → 看到方案本身 → 改一改 / 撤销 / 分享出去」现在是**通的**；
> 地图（§8.7）也已接上，但需要配置 JS API Key。
> 2026-09-15 又补上两块：三套方案的交叉对比表（卡片上方，只做对齐、不排名）、
> 行程自己的地址 `/trip/{id}` 与分享页的「复制这套路线」。
> 分享页的 OG 图与 JSON-LD 也已接上（§8.10）。
> 首页那条路径仍然把结果内嵌在表单下方（刻意如此：用户还在同一页上，不替他改地址），
> 结果区里给出「在新页面打开 / 复制地址」两个入口。
> 这一栏现在没做的只剩可交互的路线导航图（当前是站点位置示意图）。

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
make perf       # 性能压测 + 前端 LCP，重写 docs/PERF_REPORT.md（约 3 分钟，见 §8.11）
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
- ⚠️★ **已导出的环境变量会盖住 `.env`** ★：pydantic-settings 的优先级是
  「环境变量 > `.env` 文件」，而且**空值也算"已经设置"**（空串会被归一化成 `None`）。
  实测：shell 里 `export TAVILY_API_KEY=`（空）时，往 `.env` 里填好 Key 也读不到，
  `search_provider_effective` 仍然是 `seed_only`。
  排查手法：`printenv TAVILY_API_KEY` 看有没有被导出；临时验证用 `env -u TAVILY_API_KEY ...`。
  详见 §8.8（这也是那个「测试偷读开发机环境」问题的另一半）。
- **前端默认走同源代理**：浏览器只请求 `localhost:3000/api/*`，由 `frontend/next.config.ts` 的
  `rewrites` 转发到后端（目标地址取 `API_BASE_URL`，默认 `http://127.0.0.1:8000`）。
  这是**必需**的，不是省一次 CORS：页面 `localhost:3000` 与后端 `127.0.0.1:8000` 属于跨站，
  后端签发的 `td_session` 会被浏览器当成**第三方 cookie 直接丢掉**，
  于是每个请求都是新会话、`GET /trips/{id}` 永远 403（详见 §8.6）。
  确实要直连别的后端时可在 `frontend/.env.local` 里设 `NEXT_PUBLIC_API_BASE_URL`，
  但那样会话能不能用取决于浏览器的第三方 cookie 策略。
- 铁律：任何 Key 只存在于后端环境变量，禁止进入前端产物。
  **唯一已知例外**：高德地图的 JS API Key（`frontend/.env.local` 的 `NEXT_PUBLIC_AMAP_JS_KEY`）。
  它必须出现在浏览器加载的 `<script src="https://webapi.amap.com/maps?...&key=...">` 里 ——
  这是 JS API 的固有形态，绕不过去；保护手段是高德控制台的**域名白名单**。
  与之配套的安全密钥**不在此例外内**：它走服务端代理（`AMAP_SECURITY_CODE`，不加 `NEXT_PUBLIC_` 前缀），
  详见 §8.7。前端环境变量的完整清单见 `frontend/.env.example`。
- 两份模板都有**漂移守卫单测**（`make check` 会跑）：
  根 `.env.example` ↔ `Settings` ↔ 开发面板白名单（`tests/unit/test_env_hygiene.py`）；
  `frontend/.env.example` ↔ 源码里真正读 `process.env` 的那几处（`lib/__tests__/env-example.test.ts`）。
  于是「加了变量忘写模板」「模板里拼错了键名」「留了个没人读的开关」都会当场变红；
  允许被浏览器看见的 `NEXT_PUBLIC_*` 清单也写死在那条测试里 —— 新增一个必须是有意识的决定。

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
| `search.tavily` | ✅ 已回填 credits 口径，且 2026-09-14 真实调用验证过（`test_search_live.py`：单独跑花 2 credits） |
| `search.seed_only` / `map.osrm` / `map.haversine` / `weather` | ✅ 免费，金额 0 且 `calibrated=true`（0 元是已知事实） |
| `map.amap` / `search.serper` / `search.bing` / `llm.openai` / `llm.anthropic` | ❌ 仍为 `null` + `needs_calibration: true`：金额记 0 且标记未校准，**不是"免费"** |

`/health` 的 `config.pricing_calibrated` 是"**全部**供应商都已校准"的含义，
所以只要 `map.amap` 还没取到价目，它就一直是 `false` —— 这与"单次规划用的 DeepSeek 已校准"不矛盾，
两者口径不同（前者是全局、后者是这次调用）。

降级矩阵：

| 能力 | 有 Key | 无 Key 降级 |
| --- | --- | --- |
| LLM（DeepSeek） | 意图补全（规则没解析到的自由文本）+ 方案叙事（路线名/推荐理由） | 规则引擎 + 模板文案 |
| 搜索（Tavily） | 具备联网检索能力（已真实调用验证，见 §8.7 旁的 `test_search_live.py`） | `seed_only`：只读本地知识库 |
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

### 8.6 ✅ 已修复：跨站页面下会话 cookie 被浏览器丢弃（点「开始规划」看不到任何路线）

**症状**（2026-09-14 用无头 Chrome 实测）：点「开始规划」后界面只显示
「后端已接受这次规划请求 / 已生成 3 套方案」，**一条路线都没有**；同一次点击里，
`GET /api/v1/trips/{trip_id}` 返回 **403 `FORBIDDEN`「这个行程不属于当前会话」**。

**根因**（两个叠在一起）：

1. **接口从不返回路线内容**。`plan.completed` 事件只带元信息
   （`trip_id` / `route_count` / `cached` / `degraded_modes` / `llm`），
   路线与站点只能从 `GET /api/v1/trips/{id}` 读 —— 而前端当时根本没调它（结果页属 M5）。
2. **就算调了也一定 403**：页面在 `localhost:3000`、后端在 `127.0.0.1:8000`，两者**跨站**，
   后端签发的 `td_session` 是**第三方 cookie**。
   - `lib/api.ts` 的 `fetch` 没带 `credentials`（默认 `same-origin`）→ 跨域响应上的
     `Set-Cookie` 不发送也不保存；
   - 补上 `credentials: "include"` 之后依然不行：Chrome 现在的默认策略**直接丢弃第三方 cookie**
     （实测 `Network.getAllCookies` 为空，而把页面换成 `http://127.0.0.1:3000` 就能存下 `td_session` 并拿到 200）。
   另外 `lib/plan-stream.ts` 的 SSE 一直用的是 `withCredentials: true`，
   两条通道凭证不一致，等于把同一次「开始规划」拆成了两个互不相识的会话。

**修法**：

- 浏览器改走**同源**：`frontend/next.config.ts` 增加 `rewrites`（`/api/:path*` → 后端），
  `lib/api.ts` 的 `API_BASE_URL` 默认空串；`NEXT_PUBLIC_API_BASE_URL` 不再内联进产物。
  于是 cookie 是第一方的，顺带整个 CORS 都不再需要（后端白名单保留，直连场景仍可用）。
- `fetch` 显式 `credentials: "include"`（`REQUEST_CREDENTIALS`），与 SSE 的 `withCredentials` 对齐。
- 前端补上「回查行程」这一步：新增 `components/trip-result.tsx`，
  收到 `plan.completed` 后 `GET /trips/{trip_id}` 并把 3 套方案渲染成带时间线的卡片，
  同时把估算值、未知项、校验提醒如实标出。

**防回归**：`lib/__tests__/api.test.ts` 钉住 `API_BASE_URL === ""` 与 `credentials: "include"`；
`components/__tests__/trip-result.test.tsx` 钉住加载/失败/重试/空路线四条路径与诚实性标注；
`planner-llm-status.test.tsx` 钉住「完成后真的去取行程并渲染」。

**实测（修复后，无头 Chrome）**：`localhost:3000` 上点「开始规划」→
`202` → SSE `plan.completed` → `GET /api/v1/trips/{id}` **200** → 页面出现「方案 A/B/C」的站点时间线；
`Network.getAllCookies` = `["td_session"]`。

### 8.5 ✅ 已修复：文档口径不一致

`README.md` 的状态表曾写着 M4「进行中」、并描述首页提交会「打印真实 JSON 请求体」；
`TASKS.md`（最后更新 2026-09-12）已记录 M4 完成、提交会真实调用 `POST /trips:plan`。
现已对齐：`README.md` 的里程碑表与「当前不能做什么」都按实际能力重写，
本文件 §5 那两句「M5 前端结果页未开始」也已改成实情。

剩下的一处不一致：`TASKS.md` 还没有 M5 小节（M4 之后直接跳到「下一步（M5 起）」）。

### 8.7 高德地图接入（2026-09-14）

结果页的每套方案里多了一张站点位置示意图。三件值得记下来的事：

**一、Key 的类型是实测出来的，不是猜的。**
高德的「Web端(JS API)」与「Web服务」是两类 Key，用错会返回 `USERKEY_PLAT_NOMATCH (10009)`。
实测拿到的那把 Key：

```
GET https://restapi.amap.com/v3/geocode/geo?key=...   → 10009 USERKEY_PLAT_NOMATCH
GET https://restapi.amap.com/v3/ip?key=...            → 10009 USERKEY_PLAT_NOMATCH
```

即它是 **JS API 类型**：只能给前端地图用，**不能**填到根 `.env` 的 `AMAP_WEB_KEY`
（那个要走 Web服务，用于后端路径规划）。所以后端的 `map_provider_effective` 目前仍是 `osrm`。

**二、坐标必须纠偏，否则每个点偏 100–700 米。**
库里的坐标全部来自 OSM，是 **WGS-84**；高德瓦片是 **GCJ-02**。
实现放在 `frontend/lib/amap-coords.ts`（纯函数），并**拿官方值当过基准**：
用 `AMap.convertFrom(lnglat, "gps", cb)` 对四个点求值再比对 ——
最大偏差 2.8×10⁻⁶ 度（≈0.31 米），而高德返回的坐标本身只保留 6 位小数（≈0.11 米），
所以这点差异基本全来自它自己的取整。实测向量已写进 `lib/__tests__/amap-coords.test.ts`。

**三、安全密钥不进前端产物（Key 则是绕不过去的例外）。**

| 变量 | 位置 | 会不会进浏览器 |
| --- | --- | --- |
| `NEXT_PUBLIC_AMAP_JS_KEY` | `frontend/.env.local` | **会**（脚本 URL 里，JS API 的固有形态；保护靠控制台域名白名单） |
| `AMAP_SECURITY_CODE` | `frontend/.env.local`（**不加** `NEXT_PUBLIC_`） | 不会：由 `app/amap-proxy` 转发时补 `jscode` |

接线：`lib/amap.ts` 在插脚本**之前**设 `window._AMapSecurityConfig = { serviceHost: "<origin>/_AMapService" }`
（顺序错了设置无效，官方文档写明），`next.config.ts` 把 `/_AMapService/*` 转到
`app/amap-proxy/[...path]/route.ts`，由它补上密钥后转发给高德。

**实测（无头 Chrome，真实链路）**：

| 检查 | 结果 |
| --- | --- |
| 页面里是否有明文安全密钥 | `plaintextCodeInPage: false` |
| SDK 是否走了同源代理 | 服务端日志出现 `GET /_AMapService/v3/log/init?...`（无失败请求） |
| 地图是否真的画出来 | `<canvas class="amap-layer">` + 三个标记（`title="广州塔/海心沙/陈家祠"`，编号 1/2/3） |
| 客户端自带 `jscode=ATTACKER` | 被服务端**覆盖**：`sec_code` 与「用真密钥直连高德」逐字符相同 |
| 非 GET 方法 | 405 |
| 缺 `AMAP_SECURITY_CODE` | 503，并说清去哪儿配 |

**代价与已知取舍（如实记录）**：

- **站点连线是直线**：我们只有站点坐标，没有路段几何。图注写明了「虚线只表示先后顺序，不是实际行车路线」——
  要画真路线得上高德 Web服务（需要另一把 Key）；
- **每套方案各一张地图**（最多 3 张），每张占一个 WebGL 上下文；再多就得改成共享一张大地图 + 切换；
- **高德 JS SDK 是外部脚本**：页面会请求 `webapi.amap.com`。没配 Key 时**不加载任何外部脚本**，
  位置改成一句「未配置地图 Key」；加载失败/WebGL 不可用也各有对应的一句话，站点列表始终不受影响；
- **安全密钥走同源代理，意味着前端进程必须由 Next 伺服**（与 §8.6 的同源代理同一个约束）。

### 8.8 ★ 已解释：`.env` 里填好的 Key 不生效（被 shell 里的空值遮蔽）

**症状**：在 `.env` 里把 `TAVILY_API_KEY` 填好，`search_provider_effective` 却仍是 `seed_only`，
`test_search_live.py` 三条全 skip。

**根因**：pydantic-settings 读配置的优先级是「**环境变量 > `.env` 文件**」。
开发机的 shell 里已经 export 了一个**空**的 `TAVILY_API_KEY`，
而空串会被 `Settings._blank_to_none` 归一化成 `None` —— 于是「已设置但为空」
比「`.env` 里有真值」优先，真值永远进不来。

```
$ printenv TAVILY_API_KEY        # → 已导出（0 字符）
$ cd backend && python -c "..."
tavily_api_key            -> None          # .env 里明明填了
$ env -u TAVILY_API_KEY python -c "..."    # 去掉这个空值后
tavily_api_key            -> 已读到，长度 58
search_provider_effective -> tavily
```

**这不是代码 bug，是标准（且合理）的优先级规则**：12-factor 的用意就是让运行环境能覆盖
配置文件（容器/CI 都靠它）。所以修法在环境侧，不在代码侧：

- 确认 shell 里没有多余的 `export XXX_API_KEY=`（`printenv XXX` 为空却是「已设置」时最难发现）；
- 临时验证用 `env -u XXX_API_KEY make ...` 或新开一个 shell。

同一个机制反过来造成了 `make_settings()` 那条测试问题（测试「无 Key 时应降级」却读到了
shell 里的 `DEEPSEEK_API_KEY`，见 TASKS.md 本节）。两处合起来的结论：
**「配置没生效」与「测试环境不干净」可以有同一个原因。**

### 8.9 ★ 一次真实的误判：面板上「未配置」不等于「整条能力没配」（2026-09-15）

**症状**：高德 JS API Key 明明已经配好、地图也实测渲染出来了，
但在 `/dev` 面板上看到「高德 Web Key（未配置）」，于是断定「高德地图没配置」。

**根因**：那是个**同名不同物**的误读 —— 高德要两把不同平台的 Key：

| Key | 位置 | 干什么 |
| --- | --- | --- |
| `NEXT_PUBLIC_AMAP_JS_KEY` + `AMAP_SECURITY_CODE` | `frontend/.env.local` | **结果页那张地图**（Web端 JS API） |
| `AMAP_WEB_KEY` | 仓库根 `.env` | **后端路径规划**（Web服务），目前仍是空的，后端走 OSRM |

面板只写仓库根的 `.env`（后端进程读它），而 Next **只读 `frontend/.env*`** ——
所以前端那两个变量在面板上从来就没地方显示，「未配置」三个字属于另一把 Key。
这直接导致了一个错误结论：**「看不见」被当成了「没配」。**

**修法（三层）**：

1. 面板新增一栏**「前端专用配置（在这里只读）」**：列出 `frontend/.env.local` 里那几个变量
   现在**配没配**、**从哪个文件读到的**（`.env.local` 优先），
   并写明「本面板改不到它们」。后端只报存在性、**不返回值**，与 Secret 的只写不读同一条纪律；
   前端解析时逐字段重建，就算后端多回一个 `value` 也不会被带进界面。
2. `AMAP_WEB_KEY` / `MAP_PROVIDER` 的说明改成明确分工：一个说「只有后端路由用它，
   另一把在 `frontend/.env.local`，见下方那一栏」，一个说「只决定后端算距离走谁，
   与结果页那张地图无关」。两条都有测试钉住。
3. 每个字段右侧加了 **ⓘ 圈感叹号**：悬停/键盘聚焦/点击都能看到「这个字段是干什么的」。
   提示文字**常驻 DOM**（`role="tooltip"` + 控件的 `aria-describedby`），
   所以鼠标、键盘、屏幕阅读器、Ctrl+F 拿到的都是同一段字，且只有一个出处（后端的 `hint`）。
   圈感叹号**刻意放在 `<label>` 外面**：`<label>` 里的可交互元素会被算进控件的无障碍名字，
   屏幕阅读器会把输入框读成「…DEEPSEEK_API_KEY 字段说明」。

**顺着这件事查出来的更大的问题：白名单里有 11 个键改了不会改变任何行为。**

写提示字的过程里被迫逐个回答「那它到底影响什么」，于是发现：

| 旋钮 | 它本该干什么 | 处置 |
| --- | --- | --- |
| `PLAN_` / `SEARCH_COST_CIRCUIT_BREAKER_CNY` | 熔断阈值 | **移除**（归属 `limits.yaml`）；其中一个曾经在 `/health` 上说谎，已单独修掉 |
| `RATE_LIMIT_COLD_PLANS_PER_DAY` | 每会话每日冷规划上限 | **移除**（归属 `limits.yaml`） |
| `MAP_MAX_CALLS_PER_PLAN` | 按次数的地图熔断 | **移除**（归属 `limits.yaml`） |
| `GLOBAL_DAILY_BUDGET_CNY` | 全局日成本上限 | **接上**：以前没人读，现在规划链真的按它熔断（见下） |
| `BACKEND_PORT` | 后端端口 | **移除**：实际端口由 `uvicorn --port` 决定 |
| `API_BASE_URL` | — | **移除**：CORS 用的是 `FRONTEND_URL`，这个键没人读 |
| `SERPER_API_KEY` / `BING_SEARCH_API_KEY` / `ENABLE_LOCAL_FETCH` / `NOMINATIM_USER_AGENT` | 已规划、尚未实现 | **保留并标注**：ⓘ 里写「改了不生效」，`/health` 还会点出被忽略的搜索 Key |

**那个会说谎的旋钮**（最严重的一条，已修）：`/health` 从前报的是熔断阈值的**环境镜像**，
而熔断器读的是 `config/limits.yaml`。把它改成 99，`/health` 就会高高兴兴地显示
「99 元熔断」，而请求仍然在 **1 元**处被拦下 —— 两边默认值恰好相等（1.0 / 0.30）才一直没人发现。
现在 `/health` 只报 `limits.yaml` 里真正生效的两个数，并配了回归测试
（把环境变量改成 99，断言 health 报的仍是 limits.yaml）。

**接上的是全局日成本**（PRD §15.4 第四级熔断：「全局日成本 > 配置上限 ⇒ 全站进入缓存优先模式」）：

- 判定在**建链之前**：链一旦跑起来钱就已经花了；超限时不调模型（`trace.enabled = False`），
  只走本地知识库与缓存；
- 降级理由带上**两个数**：「今日已花 20.43 元，上限 20.00 元」——
  “超预算了”没用，“花了多少/上限多少”才有用；
- `/health` 新增 `cost` 区块：今天的累计支出、上限、是否超限；
  **库读不出来时 `budget_exceeded` 是 `null` 而不是 `false`**（查不到 ≠ 没超，同「null ≠ 0」）；
- `limit <= 0` 视为不设上限（与 `rate_limit` 同一口径）；
- 边界取「到线即算用完」（`>=` 而不是 `>`）：预算是“最多花这么多”，写成 `>` 等于每天都多送一份额度。

为什么这一级不能靠 `CostLedger` 的熔断器代劳：那个账本**一次规划一份**，只看得住一次；
真正的账单失控是“一天里很多次”堆起来的。

**防复发**：`INEFFECTIVE_ENV_KEYS` 把这类键显式登记，三道测试卡住：
①声明与实际必须完全一致（扫源码找消费方，多一个少一个都红）；
②声明了的键**必须在提示里写明「改了不生效」**；③每个字段都必须有说明。
另加一条锁住“镜像键不许回到面板”的测试。四条都做了变异验证
（把 `/health` 改回镜像、拿掉某个声明、删掉某句提示、关掉规划链那一行，确认对应用例真的变红）。

**另附一个小工具**：`app/core/paths.py` 新增 `frontend_dir()`（定位项目根下的 `frontend/`），
面板读前端环境变量时用它，不再各自拼路径。

### 8.10 分享页 OG 图 + JSON-LD（2026-09-15，M5 收尾）

分享链接被微信/微博/Twitter 抓取时要有大图预览（PRD AC-9.5）。实现分三层：

| 层 | 文件 | 干什么 |
| --- | --- | --- |
| 纯逻辑 | `frontend/lib/og.ts` | 选卡上的方案（`recommend_score` 最高，与页面排序同口径）、措辞（缺数据就说缺）、站点坐标归一化、JSON-LD 构造 |
| 渲染 | `frontend/components/og-card.tsx` | 1200×630 卡片：行程卡与「链接不可用」空卡两种形态 |
| 路由 | `frontend/app/t/[slug]/opengraph-image.tsx` | Next 文件约定：自动把 `og:image`（含 width/height/alt）与 `twitter:image` 补进页面 head |

页面 head 里的 `schema.org/TouristTrip` JSON-LD 由 `tripJsonLd()` 生成，
**没拿到的字段不写**（没有方案时 `itinerary` 整键不出现；时间缺失的站只写站名；
超过 8 站只列前 8）—— 结构化数据里的空值会被消费方当真数据。
读不到行程时整个 `<script>` 不输出，失效页额外 `noindex, nofollow`。

**三条实测出来的经验（都发生在真实冒烟里，单测全绿时也发生了）**：

1. **SVG 内 `<text>` 在 satori 里不支持**（报 `<text> nodes are not currently supported`）。
   站点编号改用 HTML 绝对定位叠加在圆点上（HTML 文本没问题）。
2. **多子节点的 `<div>` 必须显式 `display: flex`**；只有一个文本子节点时 satori 同样要求显式声明。
3. **页面 metadata 里手写 `images` 数组会顶掉 opengraph-image 文件约定的自动填充**：
   曾写 `images: [{ url: "", width, height }]`，结果 `og:image` 从 head 里彻底消失。
   不要写 `images`，让文件约定自己说话（这条已写进代码注释与测试）。

**冒烟顺带捞出一个后端真 bug（已修）**：`CacheStore.put_llm` 硬插 LLM 缓存行，
两个并发规划算出同一个 prompt 时后写方撞 `llm_cache.cache_key` 唯一约束 ——
上层 `except` 吞得掉异常，**吞不掉已被失败 flush 污染的 session**，
本次规划随后在落库时必然 500，与「缓存写失败不影响本次结果」的注释直接矛盾。
修法：先查再插，撞键保留原行并刷新 `last_hit_at`（集成回归测试
`test_llm_cache_duplicate_write_does_not_poison_session` 钉住：重复写之后
继续 flush / 查询必须正常，只断言「不抛异常」抓不到会话已污染的形态）。

**部署注意**：`SITE_URL`（根目录或 `frontend/.env` 均可，Next 只读后者）
生产必须设成对外可访问的地址，否则 `og:image` 解析成 `http://localhost:3000/...`，
社交平台的预览图会裂。`sharp` 已在 `dependencies`（生产渲染 OG 图用它）。

**实测（前后端 + 真实知识库，`next start`）**：`og:image/width/height/alt` 与
`twitter:card=summary_large_image`、`twitter:image` 全部出现在 head；
JSON-LD 三站带时间窗；行程卡与空卡是**不同的两张图**（像素差异 14.5%，不是缓存串了）；
失效 slug 的 OG 图是真话空卡（200 PNG），页面 `noindex`。

### 8.11 性能与安全检查怎么跑（2026-09-16，M7）

三个命令，分属三种性质（不要混成一个）：

```bash
make security   # 密钥泄露模式扫描 + domain 层纯净性（20 条 AST 断言）—— 秒级
make perf       # 压测 + 前端首屏/LCP，重写 docs/PERF_REPORT.md —— 约 3 分钟
make check      # 那几条秒级的性能门槛已经在这里面（不必等 perf）
```

`make perf` 做四件事，四个都能单独跑：

| 目标 | 做什么 | 需要什么 |
| --- | --- | --- |
| `make perf-build` | `pnpm build`，输出留档到 `.perf/next-build.log`（报告要从它取首屏 JS） | 无 |
| `make perf-lcp` | 起前后端（已经在跑的就复用）→ 造一条**真实**的分享行程拿 slug → 量首页与分享页 LCP | Postgres + 已建库 + Playwright 的 chromium |
| `make perf-backend` | 冷/缓存/修改/并发/束搜索/慢 SQL + 生成报告 | Postgres + 已建库 |
| `make perf` | 以上三件的顺序执行 | 同上 |

产物分两处：**结论**在 `docs/PERF_REPORT.md`（要入库，给人看），
**原始证据**在 `.perf/`（不入库：`next build` 日志、`web-vitals.json`、`perf.json`、服务日志）。
`make perf` 里的 `perf-lcp` 前面有一个 `-`：LCP 采集失败（比如后端被限流、Chromium 没装）
不该连累后端那份报告，报告会在"其他核对项"里如实写"分享页 LCP 未测"。

**报告里的三条纪律**（都是有代价才定下来的）：

1. **没测到的门槛不许打勾**。首页 TTI 曾经长期写着 `⏭ 不可测`（拿 `domInteractive` 之类的
   东西顶替会得到一个偏低且含义不同的数），现在它有了真读数：判定是纯函数 `compute_tti_ms`
   （7 条单测），采集侧收长任务 **与** Resource Timing 的在途区间两半 —— 少了后半会系统性偏乐观。
   顺带记住：**TTI 与 LCP 不可互推**（实测 TTI 569ms < LCP 中位 2592ms，Hero 视频继续画帧并不阻塞主线程）。
2. **样本量与读数一起报**。分位数用最近秩（rank = ceil(q·n)），n=10 时 p95 就是最大的那个样本。
3. **第三方延迟不进门槛**。PRD 的"冷启动 ≤ 8s"用**关掉 LLM**的规则引擎路径判（确定性、可复现），
   开着 LLM 的读数另报一行观察值 —— 否则这道门槛会随上游当天的心情浮动。
4. **纯计算的耗时门槛取"重复的最好一次"**。CPU 噪声是单向的（同机的开发服务器、浏览器只会让它更慢）：
   束搜索曾因 `[223.6, 326.17, 500.59]`ms 里那 **0.59ms** 越过 500ms 而变红，算法却一字未改。
   现在每种 archetype 重复 3 次取最好一次（`best_and_worst()`），**最差一次照旧写进报告**。

秒级的那部分在 `backend/tests/integration/test_perf_budget.py`（17 条）：
门槛数值按 PRD 原文写在测试里、再与 `scripts/benchmark.py` 的常量互相钉住
（直接引脚本常量的话，把 8s 改成 30s 测试也照样绿）；TTI 的判定逻辑 7 条；
另有三条守"报告不说假话"。

> ⚠️ **写 shell 脚本时注意**：`"$PERF_PORT）"` 这种"变量紧跟全角标点"会让 bash 把标点的首字节
> 读进变量名，报出来是一句看不懂的 `PERF_PORT<乱码>: unbound variable`（本轮首次运行就挂在这里）。
> 一律写 `${PERF_PORT}`。

### 8.12 关系图里有多少对能拿到「真实路网」（2026-09-16 更新）

`make relations OSRM=1` 会把一部分地点对的直线估算换成真实路网距离。
它**永远不可能全覆盖**，原因是结构性的：

- OSRM 的 `table` 接口一次只算**一批点内部**的矩阵（`batch_size: 25`），
  跳批次的候选对按设计退回估算；
- 所以能拿到多少，**取决于怎么分批**。

分批方式的差别是数量级的（同一份 5445 对候选、同一台机器、同样 32 次请求）：

| 分批方式 | 真实路网对数 | 占比 | 失败批次 | 备注 |
| --- | --- | --- | --- | --- |
| 按 UUID 排序切连续块（旧） | 130 | 2.4% | 0 | 与地理位置无关；重跑建库重新编号 → 数字会漂（历史值 163 / 161 / 146 / 130） |
| 按候选关系图贪心聚类（现） | **4085** | **75.0%** | 0 | `compute_relations.cluster_batches`；不改接口、不调 `batch_size` |

**关键读法**：两次里"实际拿到"与"批内上限"都是**逐对相等**、失败批次为 0 ——
批内能拿到的都拿到了，剩下的 25% 是分批尺寸的固有代价（一批只能装 25 个点），
不是网络失败。`docs/RELATION_REPORT.json` 把这几个数**分开**记
（`osrm_pairs` / `osrm_eligible_pairs` / `osrm_batches_failed` / `osrm_batches` /
`use_osrm` / `generated_at`），就是为了一眼看出来是哪一种。

> 口径提醒：公共 OSRM 演示实例只有 car profile，所以 `--osrm` 换来的是**真实路网距离**
> 与**行车耗时**；`walk` / `metro` / `bike` 的耗时仍由真实距离 × `limits.yaml` 的速度推导，
> 逐条记在 `derived_modes` 里并标 `estimated`。距离变真了，时间仍然老实标着是推算值。

> 因此：**别把覆盖率数字抄进别的文档** —— 它一重算就变了。
> `docs/DATA_REPORT.md` 里那一段已经改成只讲结构与后果，数字指向上面那份 JSON。

两个跟它挨着的坑：

- **重跑建库会静默清空关系图**（`place_relations` 对 `places` 是级联删除），
  所以 `seed` 之后**必须**再跑一次 `make relations`，否则规划会退化成“没有任何关系数据”
  （不会报错，只是建议质量变差——更难发现）。
- **`kb_version` 里带日期**（`gz-YYYY.MM.DD-<内容摘要>`）：内容没变时摘要不变，
  但日期会变。所以文档里的版本号只在本日建库后才是对的 —— 对不齐时先看
  `SELECT version FROM kb_versions ORDER BY created_at DESC LIMIT 1`，别去改文档。

### 8.13 试用时觉得哪个数字不真实，先来这张表定位（2026-09-16）

这些**全都不是报错**，是"我们输出了一个数、但我们其实不知道它"。
每一条都对应一处可审计的配置或数据源，改口径就去改那里：

| 现象 | 真正的原因 | 去哪看 / 改 |
| --- | --- | --- |
| 预算低得离谱（例：两顿老字号只报 ¥19.59/人，而那正是那段打车费） | 库里 **0/3166** 条地点有价格；旧逻辑把"有餐饮站点但没价"当成"不计入" | `app/domain/budget.py`；餐费单价在 `config/limits.yaml` 的 `budget.meal_cost_cny`。现在推定值逐项进 `estimated_items`（与 `unknown_items` 分开） |
| 每站停留时长一模一样，换 pace 也不变 | `pace` 以前**只**影响步行上限，不影响任何一站待多久 | `limits.yaml` 的 `planning.stay_scale_by_pace`（relaxed 1.3 / balanced 1.0 / packed 0.85） |
| 每个站点都写「营业时间未知」 | OSM 覆盖率只有 16% 有原文，解得出时段的又只有其中一半（267/509） | 解析器 `app/domain/opening_hours.py`（**读时解析**，不落库）；解不出的**不猜** |
| 通勤全是「（估算值）」，距离明显偏短 | 关系图里只有少数对拿到了真实路网距离 | 先看 `docs/RELATION_REPORT.json` 的 `osrm_pairs`；重跑 `make relations OSRM=1`（分批方式见 §8.12） |
| 三个站点的推荐理由都是同一个词（如「美食」） | 旧文案把**用户自己勾的偏好**当理由复述回去 | `_stop_why`（`app/services/plan_service.py`）；现在带分值 + 地点标签，两座同类茶楼能被区分开 |

一条共同的读法：看结果页上一个数字时，先问它**从哪来**。界面上的 `（估算值）`、
`含估算：`、`未含价格：` 与 `transport_source` 不是装饰，它们就是这三个问题的答案。

---

## 9. 排障

| 症状 | 原因 / 处理 |
| --- | --- |
| 前端首页显示「未连接到后端服务」 | 后端没起：`make dev-backend`；或 `NEXT_PUBLIC_API_BASE_URL`（写在 `frontend/.env.local`）指错了地址 |
| `psql: FATAL: role "..." does not exist` | `.env` 里的用户名还是作者的 `tanjiaxi`，改成 `whoami` |
| `make db-up` 报 compose 文件找不到 | 仓库无 `docker-compose.yml`，改用 `brew services start postgresql@16` |
| 首页点「开始规划」报 404/NOT_FOUND | 后端未启动或端口/地址不对（M4 已完成，这个接口是存在的） |
| 首页点「开始规划」报 422 `INVALID_INPUT` | 后端认为输入不合法：看 `hint` 里的合法取值。表单正常情况下不会触发它 |
| 点「开始规划」显示「已生成 N 套方案」但看不到路线 / 取回行程报 403 `FORBIDDEN` | 会话对不上：确认前端请求的是**同源** `/api/*`（由 Next 代理转发，见 §8.6），而不是直连 `127.0.0.1:8000`。清掉 `td_session` cookie 会换成一个新游客会话（旧行程按会话隔离，读不回来） |
| 想反馈后端错误 | 每个响应都带 `X-Request-Id`（响应体 `meta.request_id`），带上它去查后端日志 |
| 后端日志出现「降级模式生效」warning | 正常，说明缺 Key；填 Key 后重启即可 |
| `/dev` 面板显示「高德 Web Key（未配置）」 | **不代表地图没配**：那是后端路由用的「Web服务」Key；结果页那张地图用的是 `frontend/.env.local` 里的「Web端(JS API)」Key —— 看面板下方「前端专用配置（在这里只读）」那一栏（§8.9） |
| 面板上某个旋钮改了却什么也没发生 | 先看它 ⓘ 里有没有「改了不生效」；成本/限流阈值不在这张表里，它们在 `config/limits.yaml`（§8.9） |
| 想改熔断阈值 / 限流 / 日预算 | 改 `config/limits.yaml`（面板下一张卡片可直改，先校验后落盘）；`.env` 里那些同名变量没用（§8.9） |
| `/health` 里 `cost.budget_exceeded` 是 `null` | 库读不出来，**不等于没超预算**（§8.9） |
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
