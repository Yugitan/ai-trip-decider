"""开发设置面板的出入口 Schema（**不是用户面向的功能**，见 `services/dev_config.py`）。

刻意做成"结构固定 + 值动态"：环境变量的清单由后端白名单决定，
前端只是把 ``GET /dev/config`` 的结果渲染出来 ——
这样"面板上能改什么"永远只有一个事实来源（后端），不会两边漂移。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnvFieldOut(BaseModel):
    """一个可编辑的环境变量（Secret 的 ``value`` 是打码后的）。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    group: str
    kind: str
    hint: str = ""
    choices: list[str] = Field(default_factory=list)
    is_set: bool = False
    value: str = ""


class ConfigFileOut(BaseModel):
    """一份可编辑的 YAML 配置（内容原样返回，前端用等宽字体展示）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    content: str


class DevConfigOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: list[EnvFieldOut]
    config_files: list[ConfigFileOut]
    #: 当前**真正生效**的配置快照（providers / 阈值 / 降级模式 / 版本号）
    effective: dict[str, Any]
    #: 面板不被生产环境暴露的说明，前端直接展示，避免"以为用户也能看到"
    notice: str


class EnvUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: dict[str, str] = Field(default_factory=dict)


class EnvUpdateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changed: list[str]
    #: 改了这些键之后**必须重启**才完整的项（当前为空；保留字段是为了不假装"一切都热生效"）
    restart_required: list[str] = Field(default_factory=list)


class ConfigUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class ConfigUpdateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    bytes: int
    effective: dict[str, Any]
