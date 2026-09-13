# 代码审查报告 — TripDecider M0 + M1

> 审查时间：2026-09-10
> 审查范围：M0 脚手架（配置 / 后端骨架 / 迁移 / 前端骨架）+ M1 广州知识库（采集 / 丰富化 / 建库 / 质检 / API）
> 结论：**发现 9 项缺陷（2 项高、3 项中、4 项低），全部已修复并配回归测试；后端测试 212 → 576，覆盖率 90%（失真）→ 100%**

---

## 一、执行摘要

| 指标 | 审查前 | 审查后 |
| --- | --- | --- |
| 后端测试 | 212（145 unit + 67 integration） | **593（487 unit + 106 integration）** |
| 前端测试 | 23 | **93** |
| 后端覆盖率 | 90%（**测量失真**，真实约 95%） | **100%**（1703 语句 / 0 未覆盖） |
| 前端覆盖率 | 无工具、无门槛；`lib/api.ts` 零覆盖 | 工具配置就绪但 **provider 装不上（环境受阻）**，见遗留项 4 |
| mypy strict | 39 文件 0 错 | 49 文件 0 错 |
| ruff | 0 问题 | 0 问题 |
| 覆盖率闸门 | 无 | `--cov-fail-under=93`（可覆盖） |

审查的第一步不是读代码，而是**验证文档里的数字**。项目的 README 写着"91% 覆盖率"，
实测发现这个数字本身是错的 —— 这是本轮最有价值的发现。

---

## 二、缺陷清单

### 🔴 R1 覆盖率配置缺陷：async 端点覆盖率被系统性低估

**现象**：`make test-cov` 报告 `app/api/v1/catalog.py` 覆盖率仅 **54%**（59 行未覆盖），
但该文件的 23 个集成测试全部通过、接口返回真实数据。未覆盖的行**聚集在 `await` 之后** ——
函数开头几行被执行，`await` 之后的代码全部记为未覆盖。

**根因**：SQLAlchemy 的 async 引擎内部通过 **greenlet** 切换执行上下文（`greenlet_spawn`），
而 coverage.py 默认只跟踪 **thread**。协程被挂起再恢复后，后续代码不在追踪范围内。

**定位过程**（两步排除法，避免猜测）：

1. 用 `TestClient` 打端点 → 覆盖率 54%（异常）
2. 直接 `await` 调用同一个协程函数 → 覆盖率 67%，且该文件全部端点行都被正确记录（正常）

两者差异只在"是否经由 `TestClient` 的 portal 线程"，据此锁定 greenlet/thread 问题。
再以 `concurrency = ["thread", "greenlet"]` 验证，`catalog.py` 立刻从 54% 跳到 **98%**。

**修复**：`backend/pyproject.toml`

```toml
[tool.coverage.run]
concurrency = ["thread", "greenlet"]
```

**为什么这比"数字低 5 个点"严重得多**：失真的覆盖率会**淹没真实缺口**。
真正未覆盖的分支与大量假阴性混在一起无法分辨，覆盖率作为质量闸门的意义就消失了 ——
例如 `catalog.py` 里真实未覆盖的 3 行（两个空输入守卫、一个行政区过滤分支）
在修复前根本无从识别。

**附带改进**：`make test-cov` 加 `--cov-fail-under=$(COV_MIN)`（默认 93），
并把 `make check` 从 `lint + typecheck + test` 改为 `lint + typecheck + test-cov + test-frontend`，
使闸门真正生效且只跑一遍测试。

---

### 🔴 R2 未捕获异常时 `request_id` 丢失，而错误提示却让用户反馈它

**现象**：`app/main.py` 的请求中间件在异常分支里先 `set_request_id(None)` 再重新抛出，
于是最外层的未捕获异常处理器读到 `None`：

- 响应体 `meta.request_id` 为 `null`
- 响应头**没有** `X-Request-Id`

**而错误提示原文是**：

> 若持续出现，请把响应头里的 X-Request-Id 反馈给我们。

