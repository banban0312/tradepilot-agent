"""验证登录会话、CSRF、角色边界及可信部门范围。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth.context import (
    BUSINESS_APPROVER,
    BUSINESS_OPERATOR,
    SYSTEM_ADMIN,
    AuthContext,
    require_business_access,
    require_department_scope,
    require_role,
)
from app.auth.dependencies import SESSION_COOKIE, session_digest
from app.auth.passwords import hash_password
from app.database import async_session_factory
from app.models import AuthSession, User

PUBLIC_ORIGIN = "http://localhost:5173"


def _demo_password() -> str:
    if os.environ.get("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("此集成测试只能在隔离 Compose 验证环境中运行")
    password = os.environ.get("DEMO_PASSWORD", "")
    if os.environ.get("DEMO_MODE") != "true" or len(password) < 12:
        raise RuntimeError(
            "隔离验证环境必须显式设置 DEMO_MODE 和强度合格的 DEMO_PASSWORD"
        )
    return password


async def _login(
    client: httpx.AsyncClient, login: str, password: str
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login",
        headers={"Origin": PUBLIC_ORIGIN},
        json={"login": login, "password": password},
    )


@pytest.mark.asyncio
async def test_login_me_logout_and_session_token_hash_are_real_database_backed() -> (
    None
):
    password = _demo_password()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
        login = await _login(client, "demo-operator-north", password)
        assert login.status_code == 200
        payload = login.json()["data"]
        assert payload["login"] == "demo-operator-north"
        assert payload["roles"] == [BUSINESS_OPERATOR]
        assert payload["department_ids"] == ["00000000-0000-4000-8000-000000000001"]
        assert len(payload["csrf_token"]) >= 40
        assert login.headers["x-request-id"]

        cookie = client.cookies.get(SESSION_COOKIE)
        assert cookie and cookie not in login.text
        set_cookie = login.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=lax" in set_cookie
        assert "max-age=43200" in set_cookie

        async with async_session_factory() as session:
            record = await session.get(AuthSession, session_digest(cookie))
            assert record is not None
            assert record.id_hash == session_digest(cookie)
            assert record.id_hash != cookie
            assert record.revoked_at is None
            assert record.expires_at > datetime.now(UTC)

        current = await client.get("/api/v1/auth/me")
        assert current.status_code == 200
        assert current.json()["data"]["csrf_token"] == payload["csrf_token"]

        bad_csrf = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": PUBLIC_ORIGIN,
                "X-CSRF-Token": "invalid",
            },
        )
        assert bad_csrf.status_code == 403
        assert bad_csrf.json()["error"]["code"] == "CSRF_INVALID"
        assert (await client.get("/api/v1/auth/me")).status_code == 200

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": PUBLIC_ORIGIN,
                "X-CSRF-Token": payload["csrf_token"],
            },
        )
        assert logout.status_code == 200
        assert logout.json()["data"]["logged_out"] is True
        assert (await client.get("/api/v1/auth/me")).status_code == 401

        async with async_session_factory() as session:
            record = await session.get(AuthSession, session_digest(cookie))
            assert record is not None and record.revoked_at is not None


@pytest.mark.asyncio
async def test_invalid_credentials_expiry_and_inactive_users_are_rejected() -> None:
    password = _demo_password()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
        wrong = await _login(client, "demo-operator-north", password + "wrong")
        unknown = await _login(client, "missing-demo-user", password)
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"]

        login = await _login(client, "demo-operator-north", password)
        assert login.status_code == 200
        token = client.cookies.get(SESSION_COOKIE)
        assert token
        async with async_session_factory() as session:
            record = await session.get(AuthSession, session_digest(token))
            assert record is not None
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        expired = await client.get("/api/v1/auth/me")
        assert expired.status_code == 401
        assert expired.json()["error"]["code"] == "SESSION_EXPIRED"

        client.cookies.clear()
        login = await _login(client, "demo-operator-south", password)
        assert login.status_code == 200
        async with async_session_factory() as session:
            user = await session.scalar(
                select(User).where(User.login == "demo-operator-south")
            )
            assert user is not None
            user.active = False
            await session.commit()
        try:
            inactive = await client.get("/api/v1/auth/me")
            assert inactive.status_code == 401
        finally:
            async with async_session_factory() as session:
                user = await session.scalar(
                    select(User).where(User.login == "demo-operator-south")
                )
                assert user is not None
                user.active = True
                await session.commit()


@pytest.mark.asyncio
async def test_cross_origin_writes_and_weak_demo_password_are_rejected() -> None:
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
        response = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://attacker.example"},
            json={"login": "demo-operator-north", "password": _demo_password()},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ORIGIN_INVALID"

    with pytest.raises(ValueError, match="12"):
        hash_password("short")


@pytest.mark.asyncio
async def test_versioned_auth_path_is_preserved_by_frontend_proxy() -> None:
    async with httpx.AsyncClient(base_url="http://web:5173", timeout=10) as client:
        response = await client.get(
            "/api/v1/auth/me", headers={"Host": "localhost:5173"}
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_department_and_role_policy_uses_server_resolved_context() -> None:
    north = UUID("00000000-0000-4000-8000-000000000001")
    south = UUID("00000000-0000-4000-8000-000000000002")
    operator = AuthContext(
        user_id=UUID("00000000-0000-4000-8000-000000000100"),
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({north}),
    )
    administrator = AuthContext(
        user_id=UUID("00000000-0000-4000-8000-000000000103"),
        login="demo-admin",
        roles=frozenset({SYSTEM_ADMIN}),
        department_ids=frozenset({north, south}),
    )

    require_department_scope(operator, north)
    require_role(operator, BUSINESS_OPERATOR)
    require_business_access(operator, north)
    with pytest.raises(HTTPException) as forbidden_department:
        require_department_scope(operator, south)
    assert forbidden_department.value.status_code == 404
    with pytest.raises(HTTPException) as forbidden_role:
        require_role(operator, BUSINESS_APPROVER)
    assert forbidden_role.value.status_code == 403
    with pytest.raises(HTTPException) as admin_not_approver:
        require_role(administrator, BUSINESS_APPROVER)
    assert admin_not_approver.value.status_code == 403
    with pytest.raises(HTTPException) as admin_business_read:
        require_business_access(administrator, north)
    assert admin_business_read.value.status_code == 404


def test_default_seed_has_no_demo_users_and_enabled_seed_uses_reserved_accounts() -> (
    None
):
    from app.seed import seed_rows

    default_tables = {table.name for table, _ in seed_rows()}
    explicit_demo_tables = {table.name for table, _ in seed_rows("encoded-hash")}
    assert "users" not in default_tables
    assert "users" in explicit_demo_tables
    assert {"user_roles", "user_department_scopes"} <= explicit_demo_tables
