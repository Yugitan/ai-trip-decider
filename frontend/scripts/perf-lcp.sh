#!/usr/bin/env bash
#
# 前端首屏指标（LCP）的采集编排（PRD §23.6：分享页 LCP ≤ 2.5s、首页 TTI 那一行）。
#
# 做什么：
#   1. 复用**已经健康**的后端/前端（本地 `make dev` 起着时不再起第二份）；
#   2. 没有就自己起（后端 uvicorn、前端 `next start`，日志写进 .perf/）；
#   3. 造一条**真实**的分享行程拿 slug（分享页的 LCP 才是 PRD 的门槛）；
#   4. 用 web-vitals.mjs 量首页与分享页的 LCP，写一份 JSON 给 benchmark.py 读。
#
# ★ 为什么是一个脚本而不是 Makefile 里的几行 ★
# 它要在**同一个 shell** 里起后台进程、等就绪、最后 trap 收尾；而 Makefile 的每条配方
# 各自一个 shell —— 后台进程与 trap 会被拆到不同进程里，收不了尾（端口被占住，
# 下一次运行就成了"复用"，量到的其实是上一份构建）。
#
# ★ 五个刻意的选择 ★
# 1. **只复用健康的服务，不重启**（与 playwright.config.ts 的 reuseExistingServer 同一套约定）：
#    端口冲突比"测不到"更难排查。
# 2. **只收自己起的进程**（记 pid + `pkill -P` 连子进程一起收），不 pkill 全机的 node/uvicorn。
# 3. **造不出分享行程时不编造**：只测首页，缺的那项由 benchmark.py 在报告里如实写"未测"。
# 4. 浏览器用 Playwright 自带的 chromium（仓库已有），不为一行指标再下一个浏览器。
# 5. 变量紧跟全角标点时必须写 `${VAR}`：`$PERF_PORT）` 会被 bash 把那个全角括号的
#    第一个字节读进变量名，报出来是一句看不懂的 `PERF_PORT<乱码>: unbound variable`
#    （实测踩过，见 TASKS.md 里 M7 那一条）。
#
# 用法（一般由 `make perf-lcp` 调用）：
#   bash frontend/scripts/perf-lcp.sh [--port 3100] [--backend-port 8000] [--runs 3] [--slug abc123]
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"

PERF_BASE="${PERF_BASE:-}"
PERF_API="${PERF_API:-}"
PERF_PORT="${PERF_PORT:-3100}"
PERF_BACKEND_PORT="${PERF_BACKEND_PORT:-8000}"
PERF_RUNS="${PERF_RUNS:-3}"
PERF_DIR="${PERF_DIR:-$REPO_ROOT/.perf}"
PERF_OUT="${PERF_OUT:-$PERF_DIR/web-vitals.json}"
PERF_SLUG="${PERF_SLUG:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --base) PERF_BASE="$2"; shift 2 ;;
    --api) PERF_API="$2"; shift 2 ;;
    --port) PERF_PORT="$2"; shift 2 ;;
    --backend-port) PERF_BACKEND_PORT="$2"; shift 2 ;;
    --runs) PERF_RUNS="$2"; shift 2 ;;
    --out) PERF_OUT="$2"; shift 2 ;;
    --slug) PERF_SLUG="$2"; shift 2 ;;
    -h | --help) sed -n '2,30p' "$0"; exit 0 ;;
    *)
      echo "未知参数：${1}（-h 看用法）" >&2
      exit 2
      ;;
  esac
done
PERF_BASE="${PERF_BASE:-http://127.0.0.1:$PERF_PORT}"
PERF_API="${PERF_API:-http://127.0.0.1:$PERF_BACKEND_PORT}"
mkdir -p "$PERF_DIR"

# ── 只收自己起的进程 ────────────────────────────────────────────────────────
started=()
cleanup() {
  local pid
  for pid in ${started[@]+"${started[@]}"}; do
    pkill -P "$pid" 2>/dev/null || true # uv / pnpm 都会再起一层子进程
    kill "$pid" 2>/dev/null || true
  done
  started=()
}
trap cleanup EXIT INT TERM

healthy() { curl -fsS -o /dev/null -m 3 "$1" 2>/dev/null; }