等于让用户反馈一个不存在的东西 —— 用户报障时我们无法定位。

**修复**：异常分支不再清空 `request_id`（每个请求开始时都会重设，不会串号）；
`_unhandled` 处理器把 `X-Request-Id` 补进响应头。

---

### 🟡 R3 5xx 响应缺少安全响应头与 CORS 头

**根因**：Starlette 的 `ServerErrorMiddleware` 位于**所有自定义中间件之外**，
未捕获异常产生的 500 响应会绕过 `_request_context`（安全头）与 `CORSMiddleware`（CORS 头）。

**影响**：前端在浏览器里读到的是"跨域被拦"这种**不可读的失败**，
而不是我们精心写的统一错误 envelope。用户只会看到一个空白页。

**修复**：`_unhandled` 处理器手动补齐安全头 + 按白名单回显 `Origin`；
CORS 白名单抽成 `allowed_origins(settings)` 供中间件与异常处理器共用，避免两处各自演化。

---

### 🟡 R4 `assert_safe_for_production` 里有一条永远不触发的检查

```python
if self.admin_token == "":          # ← 永远为假
    problems.append("ADMIN_TOKEN 为空（后台无保护）")
```

`_blank_to_none` 校验器已把空字符串归一化成 `None`，因此 `== ""` 永不成立 ——
**"后台无保护"这条生产环境检查实际上从未生效过**。

**修复**：改为 `if not self.admin_token:`。

> ⚠️ **这是行为变更**：生产环境未设置 `ADMIN_TOKEN` 时，应用现在会**拒绝启动**
> （此前会静默启动）。这与该检查的注释意图一致，但需要知情。
> 开发环境不受影响（`is_production` 为假时不抛错）。

---

### 🟡 R5 `travel_minutes` 取整少算 1 分钟

```python
return max(1, int(minutes + 0.999))   # 想实现 ceil，但...
```

当分钟小数部分落在 `(0, 0.001]` 时，`int(x + 0.999)` 返回 `floor` 而非 `ceil` ——
**少算 1 分钟**。这个函数是路线时长的来源，少算会让用户低估行程。

**修复**：`ceil(round(minutes, 6))` —— 先按 1e-6 规整再取整。
规整这一步是必需的：直接 `ceil` 会被浮点噪声反噬（实测 10–60000 米 × 14 种速度的穷举中
有 371 组输入会产生 `18.000000000000004` 这类值，被凭空多算一分钟）。

---

### 🟢 R6–R9 文档与死代码

| # | 问题 | 处理 |
| --- | --- | --- |
| R6 | `point_in_polygon` 的 docstring 声称"边界上的点视为在内"，实际只保证**顶点命中**，边中点不保证 | docstring 写清确切语义与取舍理由；新增测试钉住实际行为（而非修改实现） |
| R7 | `enrich_or_reason` 的 docstring 过滤顺序与代码不一致（坐标范围实际在名称黑名单**之前**） | docstring 改为与代码逐条对应的编号列表，并说明为何行政归属判定必须早于保护名单 |
| R8 | `main.py` 注释"add_middleware 后加的更外层，因此 CORS 在内"与事实相反 | 从 traceback 的中间件调用链确认实际顺序为 CORS 在外层，更正注释并说明这是刻意设计 |
| R9 | `geo.py` 的 `bbox_contains` 与 `DEFAULT_ROUTE_FACTOR` 无任何调用方 | **保留**（属对外导出，M2 可能使用），补测试并加注释标注"当前无调用方" |

关于 R6 的取舍说明：**没有**给"点落在边上"加容差。坐标来自 OSM（6 位小数，约 0.1 米级），
恰好落在边线上的概率可忽略；而引入一个任意的 epsilon 会让城市边界判定变得不可复现 ——
这比"边中点偶尔判为外部"更危险。测试里同时钉住了"顶点命中"与"边中点返回 False"两个行为。

---

### 前端（M0-5）补充审查

前端此轮只做了测试运行，代码是第二轮才读的。整体质量与后端一致（诚实性标注、降级提示、
错误分支都有处理），发现两处缺陷：

