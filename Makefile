SHELL := /bin/bash
.DEFAULT_GOAL := help

BACKEND  := backend
FRONTEND := frontend
PYTHON   := 3.12

DB_NAME  ?= tripdecider_dev
DB_TEST  ?= tripdecider_test
PG_HOST  ?= 127.0.0.1
PG_PORT  ?= 5432
PG_USER  ?= $(shell whoami)

# 覆盖率闸门。当前真实水平约 95%，留 2 个点缓冲：既能挡住"悄悄拉低覆盖率"的改动，
# 又不会因为无关的小改动频繁失败。要收紧时改这里（或 make check COV_MIN=95）。
COV_MIN  ?= 93

UV   := uv
PIP  := cd $(BACKEND) && uv run --no-sync

# 跑 backend/scripts/*.py 时必须显式带 PYTHONPATH=.：裸 python 执行脚本时 sys.path[0] 是脚本
# 所在目录（backend/scripts/），而 `app` 包在 backend/app/，于是 import 不到
# （uvicorn 靠 cwd、pytest 靠 pyproject 的 pythonpath 各自绕过了这一点，裸 python 两者都不占）。
# 见 RUNNING.md §8.1。
SCRIPT_ENV := PYTHONPATH=.

.PHONY: help
help: ## 显示所有可用命令
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── 安装与环境 ──────────────────────────────────────────────────────────────
.PHONY: setup
setup: ## 首次安装：Python/Node 依赖 + 建库 + 迁移 + 生成 .env
	$(UV) python install $(PYTHON)
	cd $(BACKEND) && $(UV) sync
	cd $(FRONTEND) && pnpm install
	@test -f .env || cp .env.example .env
	$(MAKE) db-create
	$(MAKE) migrate
	@echo ""
	@echo "✅ setup 完成。请检查 .env（尤其 DATABASE_URL 的用户名），然后运行：make dev"

.PHONY: db-create
db-create: ## 创建开发库与测试库（已存在则跳过）
	@psql -h $(PG_HOST) -p $(PG_PORT) -U $(PG_USER) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$(DB_NAME)'" | grep -q 1 \
		|| { createdb -h $(PG_HOST) -p $(PG_PORT) -U $(PG_USER) $(DB_NAME); echo "created database $(DB_NAME)"; }
	@psql -h $(PG_HOST) -p $(PG_PORT) -U $(PG_USER) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$(DB_TEST)'" | grep -q 1 \
		|| { createdb -h $(PG_HOST) -p $(PG_PORT) -U $(PG_USER) $(DB_TEST); echo "created database $(DB_TEST)"; }

.PHONY: db-drop
db-drop: ## 删除开发库（危险，需 CONFIRM=yes）
	@test "$(CONFIRM)" = "yes" || { echo "拒绝执行：请用 make db-drop CONFIRM=yes"; exit 1; }
	dropdb -h $(PG_HOST) -p $(PG_PORT) -U $(PG_USER) --if-exists $(DB_NAME)

.PHONY: db-reset
db-reset: ## 重建开发库并重新迁移 + 灌入种子数据 + 重算关系图
	$(MAKE) db-drop CONFIRM=yes
	$(MAKE) db-create
	$(MAKE) migrate
	$(MAKE) seed
	@# 关系图必须在地点之后算：place_relations 对 places 是级联删除，
	@# 重新建库会把关系图一起清掉（这里踩过一次，所以写进注释与目标顺序）。
	$(MAKE) relations

.PHONY: db-up
db-up: ## 用 Docker 起 Postgres（需要 Docker daemon 运行）
	@docker info >/dev/null 2>&1 || { echo "❌ Docker daemon 未运行。本项目开发默认使用本机 Postgres："; echo "   brew services start postgresql@16"; exit 1; }
	docker compose up -d db

.PHONY: db-status
db-status: ## 查看本机 Postgres 连通性与库信息
	@psql -h $(PG_HOST) -p $(PG_PORT) -U $(PG_USER) -d $(DB_NAME) -c "select current_database(), version();" 2>&1 | head -5

# ── 迁移 ────────────────────────────────────────────────────────────────────
.PHONY: migrate
migrate: ## 应用全部迁移到开发库
	cd $(BACKEND) && $(UV) run alembic upgrade head