wait_for() { # <url> <名字> <最多等多少秒>
  local url="$1" name="$2" limit="${3:-60}" i
  for ((i = 0; i < limit * 2; i++)); do
    healthy "$url" && return 0
    sleep 0.5
  done
  echo "❌ 等了 ${limit}s，${name} 还没就绪：${url}（日志见 ${PERF_DIR}/）" >&2
  return 1
}

create_share_slug() {
  # 造一条真实的分享行程。失败一律 return 1（而不是返回一个编造的 slug）。
  local jar="$PERF_DIR/cookies.txt" plan share trip slug
  local payload='{"city":"guangzhou","days":1,"people":2,"preferences":["food"],"pace":"relaxed","budget":{"amount":400,"scope":"per_person"},"free_text":""}'
  plan="$(curl -fsS -m 120 -c "$jar" -b "$jar" -X POST "$PERF_API/api/v1/trips:plan?sync=true" \
    -H 'Content-Type: application/json' -d "$payload" 2>>"$PERF_DIR/backend.log")" || return 1
  trip="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["trip_id"])' 2>/dev/null)" || return 1
  [ -n "$trip" ] || return 1
  share="$(curl -fsS -m 30 -b "$jar" -X POST "$PERF_API/api/v1/trips/$trip/share" \
    -H 'Content-Type: application/json' -d '{"public":true}' 2>>"$PERF_DIR/backend.log")" || return 1
  slug="$(printf '%s' "$share" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["slug"])' 2>/dev/null)" || return 1
  [ -n "$slug" ] || return 1
  printf '%s' "$slug"
}

# ── 后端 ────────────────────────────────────────────────────────────────────
if healthy "$PERF_API/api/v1/health"; then
  echo "• 复用已在运行的后端：${PERF_API}"
else
  echo "• 启动后端（uvicorn :${PERF_BACKEND_PORT}）→ ${PERF_DIR}/backend.log"
  (
    cd "$REPO_ROOT/backend" && exec uv run uvicorn app.main:app --host 127.0.0.1 --port "$PERF_BACKEND_PORT"
  ) >>"$PERF_DIR/backend.log" 2>&1 &
  started+=("$!")
  wait_for "$PERF_API/api/v1/health" "后端" 90 || exit 1
fi

# ── 前端（必须是生产构建：`next dev` 的首屏数字没有意义）────────────────────
if healthy "$PERF_BASE/"; then
  echo "• 复用已在运行的前端：${PERF_BASE}"
else
  echo "• 启动前端（next start :${PERF_PORT}）→ ${PERF_DIR}/frontend.log"
  (
    cd "$FRONTEND_DIR" && exec pnpm exec next start -p "$PERF_PORT"
  ) >>"$PERF_DIR/frontend.log" 2>&1 &
  started+=("$!")
  wait_for "$PERF_BASE/" "前端" 90 || exit 1
fi

# ── 分享页需要一个真的 slug ─────────────────────────────────────────────────
if [ -z "$PERF_SLUG" ]; then
  echo "• 造一条真实的分享行程（拿 slug）"
  if PERF_SLUG="$(create_share_slug)" && [ -n "$PERF_SLUG" ]; then
    echo "  slug=${PERF_SLUG}"
  else
    PERF_SLUG=""
    echo "  ⚠︎ 造不出来（后端限流 / 知识库为空 / 接口变更？）—— 本次只量首页，" >&2
    echo "    报告里「分享页 LCP」会如实写未测，不会拿首页的数字冒充。" >&2
  fi
fi

# ── 采集 ────────────────────────────────────────────────────────────────────
echo "• 采集 LCP：${PERF_BASE}（runs=${PERF_RUNS}，4G + CPU 4x + 移动端 viewport）"
if ! (cd "$FRONTEND_DIR" && exec node scripts/web-vitals.mjs \
  --base "$PERF_BASE" --slug "$PERF_SLUG" --out "$PERF_OUT" --runs "$PERF_RUNS"); then
  echo "❌ LCP 采集失败（见上面的输出；Playwright 的 chromium 未装时先跑 pnpm exec playwright install chromium）" >&2
  exit 1
fi

echo "✅ LCP 已写入：${PERF_OUT}"