#### 🟡 R10 `engineNote` 把「接口没实现」与「输入被拒绝」混为一谈

```ts
if (failure.status === 404 || 405 || 501 || 422 || 400) {
  return "规划引擎正在开发中（M2–M4 里程碑）。上面的需求已经被前端完整校验，…";
}
```

`422` 恰恰意味着**后端认为这份需求不合法** —— 此时告诉用户"引擎在开发中"、
并附上"需求已被前端完整校验"，会把用户引向「等引擎上线」而不是「改输入」，
而后者才是他能采取的行动。

**修复**：拆成两组状态码。`404/405/501` → "引擎开发中"；`400/422` → "后端认为这份需求不合法"；
其余 → 中性说明（不编造原因）。

#### 🟢 R11 `formatDuration` 用真假值判断，把 `0` 与 `null` 混为一谈

```ts
function formatDuration(minutes: number | null): string {
  if (!minutes) return "时长未知";   // ← 0、NaN、null 一起命中
```

同一文件里的 `formatDistance` 用的是显式 `meters === null`，两者不一致。
这是与后端 R4 完全同类的模式：**真假值判断吞掉了语义上不同的取值**。

**修复**：显式判断 `null / 非有限数 / <= 0`；`formatDistance` 同样补上负数与 `NaN` 防护。
两个函数从 `app/explore/[city]/page.tsx`（async 服务端组件，难单测）**提取到 `lib/format.ts`**，
使其可被单测覆盖 —— 它们恰恰是最该测的一类代码（单位切换点、边界值决定用户看到的是"未知"还是一个错误数字）。

#### 未修但值得知道

- `parseExample` 的天数/人数支持中文数字（"两天"），预算只支持阿拉伯数字。
  属可接受的不对称，但值得在注释里说明。

---

### 第三轮：遗留项清零

第二轮收尾时留下了几条"标记但未处理"的项，这里逐条处理：

#### 🟡 R12 `list_cities` 的 N+1 查询

原实现对**每个城市**各发 3 条查询（地点数 / 路线数 / 最新知识库版本），
即 1 + 3N —— 城市数从 1 涨到 20，查询数就从 4 涨到 61。

**修复**：改成 3 条批量聚合查询（`GROUP BY city_id` 两次 + PostgreSQL 的
`DISTINCT ON (city_id)` 取每城最新版本），与城市数无关。

**为什么必须配"查询次数"测试**：N+1 与批量聚合两种实现的**响应体完全一样**，
任何基于响应内容的断言都抓不到回归；而且当前只有广州一个城市时 N+1 也看不出来。
所以新增的测试是：临时插入一个城市，用 `before_cursor_execute` 事件数一数实际发了几条 SQL，
断言"查询次数不随城市数增长"。实测确认该端点稳定在 4 条（1 条城市 + 3 条聚合）。

#### 🟢 R13 来源链接未校验协议

`PlaceCard` 直接把 `place.sources[0].url` 放进 `<a href>`。
`javascript:` / `data:` 协议在点击时会执行脚本 —— 当前 URL 全部由抓取脚本构造成
OSM 的 https 链接，实践上安全，但缺少防御纵深。

**修复**：新增 `safeExternalUrl()`（仅放行 http/https，返回规范化后的 URL）。
非法协议时**降级为纯文本**并保留来源署名 —— 来源可追溯是硬性要求，
不能因为链接不可用就把出处一起抹掉。

#### 🟢 R14 `categories.py` 缺少一致性测试

该模块自称"单一事实源"，被数据库 CHECK 约束、配置校验、评分三处消费，
但没有任何测试守住"三处是否一致"。

**新增 `tests/unit/test_categories.py`（14 个用例）**，其中最有价值的一条是
**"每个类别都必须能从 OSM 标签推导出来"** —— 实测踩过两次：
`photo` 与 `citywalk` 曾经都没有任何产生者，类别名留在枚举里但永远是空的。
`citywalk` 至今仍是刻意的例外（"适合 CityWalk"是属性而非类型），
测试用 `INTENTIONALLY_UNPRODUCED` 显式记录原因，并禁止它悄悄多出别的条目。

