"""架构铁律的**静态守卫**：`app/domain/**` 必须是纯逻辑，零 IO、零框架依赖。

为什么需要它（PRD §23.2 第 95 条，M2 的前置约束）：
    `domain/` 里放的是这个产品真正的护城河 —— 评分、可行性校验、路线组合。
    它们的价值前提是**能被脱离数据库与网络单测**：给定同样的输入必须给出同样的输出，
    不受外部服务是否可用影响。

    一旦有人在 `domain/scoring.py` 里顺手 `import httpx` 去查一次距离，
    这个前提就没了 —— 而症状不会立刻显现：测试仍然通过（因为测试环境网络恰好可用），
    直到线上某个 Provider 挂掉，整个评分链路一起挂。

    所以这条约束必须由**机器**守住，不能靠代码评审时的人眼。

同时这也是"依赖方向"的守卫：domain 是最内层，不允许反向依赖
`app.db` / `app.api` / `app.services` / `app.providers` —— 那是依赖倒置。

注意：本文件同时**测试了扫描器自身**（用合成的代码片段断言它真的能发现违规）。
一个永远返回"没问题"的扫描器比没有扫描器更糟。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.paths import backend_dir

pytestmark = pytest.mark.unit

DOMAIN_DIR = backend_dir() / "app" / "domain"

# 外部框架 / 数据库 / 网络库：domain 一律不得依赖
FORBIDDEN_TOP_LEVEL = (
    "httpx",
    "sqlalchemy",
    "fastapi",
    "starlette",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "alembic",
    "requests",
    "aiohttp",
    "redis",
    "boto3",
)

# 只禁具体子模块的（`urllib.parse` 是纯函数，不该被牵连）
FORBIDDEN_EXACT = (
    "urllib.request",
    "urllib.error",
    "http.client",
    "socket",
    "subprocess",
)

# 依赖方向：domain 不得反向依赖上层
FORBIDDEN_APP_PREFIXES = (
    "app.db",
    "app.api",
    "app.services",
    "app.providers",
    "app.main",
    "app.schemas",
)


def _imported_modules(source: str) -> set[str]:
    """收集一个 Python 源文件里所有被 import 的模块名（含 `from x import y` 的 x）。"""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入（`from .geo import ...`）不参与判断
                continue
            if node.module:
                modules.add(node.module)
    return modules


def _violations(modules: set[str]) -> list[str]:
    """返回被违反的规则描述（空列表 = 干净）。"""
    problems: list[str] = []
    for module in sorted(modules):
        if module.split(".")[0] in FORBIDDEN_TOP_LEVEL:
            problems.append(f"依赖了外部框架/驱动：{module}")
        if module in FORBIDDEN_EXACT:
            problems.append(f"依赖了 IO 模块：{module}")
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_APP_PREFIXES):
            problems.append(f"反向依赖了上层模块：{module}")
    return problems


def _domain_files() -> list[Path]:
    return sorted(DOMAIN_DIR.glob("*.py"))


# ── 扫描器自身的可靠性（"测试的测试"）───────────────────────────────────────


@pytest.mark.parametrize(
    "snippet",
    [
        "import httpx",
        "import sqlalchemy.orm",
        "from fastapi import APIRouter",
        "from sqlalchemy import select",
        "import asyncpg",
        "from urllib.request import urlopen",
        "import subprocess",
        "from app.db.models import Place",
        "from app.api.v1 import catalog",
        "import app.services.plan_service",
    ],
)
def test_scanner_detects_violations(snippet: str) -> None:
    """★ 扫描器必须真的能发现问题 —— 否则整个守卫是空的。"""
    assert _violations(_imported_modules(snippet)), f"扫描器漏掉了违规：{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "import math",
        "from dataclasses import dataclass",
        "from app.core.config import SeedConfig",
        "from app.domain.geo import haversine_m",
        "from app.domain import naming",
        "import urllib.parse",
    ],
)
def test_scanner_allows_legitimate_imports(snippet: str) -> None:
    """★ 不能误报：纯函数库、项目内的下层模块、以及 `urllib.parse` 都是合法的。"""
    assert not _violations(_imported_modules(snippet)), f"扫描器误报了：{snippet}"


def test_relative_imports_are_ignored() -> None:
    """相对导入（`from .geo import ...`）解析不出绝对模块名，不应被当成违规。"""
    assert _imported_modules("from .geo import haversine_m") == set()


# ── 真实代码的守卫 ──────────────────────────────────────────────────────────


def test_domain_directory_is_not_empty() -> None:
    """守卫本身要能发现"扫错目录"——扫了个空目录会永远通过。"""
    files = _domain_files()
    assert files, f"{DOMAIN_DIR} 下没有找到任何 Python 文件，守卫失效"
    assert {path.name for path in files} >= {"categories.py", "geo.py", "naming.py"}


def test_domain_modules_are_pure() -> None:
    """★ 架构铁律：`app/domain/**` 不得 import 任何框架 / 驱动 / IO 模块。

    违反时这里会直接失败，并指出是哪个文件依赖了什么。
    """
    offenders: dict[str, list[str]] = {}
    for path in _domain_files():
        problems = _violations(_imported_modules(path.read_text(encoding="utf-8")))
        if problems:
            offenders[path.name] = problems

    assert not offenders, (
        "domain 层被污染了（它必须能被脱离数据库与网络单测）：\n"
        + "\n".join(f"  {name}: {'; '.join(problems)}" for name, problems in offenders.items())
    )


def test_domain_has_no_module_level_side_effects() -> None:
    """模块顶层只允许 import / 赋值 / 定义，不允许调用函数。

    顶层调用会在 import 时执行 —— 比如 `logging.basicConfig()` 或 `open(...)`，
    这会让"纯逻辑模块"在被导入的瞬间就产生副作用。
    """
    offenders: dict[str, list[str]] = {}
    for path in _domain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node.lineno
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        if calls:
            offenders[path.name] = [f"第 {line} 行" for line in calls]

    assert not offenders, (
        "domain 模块在 import 时就执行了函数调用：\n"
        + "\n".join(f"  {name}: {', '.join(lines)}" for name, lines in offenders.items())
    )
