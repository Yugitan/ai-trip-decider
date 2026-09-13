"""游客会话（签名 cookie）单元测试（PRD FR-10 AC-10.3）。

守的核心只有一条：**cookie 被改过就等于没有**。
游客行程按 session 隔离，如果签名能被绕过，任何人都能改别人的行程 ——
所以伪造、缺签名、非法 uuid、换了 secret 的签名，都必须解不出 session。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Response
from starlette.requests import Request

from app.services.session import (
    SESSION_COOKIE,
    decode_session_cookie,
    encode_session_cookie,
    get_session_id,
    set_session_cookie,
    sign_session,
)

pytestmark = pytest.mark.unit

SECRET = "test-secret"


def _request(cookie_value: str | None) -> Request:
    headers = [] if cookie_value is None else [(b"cookie", f"{SESSION_COOKIE}={cookie_value}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_sign_and_decode_round_trip() -> None:
    session_id = uuid.uuid4()
    signed = sign_session(str(session_id), secret=SECRET)
    assert decode_session_cookie(signed, secret=SECRET) == session_id


def test_cookie_format_has_uuid_and_signature() -> None:
    session_id = uuid.uuid4()
    signed = sign_session(str(session_id), secret=SECRET)
    session_text, _, signature = signed.rpartition(".")
    assert session_text == str(session_id)
    assert signature, "签名不能为空，否则任何人都能伪造"


def test_tampered_signature_is_rejected() -> None:
    signed = sign_session(str(uuid.uuid4()), secret=SECRET)
    text, _, signature = signed.rpartition(".")
    forged = f"{text}.{'A' * len(signature)}"
    assert decode_session_cookie(forged, secret=SECRET) is None


def test_swapped_uuid_with_original_signature_is_rejected() -> None:
    """把别人的 uuid 配上自己的签名 —— 这是最直接的越权尝试。"""
    other = str(uuid.uuid4())
    signed = sign_session(str(uuid.uuid4()), secret=SECRET)
    _, _, signature = signed.rpartition(".")
    assert decode_session_cookie(f"{other}.{signature}", secret=SECRET) is None


def test_empty_and_malformed_values_are_rejected() -> None:
    assert decode_session_cookie(None, secret=SECRET) is None
    assert decode_session_cookie("", secret=SECRET) is None
    assert decode_session_cookie("no-dot-here", secret=SECRET) is None
    assert decode_session_cookie(".onlysignature", secret=SECRET) is None
    assert decode_session_cookie("not-a-uuid.abc", secret=SECRET) is None


def test_different_secret_produces_different_signature() -> None:
    session_text = str(uuid.uuid4())
    assert sign_session(session_text, secret="a") != sign_session(session_text, secret="b")
    assert decode_session_cookie(sign_session(session_text, secret="a"), secret="b") is None


def test_get_session_id_reuses_valid_cookie() -> None:
    session_id = uuid.uuid4()
    cookie = signed_with_settings(session_id)
    assert get_session_id(_request(cookie)) == session_id


def test_get_session_id_issues_new_id_for_forged_cookie() -> None:
    signed = signed_with_settings(uuid.uuid4())
    text, _, signature = signed.rpartition(".")
    forged = f"{text}.{'B' * len(signature)}"
    fresh = get_session_id(_request(forged))
    assert fresh != uuid.UUID(text), "伪造的 cookie 必须被当作没有，而不是沿用它的 uuid"


def test_get_session_id_issues_new_id_without_cookie() -> None:
    assert isinstance(get_session_id(_request(None)), uuid.UUID)


def test_set_session_cookie_flags() -> None:
    response = Response()
    session_id = uuid.uuid4()
    set_session_cookie(response, session_id)
    header = response.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE}=" in header
    assert "httponly" in header.lower(), "JS 不该能读到会话 cookie"
    assert "samesite=lax" in header.lower()
    assert "max-age" in header.lower()


def signed_with_settings(session_id: uuid.UUID) -> str:
    """用**当前配置里的** secret 签名（``get_session_id`` 读的是配置）。"""
    return encode_session_cookie(session_id)