#### 🟢 R15 `explore` 页服务端组件未直接测试

`app/explore/[city]/page.tsx` 是 async 服务端组件，此前完全靠人工点开页面验证。

**新增 18 个测试**：它本质上是 `async (props) => JSX` 的纯函数，
直接 `await ExplorePage({...})` 拿到 React 树再渲染即可，不需要起 Next.js 运行时。
覆盖诚实性标注（营业时间未知 / 票价未知 / 估算值 / 评分来源）、
后端不可用（网络错误与语义化 API 错误两条路径）、空结果、分页提示、
搜索词回显、类别 chip 的 `aria-current`、来源链接的安全降级。

---

## 三、新增测试

| 文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/unit/test_geo.py` | 62 | **此前该模块零专用测试**，而它正是最严重数据 bug（矩形 bbox 切进邻市）的修复核心 |
| `tests/unit/test_enrichment.py` | 132 | **此前零专用测试**。知识库的唯一入口，全部过滤规则 |
| `tests/unit/test_config_validators.py` | 128 | 全部 fail-fast 校验分支 + 人工数据 Schema 非法输入 |
| `tests/unit/test_categories.py` | 14 | 类别枚举的"单一事实源"一致性：每个类别都要有产生者、有中文名、有分值/时长/标签基线 |
| `tests/integration/test_error_handling.py` | 36 | 中间件、异常处理、错误码语义、故障注入 |
| `tests/integration/test_catalog_api.py` | +3 | **查询次数（N+1 回归）**、聚合默认值、批量化后统计值不变 |
| `tests/unit/test_paths.py` | 6 | 项目根目录定位失败必须抛错 |
| `frontend/lib/__tests__/api.test.ts` | 23 | **前端唯一的网络出口，此前零覆盖**。envelope 拆包、四类错误映射、requestId 优先级、URL 构造与编码 |
| `frontend/lib/__tests__/format.test.ts` | 23 | 格式化边界值（`null`/`0`/负数/`NaN`/1000 米切换点）+ 外链协议白名单 |
| `frontend/app/explore/[city]/__tests__/page.test.tsx` | 18 | **服务端组件此前未测**。诚实性标注、后端不可用两条路径、空结果、搜索回显、来源链接降级 |
| `frontend/components/__tests__/planner-form-submit.test.tsx` | +6 | 区分「接口未实现」与「输入被拒绝」的回归 |
| **合计** | **451** | |

### 重点覆盖的边界场景

**`test_geo.py`** —— 射线法内部/外部/凹多边形、顶点命中 vs 边中点的语义差异、
洞（飞地）、多外环并集、闭合环与退化环、winding 方向无关性、haversine 基准值
（1 度纬度 ≈ 111.19 km、经度弧长按 cos(φ) 收缩）、`travel_minutes` 的 ceil 契约与浮点噪声防护、
`route_factor` 阈值（300 / 1500 边界）、`CityBoundary` 空外环必须抛错。

**`test_enrichment.py`** —— 全部 11 条 `DropReason` 各有一个"必被拦下"的用例；
边界判定**优先于**人工保护名单（深圳的动物园即使被保护也照样丢弃）；
`中山` 前缀**不**误伤中山纪念堂/国立中山大学旧址；连锁品牌整条过滤；
类别覆盖顺序（先命中先用）与前缀兜底（`historic=city_gate` 回归）；
信号 AND 语义与 `when_category_in`；`set_min`/`set_max` 的"保底/封顶"语义（用合成配置覆盖）；
分值裁剪与 3 位小数；`unknown_fields` 与**字段级溯源**（OSM 没有价格信息，
`price_min` 就绝不能出现在 `field_scope` 里）；`district` 只接受 `addr:district`（回归）。

**`test_config_validators.py`** —— 每个校验器都构造一个**必被拒绝**的输入。
这比"真实配置能加载"重要得多：写错的校验器在真实配置下永远不会暴露。
用"读真实 YAML → 注入非法值 → `model_validate`"的方式做基线，避免手写 payload 腐坏。

**`test_error_handling.py`** —— 404 的**路径语义**错误码（`/places/` → `PLACE_NOT_FOUND`、
`/trips/` → `TRIP_NOT_FOUND`、`/public/` → `SHARE_NOT_FOUND`）；
未捕获异常的统一 envelope 且不泄露堆栈；校验错误只回显字段摘要而非完整输入体；
CORS 头对错误响应可见（钉住中间件顺序）；health 的三条故障路径
（数据库不可用 / 迁移未跑 / ready 返回 503）。

---

## 四、覆盖率变化

| 模块 | 审查前 | 审查后 |
| --- | --- | --- |
| `app/api/v1/catalog.py` | 54%（**失真**） | 100% |
| `app/api/v1/health.py` | 56% | 100% |
| `app/main.py` | 86% | 100% |
| `app/core/config.py` | 93% | 100% |
| `app/domain/enrichment.py` | 94% | 100% |
| `app/domain/geo.py` | 92% | 100% |
| `app/schemas/curated.py` | 88% | 100% |
| **TOTAL** | **90%（失真，真实约 95%）** | **100%** |

---

## 五、验证

```bash
make check          # lint + typecheck + 覆盖率闸门 + 后端测试 + 前端测试
```

结果：

```
ruff check .                All checks passed!
mypy app scripts tests      Success: no issues found in 49 source files
pytest                      593 passed
Required test coverage of 93% reached. Total coverage: 100.00%
pnpm test --run             93 passed (7 files)
pnpm typecheck / lint       无输出（通过）
pnpm build                  编译成功；/ 与 /about/data 静态预渲染，/explore/[city] 按设计动态渲染
```

---

## 六、遗留（如实记录，含受阻项）

1. **100% 不等于"产品逻辑都测过了"** —— M2 规划内核（`domain/feasibility.py`、
   `domain/scoring.py`）尚不存在。PRD 要求它们 ≥95% 行覆盖 / ≥90% 分支覆盖，
   要等 M2 落地才谈得上。
2. **覆盖率闸门设为 93%**（留 2 个点缓冲）。实际已达 100%，建议收紧：
   `make check COV_MIN=98`，或直接改 `Makefile` 里的 `COV_MIN`。
3. **R4 是行为变更**（生产环境缺 `ADMIN_TOKEN` 会拒绝启动），需确认符合部署预期。
4. ⛔ **前端覆盖率 provider 装不上（环境受阻，未完成）**。
   `frontend/vitest.config.ts` 的 `coverage` 配置与 `make test-frontend-cov` 目标都已就绪，
   但 `@vitest/coverage-v8` 无法安装：沙箱通过 `NODE_OPTIONS` 注入的 Node 文件系统 shim
   拒绝写入工作区外的 pnpm store（`CODEBUDDY_BROKER_DENY` / EEXIST），
   提权也无效（它是 Node 层 preload，不是 OS 沙箱）。
   刻意**没有**绕过安全 shim，也没有手工改 `package.json`（会造成 lockfile 失配）。
   用户侧一条命令即可启用：

   ```bash
   cd frontend && pnpm add -D @vitest/coverage-v8@3.2.4
   pnpm test --run --coverage     # 先看实测数字，再把阈值填进 coverage.thresholds
   ```

5. `parseExample` 的天数/人数支持中文数字（"两天"），预算只支持阿拉伯数字。
   属可接受的不对称（三个内置示例都用阿拉伯数字），已在代码注释里说明。
6. 沙箱/受限环境下的两个已知噪声：`pytest` 的 `tmp_path` 可能因 basetemp 属主问题报
   `PermissionError`（加 `--basetemp=/tmp/pytest-td`）；`next build` 可能因文件系统代理
   报 `EEXIST`（先 `rm -rf .next`）。两者都是执行环境问题，**不是项目缺陷**。
