"""从数据库会话构造可信身份，并集中执行 CSRF 校验。"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import AuthContext
from app.database import get_db_session
from app.models import AuthSession, Department, User, UserDepartmentScope, UserRole

SESSION_COOKIE = "tradepilot_session"
CSRF_HEADER = "X-CSRF-Token"


def session_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token_for_session(token: str) -> str:
    """由高熵会话令牌派生独立 CSRF 值，避免在数据库存储第二份秘密。"""
    digest = hmac.digest(token.encode("utf-8"), b"tradepilot:csrf:v1", "sha256")
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def get_current_identity(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> AuthContext:
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_REQUIRED", "message": "请先登录"},
        )

    record = await session.get(AuthSession, session_digest(token))
    now = datetime.now(UTC)
    if record is None or record.revoked_at is not None:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_REQUIRED", "message": "登录会话无效"},
        )
    if record.expires_at <= now:
        raise HTTPException(
            status_code=401,
            detail={"code": "SESSION_EXPIRED", "message": "登录会话已过期，请重新登录"},
        )

    user = await session.get(User, record.user_id)
    if user is None or not user.active:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_REQUIRED", "message": "登录会话无效"},
        )

    roles = frozenset(
        (
            await session.scalars(
                select(UserRole.role).where(UserRole.user_id == user.id)
            )
        ).all()
    )
    department_ids = frozenset(
        (
            await session.scalars(
                select(UserDepartmentScope.department_id)
                .join(Department, Department.id == UserDepartmentScope.department_id)
                .where(
                    UserDepartmentScope.user_id == user.id,
                    Department.active.is_(True),
                )
            )
        ).all()
    )
    if not department_ids:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCESS_DENIED", "message": "账号没有可用的部门范围"},
        )

    request.state.auth_session_id_hash = record.id_hash
    request.state.auth_session_token = token
    return AuthContext(
        user_id=user.id,
        login=user.login,
        roles=roles,
        department_ids=department_ids,
    )


async def require_csrf(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
) -> AuthContext:
    # 依赖顺序确保会话先通过数据库验证。
    token = getattr(request.state, "auth_session_token", "")
    supplied = request.headers.get(CSRF_HEADER, "")
    expected = csrf_token_for_session(token)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=403,
            detail={"code": "CSRF_INVALID", "message": "CSRF 令牌无效或缺失"},
        )
    return context


def serialize_identity(
    context: AuthContext, csrf_token: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "user_id": str(context.user_id),
        "login": context.login,
        "roles": sorted(context.roles),
        "department_ids": sorted(str(value) for value in context.department_ids),
    }
    if csrf_token is not None:
        payload["csrf_token"] = csrf_token
    return payload


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")
