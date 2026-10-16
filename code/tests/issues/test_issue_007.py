"""通过前端同源代理验证登录页所依赖的真实会话契约。"""

from __future__ import annotations

import os

import httpx
import pytest
from app.auth.context import BUSINESS_OPERATOR
from app.auth.dependencies import SESSION_COOKIE


def _demo_password() -> str:
    if os.environ.get("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("此集成测试只能在隔离 Compose 验证环境中运行")
    password = os.environ.get("DEMO_PASSWORD", "")
    if os.environ.get("DEMO_MODE") != "true" or len(password) < 12:
        raise RuntimeError("隔离验证环境必须显式设置演示模式和有效演示密码")
    return password


def _origin() -> str:
    return os.environ.get("APP_ORIGIN", "http://localhost:5173")


def _frontend_headers() -> dict[str, str]:
    return {"Host": _origin().removeprefix("http://")}


@pytest.mark.asyncio
async def test_frontend_shell_and_history_routes_are_served_in_simplified_chinese() -> (
    None
):
    async with httpx.AsyncClient(
        base_url="http://web:5173", timeout=10, headers=_frontend_headers()
    ) as client:
        for path in ("/", "/login", "/interface-states"):
            response = await client.get(path)
            assert response.status_code == 200
            assert '<html lang="zh-CN">' in response.text
            assert 'id="app"' in response.text

        login_module = await client.get("/src/views/LoginView.vue")
        shell_module = await client.get("/src/views/AppShell.vue")
        assert (
            login_module.status_code == 200 and "登录 TradePilot" in login_module.text
        )
        assert shell_module.status_code == 200 and "主导航" in shell_module.text
        assert "退出" in shell_module.text


@pytest.mark.asyncio
async def test_login_me_and_csrf_logout_work_through_same_origin_frontend_proxy() -> (
    None
):
    password = _demo_password()
    async with httpx.AsyncClient(
        base_url="http://web:5173", timeout=10, headers=_frontend_headers()
    ) as client:
        denied = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": _origin()},
            json={"login": "demo-operator-north", "password": password + "wrong"},
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "INVALID_CREDENTIALS"
        assert client.cookies.get(SESSION_COOKIE) is None

        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": _origin()},
            json={"login": "demo-operator-north", "password": password},
        )
        assert login.status_code == 200
        identity = login.json()["data"]
        assert identity["login"] == "demo-operator-north"
        assert identity["roles"] == [BUSINESS_OPERATOR]
        assert identity["department_ids"] == ["00000000-0000-4000-8000-000000000001"]
        csrf_token = identity["csrf_token"]
        cookie = client.cookies.get(SESSION_COOKIE)
        assert cookie and cookie not in login.text

        current = await client.get("/api/v1/auth/me")
        assert current.status_code == 200
        assert current.json()["data"]["csrf_token"] == csrf_token

        bad_logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": _origin(), "X-CSRF-Token": "invalid"},
        )
        assert bad_logout.status_code == 403
        assert bad_logout.json()["error"]["code"] == "CSRF_INVALID"
        assert (await client.get("/api/v1/auth/me")).status_code == 200

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": _origin(), "X-CSRF-Token": csrf_token},
        )
        assert logout.status_code == 200
        assert logout.json()["data"]["logged_out"] is True
        assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_login_requests_reject_cross_origin_and_invalid_payloads() -> None:
    password = _demo_password()
    async with httpx.AsyncClient(
        base_url="http://web:5173", timeout=10, headers=_frontend_headers()
    ) as client:
        cross_origin = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://attacker.example"},
            json={"login": "demo-operator-north", "password": password},
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json()["error"]["code"] == "ORIGIN_INVALID"

        extra_field = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": _origin()},
            json={
                "login": "demo-operator-north",
                "password": password,
                "roles": ["system_admin"],
            },
        )
        assert extra_field.status_code == 422
        assert client.cookies.get(SESSION_COOKIE) is None
