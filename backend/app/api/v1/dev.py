"""开发设置面板的后端（**不是用户功能**，路由只在 ``ENV=development`` 注册）。

它存在的唯一理由是省掉"改 .env → 重启后端 → 再试"的循环。
三道门（缺一不可）：

1. **路由级**：整条路由只在开发环境注册 —— 生产上这些路径**根本不存在**，
   而不是"存在但返回 403"（后者等于告诉外界"这里有个后台"）。
2. **Token 级**：配了 ``ADMIN_TOKEN`` 时必须带 ``X-Admin-Token``，用
   ``hmac.compare_digest`` 比较（防时序侧信道，与 session 那边同一套做法）。
3. **来源级**：**没**配 ``ADMIN_TOKEN`` 时只接受本机来源 ——
   本机开发者不必为了改个 Key 先造一个 Token，但"任何人从公网都能改 Key"也不接受。

刻意不做的事：没有登录页、没有用户/权限模型、没有审计表。
它不是产品的一部分，把它做厚只会让"用户功能"与"开发工具"的边界糊掉。
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.schemas.common import ok_envelope
from app.schemas.dev import (
    ConfigFileOut,
    ConfigUpdateIn,
    ConfigUpdateOut,
    DevConfigOut,
    EnvFieldOut,
    EnvUpdateIn,
    EnvUpdateOut,
)
from app.services import dev_config

__all__ = ["require_dev_access", "router"]

log = get_logger("api.dev")

router = APIRouter(tags=["dev"], prefix="/dev")

#: 未配 ADMIN_TOKEN 时允许的来源。`testclient` 是 FastAPI TestClient 的默认 host ——
#: 少了它，集成测试要么连不上，要么得为了测试造一个 Token（那是把测试需求塞进产品）。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

_NOTICE = (
    "这是开发期的配置面板，不是用户功能：生产环境不会注册这些接口（访问即 404）。"
)


async def require_dev_access(request: Request) -> None:
    """开发面板的访问控制（三道门的后两道）。"""
    settings = get_settings()
    if settings.env != "development":
        raise AppError(
            ErrorCode.NOT_FOUND,
            "请求的资源不存在。",
            context={"path": request.url.path},
        )

    if settings.admin_token:
        provided = request.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(provided, settings.admin_token):
            raise AppError(
                ErrorCode.FORBIDDEN,
                "后台 Token 不正确",
                hint="在页面顶部填入 .env 里的 ADMIN_TOKEN。",
            )
        return

    host = request.client.host if request.client is not None else None
    if host not in _LOCAL_HOSTS:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "未设置 ADMIN_TOKEN 时只允许本机访问配置面板",
            hint="在 .env 里设置 ADMIN_TOKEN，或从本机（127.0.0.1）打开。",
            context={"host": host or ""},
        )


@router.get("/config")
async def read_dev_config(_: None = Depends(require_dev_access)) -> Any:
    """面板首屏：可编辑的环境变量 + 可编辑的 YAML + 当前生效配置。"""
    payload = DevConfigOut(
        env=[EnvFieldOut(**field) for field in dev_config.env_payload()],
        config_files=[
            ConfigFileOut(name=name, content=dev_config.read_config_file(name))
            for name in dev_config.config_file_names()
        ],
        effective=dev_config.effective_snapshot(),
        notice=_NOTICE,
    )
    return ok_envelope(payload.model_dump(mode="json"))


@router.put("/env")
async def update_dev_env(
    body: EnvUpdateIn, _: None = Depends(require_dev_access)
) -> Any:
    """更新白名单内的环境变量（Secret 只写不读；失败自动回滚）。"""
    changed = dev_config.update_env(body.updates)
    payload = EnvUpdateOut(changed=changed)
    return ok_envelope(payload.model_dump(mode="json"))


@router.put("/config/{name}")
async def update_dev_config(
    name: str, body: ConfigUpdateIn, _: None = Depends(require_dev_access)
) -> Any:
    """更新一份 YAML 配置：**先校验后生效**，不通过就把原内容写回去。"""
    dev_config.write_config_file(name, body.content)
    payload = ConfigUpdateOut(
        name=name,
        bytes=len(body.content.encode("utf-8")),
        effective=dev_config.effective_snapshot(),
    )
    return ok_envelope(payload.model_dump(mode="json"))
