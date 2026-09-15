"""环境变量模板的漂移检查（``.env.example`` ↔ ``Settings`` ↔ 开发面板白名单）。

为什么需要这个文件：
    模板是使用者唯一会看的配置清单（README / RUNNING 都把它当权威引用），
    但它和代码之间**没有任何约束**，于是会往两个方向漂移：

    - **加了字段忘了写模板** ⇒ 使用者根本不知道有这个开关（`OPENAI_API_KEY`
      这类"保留档位"就是这么漏掉的）；
    - **模板里的键名拼错 / 改了名** ⇒ 值永远读不到，而填它的人以为已经生效 ——
      正是本项目一路在抓的"配置说一套、实际跑一套"。

    所以这里把三份名单摆在一起对：**模板里的活动键 / `Settings` 的字段 /
    开发面板的可编辑键**。没有断言的地方一定会漂移。
"""

from __future__ import annotations

import re

import pytest

from app.core.config import Settings
from app.core.paths import project_root
from app.services.dev_config import ENV_FIELDS

pytestmark = pytest.mark.unit

#: 模板里「真的会被读」的键：行首就是键名（``KEY=...``）。
_ACTIVE_KEY = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.M)
#: 模板里「只是提一句」的键：注释掉的示例（``# KEY=...``）。
_COMMENTED_KEY = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=", re.M)


def _template_text() -> str:
    return (project_root() / ".env.example").read_text(encoding="utf-8")


def _active_keys() -> set[str]:
    return set(_ACTIVE_KEY.findall(_template_text()))


def _documented_keys() -> set[str]:
    """活动键 + 注释示例键：两者都算「模板里写了」。"""
    text = _template_text()
    return set(_ACTIVE_KEY.findall(text)) | set(_COMMENTED_KEY.findall(text))


def test_settings_fields_are_all_documented() -> None:
    missing = sorted({name.upper() for name in Settings.model_fields} - _documented_keys())
    assert not missing, (
        f"这些 Settings 字段在 .env.example 里一个字都没写：{missing}。"
        "模板是使用者唯一会看的配置清单，加了字段就要同步（哪怕只是一行注释掉的示例）。"
    )


def test_template_has_no_active_key_that_nobody_reads() -> None:
    unknown = sorted(_active_keys() - {name.upper() for name in Settings.model_fields})
    assert not unknown, (
        f".env.example 里这些键是**活的**（`KEY=`），但 Settings 没有对应字段：{unknown}。"
        "多半是拼错或改名了 —— 值会被静默忽略，而填它的人以为配置已生效。"
        "要么补上字段，要么把它改成注释掉的示例。"
    )


def test_frontend_variables_are_never_active_in_the_root_template() -> None:
    """根模板里不能有活的 ``NEXT_PUBLIC_*``。

    Next.js **只读 ``frontend/`` 下的 ``.env*``**，写在这里的值前端进程读不到：
    一个"看起来配好了"的值比没有更糟（同源 cookie 那次就是这么踩的）。
    前端变量写在 `frontend/.env.example`，根模板只负责指路。
    """
    active_next_public = sorted(key for key in _active_keys() if key.startswith("NEXT_PUBLIC_"))
    assert not active_next_public, (
        f"这些前端变量在根 .env.example 里是活的：{active_next_public}，"
        "但 Next 只读 frontend/.env*，它们永远不会生效 —— 移到 frontend/.env.example。"
    )

    assert "frontend/.env.example" in _template_text(), (
        "根模板必须明写「前端变量看 frontend/.env.example」，否则使用者只会在根目录找。"
    )


def test_dev_panel_whitelist_is_documented() -> None:
    """面板能改的键，模板里必须都有 —— 否则面板会写进一个模板里不存在的键。"""
    missing = sorted({field.key for field in ENV_FIELDS} - _documented_keys())
    assert not missing, f"开发面板可编辑、但 .env.example 里没有的键：{missing}"
