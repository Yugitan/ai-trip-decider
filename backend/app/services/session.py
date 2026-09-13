"""游客会话：**签名 cookie**，不依赖登录（PRD FR-10 AC-10.3）。

为什么要自己实现而不是引入 ``itsdangerous`` + ``SessionMiddleware``：
    项目只缺一个"把 uuid 放进 cookie 且不能伪造"的能力。为此新增一个依赖、
    再引入一套会**序列化整个 session dict** 的中间件，换来的是更多可变的隐式状态。
    这里用标准库 ``hmac`` 做一个显式的签名串，行为完全可预测、可单测。

格式：``<uuid>.<HMAC-SHA256 的 urlsafe base64（去 padding）>``

诚实性约定：
    - cookie 被篡改 → **当作没有**（签发新的），而不是报错 —— 用户不该因为
      手动改了 cookie 就看到 500。
    - uuid 解析失败同样当作没有。
    - 签名比较必须用 ``hmac.compare_digest``，避免把签名变成可爆破的（时序侧信道）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Final

from fastapi import Request, Response

from app.core.config import get_settings

__all__ = [
    "SESSION_COOKIE",
    "decode_session_cookie",
    "encode_session_cookie",
    "get_session_id",
    "set_session_cookie",
    "sign_session",
]

SESSION_COOKIE: Final = "td_session"

# 游客身份没有账号可找回，所以给足有效期（180 天）。这不是"安全边界"，
# 只是一个稳定标识：清掉 cookie 就等于换了一个新游客（PRD AC-10.4 有明确说明）。
_MAX_AGE_S: Final = 180 * 24 * 3600


def _secret_bytes(secret: str | None = None) -> bytes:
    return (secret or get_settings().session_secret).encode("utf-8")


def _signature(session_text: str, secret: bytes) -> str:
    digest = hmac.new(secret, session_text.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sign_session(session_id: str, *, secret: str | None = None) -> str:
    """把 uuid 字符串签成 ``<uuid>.<sig>``。``secret`` 可注入，便于测试。"""
    return f"{session_id}.{_signature(session_id, _secret_bytes(secret))}"


def decode_session_cookie(raw: str | None, *, secret: str | None = None) -> uuid.UUID | None:
    """解出并验签。任何异常情况（空/无分隔符/非法 uuid/签名不符）都返回 ``None``。"""
    if not raw:
        return None
    session_text, _, signature = raw.rpartition(".")
    if not session_text or not signature:
        return None
    try:
        candidate = uuid.UUID(session_text)
    except ValueError:
        return None
    expected = _signature(session_text, _secret_bytes(secret))
    if not hmac.compare_digest(signature, expected):
        return None
    return candidate


def encode_session_cookie(session_id: uuid.UUID, *, secret: str | None = None) -> str:
    return sign_session(str(session_id), secret=secret)


def set_session_cookie(response: Response, session_id: uuid.UUID) -> None:
    """把 session 写进响应的 Set-Cookie。

    ★ 为什么由端点显式调用，而不是在依赖里写 ★
    FastAPI 只有在"返回模型、由框架构造响应"时才会合并依赖注入的 Response 头；
    一旦端点**直接返回 Response 对象**（我们的 JSONResponse / SSE 就是这样），
    依赖里设的 cookie 会被静默丢掉。依赖与显式写在效果上完全一样，
    但后者不会因为"返回值形态变了"而失效。
    """
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        encode_session_cookie(session_id),
        max_age=_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )


def get_session_id(request: Request) -> uuid.UUID:
    """FastAPI 依赖：读签名 cookie；缺失或伪造则**签发新的**。

    同步函数：只有 HMAC 与字符串处理，没有 IO。
    """
    session_id = decode_session_cookie(request.cookies.get(SESSION_COOKIE))
    return session_id or uuid.uuid4()
