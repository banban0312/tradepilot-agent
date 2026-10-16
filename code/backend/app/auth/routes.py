"""同源 Cookie 会话登录接口。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth.context import AuthContext
from app.auth.dependencies import (
    SESSION_COOKIE,
    csrf_token_for_session,
    get_current_identity,
    request_id,
    require_csrf,
    serialize_identity,
    session_digest,
)
from app.auth.passwords import verify_password
from app.config import settings
from app.database import get_db_session
from app.models import AuthSession, Department, User, UserDepartmentScope, UserRole

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    login_name = body.login.strip().casefold()
    user = await session.scalar(
        select(User).where(func.lower(User.login) == login_name)
    )
    encoded = user.password_hash if user is not None else None
    password_matches = await run_in_threadpool(verify_password, body.password, encoded)

    if user is None or not user.active or not password_matches:
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_CREDENTIALS", "message": "账号或密码错误"},
        )

    department_ids = list(
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

    roles = list(
        (
            await session.scalars(
                select(UserRole.role).where(UserRole.user_id == user.id)
            )
        ).all()
    )
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    session.add(
        AuthSession(
            id_hash=session_digest(token),
            user_id=user.id,
            expires_at=expires_at,
        )
    )
    await session.commit()

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=int(timedelta(hours=settings.session_ttl_hours).total_seconds()),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return {
        "data": {
            "user_id": str(user.id),
            "login": user.login,
            "roles": sorted(roles),
            "department_ids": sorted(str(value) for value in department_ids),
            "csrf_token": csrf_token_for_session(token),
        },
        "request_id": request_id(request),
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    del context  # require_csrf 已完成会话、用户和 CSRF 校验。
    session_hash = request.state.auth_session_id_hash
    record = await session.get(AuthSession, session_hash)
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await session.commit()
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return {"data": {"logged_out": True}, "request_id": request_id(request)}


@router.get("/me")
async def me(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
) -> dict[str, object]:
    session_token = getattr(request.state, "auth_session_token", "")
    return {
        "data": serialize_identity(context, csrf_token_for_session(session_token)),
        "request_id": request_id(request),
    }
