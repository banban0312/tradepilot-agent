"""M5-06 运行配置版本、快照绑定和审计权限验收。"""

from __future__ import annotations

import os
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models import AgentTask, ConfigSnapshot, RuntimeConfigSecret
from app.runtime_config import config_for_snapshot
from app.tasks.service import create_task

APP_ORIGIN = os.getenv("APP_ORIGIN") or "http://api:8000"
API_URL = os.getenv("TRADEPILOT_TEST_API_URL") or "http://api:8000"
ADMIN_DEPARTMENT = "00000000-0000-4000-8000-000000000001"


def _require_compose() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_034.ps1 在隔离 PostgreSQL Compose 环境执行")


def _config_payload(expected_version: int, *, model_name: str) -> dict[str, object]:
    return {
        "expected_version": expected_version,
        "model_provider": "deepseek",
        "model_name": model_name,
        "model_base_url": None,
        "model_timeout_seconds": 60,
        "model_max_retries": 2,
        "model_max_rounds": 8,
        "model_max_tool_calls": 20,
        "max_concurrency": 3,
        "ocr_provider": "local_tesseract",
        "mcp_url": "http://mcp:8001/mcp",
        "business_timezone": "Asia/Hong_Kong",
        "trace_retention_days": 30,
        "artifact_retention_days": 30,
        "approval_mode": "human_required",
    }


async def _client(login_name: str) -> httpx.AsyncClient:
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    client = httpx.AsyncClient(base_url=API_URL, timeout=20)
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": APP_ORIGIN},
        json={"login": login_name, "password": password},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Origin": APP_ORIGIN, "X-CSRF-Token": response.json()["data"]["csrf_token"]}
    )
    return client


@pytest.mark.asyncio
async def test_runtime_config_versions_are_bound_and_audit_is_admin_only() -> None:
    _require_compose()
    admin = await _client("demo-admin")
    operator = await _client("demo-operator-north")
    task_id: UUID | None = None
    try:
        assert (await operator.get("/api/v1/admin/config")).status_code == 403
        assert (await operator.get("/api/v1/admin/audit")).status_code == 403
        async with async_session_factory() as session, session.begin():
            baseline_task = await create_task(
                session,
                owner_id=UUID("00000000-0000-4000-8000-000000000100"),
                department_id=UUID(ADMIN_DEPARTMENT),
                task_type="SALES",
            )
            baseline_snapshot_id = baseline_task.config_snapshot_id
        assert baseline_snapshot_id is not None
        async with async_session_factory() as session:
            baseline = await session.get(ConfigSnapshot, baseline_snapshot_id)
            assert baseline is not None and baseline.version == 1
            assert baseline.created_by is None
        initial = await admin.get("/api/v1/admin/config")
        assert initial.status_code == 200, initial.text
        assert initial.json()["data"]["version"] == 1
        payload = _config_payload(
            initial.json()["data"]["version"], model_name="issue-34-v1"
        )
        test_key = "sk-test-issue34-credential-never-return"
        payload["model_api_key"] = test_key
        response = await admin.put("/api/v1/admin/config", json=payload)
        assert response.status_code == 200, response.text
        first = response.json()["data"]
        assert first["version"] == 2
        assert first["content"]["model_name"] == "issue-34-v1"
        assert test_key not in response.text
        current = await admin.get("/api/v1/admin/config")
        assert current.json()["data"]["model_api_key_configured"] is True
        async with async_session_factory() as session:
            secret = await session.scalar(
                select(RuntimeConfigSecret).where(
                    RuntimeConfigSecret.name == "model_api_key"
                )
            )
            assert secret is not None and secret.ciphertext is not None
            assert test_key not in secret.ciphertext
            assert secret.ciphertext != test_key

        async with async_session_factory() as session, session.begin():
            task = await create_task(
                session,
                owner_id=UUID("00000000-0000-4000-8000-000000000100"),
                department_id=UUID(ADMIN_DEPARTMENT),
                task_type="SALES",
            )
            task_id = task.id
            first_snapshot_id = task.config_snapshot_id

        second_payload = _config_payload(1, model_name="issue-34-v2")
        second_payload["expected_version"] = 2
        second = await admin.put("/api/v1/admin/config", json=second_payload)
        assert second.status_code == 200, second.text
        assert second.json()["data"]["version"] == 3
        async with async_session_factory() as session:
            assert task_id is not None
            old_task = await session.scalar(
                select(AgentTask).where(AgentTask.id == task_id)
            )
            assert (
                old_task is not None
                and old_task.config_snapshot_id == first_snapshot_id
            )
            assert old_task.config_snapshot_id is not None
            old_config = await session.get(ConfigSnapshot, old_task.config_snapshot_id)
            assert (
                old_config is not None
                and old_config.content["model_name"] == "issue-34-v1"
            )
            assert old_config.content["approval_mode"] == "human_required"
            effective = await config_for_snapshot(session, old_task.config_snapshot_id)
            assert effective.model_api_key is not None
            assert effective.model_api_key.get_secret_value() == test_key

        audit = await admin.get(
            "/api/v1/admin/audit", params={"action": "admin.runtime_config.updated"}
        )
        assert audit.status_code == 200, audit.text
        assert len(audit.json()["data"]["items"]) >= 2
        reference = await admin.get(
            "/api/v1/admin/audit",
            params={
                "action": "admin.runtime_config.updated",
                "reference": first["content_hash"],
            },
        )
        assert reference.status_code == 200, reference.text
        assert reference.json()["data"]["items"]
        stale = await admin.put(
            "/api/v1/admin/config",
            json=_config_payload(1, model_name="stale-version"),
        )
        assert stale.status_code == 409, stale.text
        assert (
            await operator.put("/api/v1/admin/config", json=second_payload)
        ).status_code == 403
    finally:
        await admin.aclose()
        await operator.aclose()
