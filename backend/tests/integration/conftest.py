"""集成测试夹具：真实数据库 + 真实 ASGI 应用。

★ 关键设计：整个集成测试套件跑在**测试库**上，而不是开发库 ★
    早期版本里 `client` 夹具用的是 Settings 的默认 DATABASE_URL（开发库），
    于是"知识库集成测试"在一个空库上全部 skip —— 测试通过率很好看，但什么都没验证。
    现在在导入应用模块之前就把 DATABASE_URL 指向测试库，并在会话开始时把知识库
    灌进去（复用真实的建库脚本，幂等）。这样断言的才是真实数据。

数据准备顺序：
    1. 迁移测试库到 head
    2. 若测试库还没有知识库（places < 200），跑一次真实建库流程
    3. 缺少原始数据时给出可执行的修复指令（而不是静默跳过）
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from alembic import command
from app.core.paths import backend_dir

# ── 第一步：在任何应用模块读取配置之前，把 DATABASE_URL 指向测试库 ─────────────


def _resolve_test_database_url() -> str:
    """从 .env 推导测试库 URL（此时 Settings 还没被改写过）。"""
    from app.core.config import get_settings

    settings = get_settings()
    if settings.test_database_url:
        return settings.test_database_url
    head, _, _ = settings.database_url.rpartition("/")
    return f"{head}/tripdecider_test"


TEST_DATABASE_URL = _resolve_test_database_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# ── 第一步之二：集成测试**不碰真实 LLM** ──────────────────────────────────
# 开发机的 .env 里可能配了 DEEPSEEK_API_KEY。不管它的话，凡是带 free_text 的规划
# 用例都会真的去调模型：慢、要花钱，而且结果会随模型版本漂移 ——
# 测试就不再是可重复的。这里把 Provider 压成 NullLlmProvider，
# 规划路径随之降级到规则引擎（这正是“无 Key 也能跑”那条硬要求的日常回归）。
# 需要真实调用的用例（tests/integration/test_llm_live.py）自己直接构造 Provider，
# 不受这一行影响。
if os.environ.get("TRIPDECIDER_TEST_LIVE_LLM") != "1":
    os.environ["LLM_PROVIDER"] = "disabled"

# ── 第一步之三：集成测试**不碰真实搜索 Provider** ────────────────────────────
# 与上一条同一个坑：开发机的 shell 里可能 `export TAVILY_API_KEY=tvly-...`
# （**已导出的环境变量会盖住 .env**，见 RUNNING.md §8.8），而 `SEARCH_PROVIDER=auto`
# 一看见 Key 就选 tavily。于是自从 L8 接进规划链（PRD §13.4）之后，任何命中
# 触发条件的规划用例都会真的发起一轮**付费**搜索：断言随"网络上今天写了什么"漂移，
# 每次本地跑都在花钱，而且它失败时会看起来像算法改动 —— 实际是环境变量在起作用。
# 需要真联网的用例（tests/integration/test_search_live.py）自己直接构造 Provider，
# 不受这一行影响（与 test_llm_live.py 同一口径）。
if os.environ.get("TRIPDECIDER_TEST_LIVE_SEARCH") != "1":
    os.environ["SEARCH_PROVIDER"] = "seed_only"

from app.core.config import clear_config_cache  # noqa: E402

clear_config_cache()  # 让后续 get_settings() 读到测试库


def test_database_url() -> str:
    """测试库连接串（供需要用原生引擎的用例复用）。"""
    return TEST_DATABASE_URL


# ── 第二步：迁移 + 建库 ─────────────────────────────────────────────────────


def _run_alembic_upgrade() -> None:
    cfg = AlembicConfig(str(Path(backend_dir()) / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(backend_dir()) / "alembic"))
    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        pytest.fail(
            f"无法连接或迁移测试库 {TEST_DATABASE_URL}：{exc}\n"
            "请先执行：\n"
            "  make db-create\n"
            "  make migrate-test\n"
            "（本机 Postgres 未启动时：brew services start postgresql@16）"
        )
    finally:
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous


async def _place_count() -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            try:
                return int((await conn.execute(text("SELECT count(*) FROM places"))).scalar_one())
            except Exception:
                return -1  # 表还不存在
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepared_test_db() -> Iterator[None]:
    """迁移测试库；若知识库为空则灌入（复用真实建库脚本，保证测的是真实数据）。"""
    _run_alembic_upgrade()

    from app.core.paths import raw_data_dir

    if not list(raw_data_dir().glob("osm_guangzhou_*.json")):
        pytest.fail(
            "缺少原始 OSM 数据，无法在真实知识库上做集成测试。\n"
            "请先运行：make fetch-osm && make seed\n"
            "（这里刻意不 skip：跳过会让集成测试变成空跑，看起来通过其实什么都没验证。）"
        )

    async def _ensure_knowledge_base() -> None:
        """在同一事件循环内完成"检查 → 建库 → 计算关系图 → 释放引擎"。

        为什么必须放在一个 loop 里：SQLAlchemy 的异步引擎会把连接绑定到创建它的
        事件循环。早期实现在这里用 `asyncio.run()` 建库、随后 TestClient 又在自己的
        循环里复用同一个缓存的引擎，于是 /health 的数据库探针抛出
        "Event loop is closed"。修复方式是建库后立刻释放引擎缓存。
        """
        from app.db.session import dispose_engines

        # ★ 每次都重建，而不是"地点少于 200 才建" ★
        # 早期实现只在库为空时建库，于是数据规则改动后，集成测试会继续跑在
        # **过期数据**上并通过 —— 比如"矩形 bbox 切进邻市"这个 bug 修完之后，
        # 测试库仍留着 2000 条深圳/东莞地点，测试照样全绿。
        # 建库约 12 秒，用这点时间换取"测的一定是当前数据"是划算的。
        need_seed = True
        if need_seed:
            from scripts.seed_guangzhou import run as seed_run

            print("\n[集成测试] 测试库知识库为空，正在建库（复用 scripts/seed_guangzhou.py）…")
            try:
                exit_code = await seed_run(dry_run=False, force=True, write_report=False)
            except SystemExit as exc:  # 建库脚本用 SystemExit 报告前置条件缺失
                await dispose_engines()
                pytest.fail(f"建库失败，本轮集成测试中止：{exc}")
            if exit_code != 0:
                await dispose_engines()
                pytest.fail("建库脚本返回非零退出码，集成测试无法在可信数据上运行")

        # 关系图：离线模式足够（1 秒），确保相关断言不会因为"没算过"而被跳过
        from scripts.compute_relations import build_relations

        await build_relations(use_osrm=False, max_places=400, neighbors=8)

        # ★ 关键：释放绑定到本事件循环的引擎，避免 TestClient 在自己的循环里复用到已关闭的连接
        await dispose_engines()

    asyncio.run(_ensure_knowledge_base())

    clear_config_cache()
    yield


@pytest.fixture(scope="session")
def client(_prepared_test_db: None) -> Iterator[TestClient]:
    """真实 ASGI 应用客户端（触发 lifespan：配置 fail-fast + 日志初始化）。"""
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def cost_row_factory() -> Iterator[Callable[[str], int]]:
    """往**今天**的 `cost_logs` 里插一笔钱（已提交），返回行 id；用例结束自动删掉。

    ★ 为什么必须提交 ★
    全局日成本是**跨会话**的账：计的是整个站点今天花了多少。
    写在未提交的事务里，请求自己的连接根本看不见，测试就成了自欺。
    代价是要自己收拾 —— 所以这里把「插进去」和「删干净」绑成一个夹具，
    用例不管怎么结束（包括断言失败）都不会留下痕迹。
    """
    from decimal import Decimal

    from sqlalchemy import delete, insert
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.models import CostLog

    created: list[int] = []

    def create(amount_cny: str) -> int:
        row_id: int = 0

        async def run() -> None:
            nonlocal row_id
            engine = create_async_engine(TEST_DATABASE_URL)
            try:
                async with engine.begin() as conn:
                    row_id = int(
                        (
                            await conn.execute(
                                insert(CostLog)
                                .values(
                                    category="llm",
                                    provider="deepseek",
                                    operation="plan_narrative",
                                    units=1,
                                    unit_price=Decimal(amount_cny),
                                    amount_cny=Decimal(amount_cny),
                                    cache_hit=False,
                                    pricing_calibrated=True,
                                )
                                .returning(CostLog.id)
                            )
                        ).scalar_one()
                    )
            finally:
                await engine.dispose()

        asyncio.run(run())
        created.append(row_id)
        return row_id

    try:
        yield create
    finally:
        # 没插过东西就不必连库（大多数用例用不到这个夹具）
        if created:

            async def cleanup() -> None:
                engine = create_async_engine(TEST_DATABASE_URL)
                try:
                    async with engine.begin() as conn:
                        await conn.execute(delete(CostLog).where(CostLog.id.in_(created)))
                finally:
                    await engine.dispose()

            asyncio.run(cleanup())


@pytest.fixture
async def db_session() -> AsyncIterator[object]:
    """直连测试库的异步会话，用于断言 schema 与数据质量。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()