.PHONY: migrate-test
migrate-test: ## 应用全部迁移到测试库
	cd $(BACKEND) && ALEMBIC_DATABASE_URL="postgresql+asyncpg://$(PG_USER)@$(PG_HOST):$(PG_PORT)/$(DB_TEST)" $(UV) run alembic upgrade head

.PHONY: test-setup
test-setup: ## 准备测试环境（建测试库 + 迁移）
	$(MAKE) db-create
	$(MAKE) migrate-test

.PHONY: migration
migration: ## 自动生成迁移：make migration M="add xxx"
	@test -n "$(M)" || { echo "用法：make migration M=\"描述\""; exit 1; }
	cd $(BACKEND) && $(UV) run alembic revision --autogenerate -m "$(M)"

.PHONY: migrate-check
migrate-check: ## 校验 models 与迁移是否一致（不一致会失败）
	cd $(BACKEND) && $(UV) run alembic check

# ── 知识库 ──────────────────────────────────────────────────────────────────
.PHONY: fetch-osm
fetch-osm: ## 抓取广州 OSM 真实 POI 原始数据
	cd $(BACKEND) && $(SCRIPT_ENV) $(UV) run python scripts/fetch_osm_guangzhou.py

.PHONY: fetch-extras
fetch-extras: ## 定向补抓人工清单里的街区/岛屿/村落（B1 缺口）
	cd $(BACKEND) && $(SCRIPT_ENV) $(UV) run python scripts/fetch_osm_curated_extras.py

.PHONY: fetch-transport
fetch-transport: ## 定向补抓交通枢纽：地铁站/公交站/轮渡（B2 缺口）
	cd $(BACKEND) && $(SCRIPT_ENV) $(UV) run python scripts/fetch_osm_transport.py

.PHONY: seed
seed: ## 幂等灌入广州知识库（地点 + 关系 + 路线）
	cd $(BACKEND) && $(SCRIPT_ENV) $(UV) run python scripts/seed_guangzhou.py

.PHONY: validate
validate: ## 知识库质检（门槛不达标会非零退出）
	cd $(BACKEND) && $(SCRIPT_ENV) $(UV) run python scripts/validate_seed.py

.PHONY: relations
relations: ## 重算地点关系图（默认离线估算；OSRM=1 用真实路网距离）
	cd $(BACKEND) && $(SCRIPT_ENV) OSRM=$(OSRM) $(UV) run python scripts/compute_relations.py $(if $(OSRM),--osrm,)

# ── 开发服务 ────────────────────────────────────────────────────────────────
.PHONY: dev
dev: ## 并行启动后端(:8000)与前端(:3000)
	@echo "后端 → http://127.0.0.1:8000/docs   前端 → http://localhost:3000"
	@$(MAKE) -j2 dev-backend dev-frontend

.PHONY: dev-backend
dev-backend: ## 只启动后端
	cd $(BACKEND) && $(UV) run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

.PHONY: dev-frontend
dev-frontend: ## 只启动前端
	cd $(FRONTEND) && pnpm dev

# ── 测试 ────────────────────────────────────────────────────────────────────
.PHONY: test
test: ## 跑全部快速测试（单元 + 契约 + 集成 + 前端单测）
	$(MAKE) test-unit
	$(MAKE) test-contract
	$(MAKE) test-integration
	$(MAKE) test-frontend

.PHONY: test-unit
test-unit: ## 后端单元测试
	cd $(BACKEND) && $(UV) run pytest -m unit -q

.PHONY: test-contract
test-contract: ## 后端契约测试（respx 拦网络，不需要 Key 与数据库）
	cd $(BACKEND) && $(UV) run pytest -m contract -q

.PHONY: test-integration
test-integration: ## 后端集成测试（需要测试库）
	cd $(BACKEND) && $(UV) run pytest -m integration -q

.PHONY: test-frontend
test-frontend: ## 前端单元测试
	cd $(FRONTEND) && pnpm test --run

.PHONY: test-frontend-cov
test-frontend-cov: ## 前端测试 + 覆盖率闸门（阈值见 frontend/vitest.config.ts）
	cd $(FRONTEND) && pnpm test:coverage

.PHONY: test-cov
test-cov: ## 带覆盖率的测试（低于 COV_MIN 直接非零退出，默认 93%）
	cd $(BACKEND) && $(UV) run pytest --cov=app --cov-report=term-missing:skip-covered --cov-fail-under=$(COV_MIN) -q

.PHONY: e2e
e2e: ## Playwright 端到端测试（前冒烟：清限流计数器 + 自动拉起前后端）
	@echo "▶ E2E 前置：Postgres 在跑且开发库已建库（make seed）；缺库时'至少 2 套方案'根本排不出来"
	@psql -h $(PG_HOST) -p $(PG_PORT) -U $(PG_USER) -d $(DB_NAME) -c "DELETE FROM rate_limit_counters" >/dev/null 2>&1 \
		|| echo "  ⚠︎ 限流计数器没清掉（继续跑，但可能被 429 挡住——那说明库/连接串不对）"
	cd $(FRONTEND) && pnpm exec playwright test

# ── 代码质量 ────────────────────────────────────────────────────────────────
.PHONY: lint
lint: ## 代码风格检查
	cd $(BACKEND) && $(UV) run ruff check .
	cd $(FRONTEND) && pnpm lint

.PHONY: format
format: ## 自动格式化
	cd $(BACKEND) && $(UV) run ruff format .
	cd $(FRONTEND) && pnpm format

.PHONY: typecheck
typecheck: ## 类型检查（后端含 app / scripts / tests，前端 tsc）
	cd $(BACKEND) && $(UV) run mypy app scripts tests
	cd $(FRONTEND) && pnpm typecheck

.PHONY: todo
todo: ## 扫描 TODO/FIXME/console.error 等遗留问题
	@echo "── TODO / FIXME ──"
	@grep -rnE "(TODO|FIXME|XXX|HACK)" --include="*.py" --include="*.ts" --include="*.tsx" $(BACKEND)/app $(FRONTEND)/app $(FRONTEND)/components 2>/dev/null || echo "  none"
	@echo "── console.error / console.log（前端应清理）──"
	@grep -rnE "console\.(error|log|warn)" --include="*.ts" --include="*.tsx" $(FRONTEND)/app $(FRONTEND)/components $(FRONTEND)/lib 2>/dev/null || echo "  none"
	@echo "── 未完成标记 ──"
	@grep -rnE "(raise NotImplementedError|pass  # stub)" --include="*.py" $(BACKEND)/app 2>/dev/null || echo "  none"

.PHONY: security
security: ## 安全扫描（密钥泄露 + domain 层纯净性）
	@echo "── gitleaks / 密钥模式扫描 ──"
	@grep -rnE "(sk-[A-Za-z0-9]{16,}|api[_-]?key\s*=\s*[\"'][^\"']{16,})" --include="*.py" --include="*.ts" --include="*.tsx" --include="*.json" $(BACKEND)/app $(FRONTEND)/app $(FRONTEND)/lib 2>/dev/null | grep -v "example" || echo "  clean"
	@echo "── domain 层纯净性（AST 扫描：不得 import httpx/sqlalchemy/fastapi）──"
	cd $(BACKEND) && $(UV) run pytest tests/unit/test_domain_purity.py -q

.PHONY: check
check: lint typecheck test-cov test-frontend-cov ## 提交前必跑：lint + 类型 + 前后端覆盖率闸门 + 测试

# ── 报告 ────────────────────────────────────────────────────────────────────
.PHONY: health
health: ## 检查后端健康状态与降级模式
	@curl -s -m 5 http://127.0.0.1:8000/api/v1/health | python3 -m json.tool || echo "❌ 后端未启动（make dev-backend）"

.PHONY: cost
cost: ## 输出成本报告
	cd $(BACKEND) && $(SCRIPT_ENV) $(UV) run python scripts/cost_report.py

.PHONY: report
report: validate ## 生成知识库数据质量报告
	@echo "报告见 docs/DATA_REPORT.md"

.PHONY: clean
clean: ## 清理缓存与构建产物
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache $(BACKEND)/.coverage
	rm -rf $(FRONTEND)/.next $(FRONTEND)/node_modules/.cache
	@echo "cleaned"
