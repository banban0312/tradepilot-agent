"""M3-04 工作台任务快照、双向草稿保存与合同额度显示验收。"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.service import save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import AgentTask, ContractBalance, Draft, TaskSession, TaskWait

_NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
_NORTH_DEPT = UUID("00000000-0000-4000-8000-000000000001")
_NORTH_CONTRACT = UUID("00000000-0000-4000-8000-000000000050")
_SOUTH_CONTRACT = UUID("00000000-0000-4000-8000-000000000053")
_API_URL = os.environ.get("TRADEPILOT_TEST_API_URL", "http://api:8000")
_ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:5173")


def _require_isolated_compose() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_018.ps1 在隔离 PostgreSQL Compose 环境执行")


def _candidate(
    *, contract_id: UUID, quantity: Decimal = Decimal(200)
) -> CandidateOrder:
    return CandidateOrder(
        order_type="SALES",
        contract_id=contract_id,
        customer_id=UUID("00000000-0000-4000-8000-000000000010"),
        project_id=UUID("00000000-0000-4000-8000-000000000020"),
        currency="CNY",
        delivery_date=date(2026, 11, 1),
        address="北京市朝阳区示例工地",
        contact="张先生",
        items=(
            CandidateOrderItem(
                material_id=UUID("00000000-0000-4000-8000-000000000030"),
                raw_name="螺纹钢",
                specification="HRB400 12mm",
                quantity=quantity,
                unit="吨",
                unit_price=Decimal(3500),
            ),
        ),
    )


async def _create_task_snapshot(*, contract_id: UUID) -> tuple[UUID, int, str, str]:
    task_id, session_id, draft_id = uuid4(), uuid4(), uuid4()
    checkpoint_id, interrupt_id = f"issue018-cp-{uuid4()}", f"issue018-int-{uuid4()}"
    actor = AuthContext(
        user_id=_NORTH_USER,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({_NORTH_DEPT}),
    )
    async with async_session_factory() as session, session.begin():
        session.add(
            TaskSession(id=session_id, owner_id=_NORTH_USER, title="M3-04 工作台验收")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=_NORTH_USER,
                department_id=_NORTH_DEPT,
                task_type="SALES",
                state="WAITING_INPUT",
                current_step="await_input",
                checkpoint_ref=checkpoint_id,
            )
        )
        draft = Draft(
            id=draft_id, task_id=task_id, current_version=0, lock_state="EDITABLE"
        )
        session.add(draft)
        await session.flush()
        saved = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=0,
            candidate=_candidate(contract_id=contract_id),
            actor=actor,
        )
        session.add(
            TaskWait(
                id=uuid4(),
                task_id=task_id,
                checkpoint_ref=checkpoint_id,
                interrupt_id=interrupt_id,
                kind="INPUT",
                draft_version=saved.version,
                payload={
                    "kind": "input",
                    "missing_fields": ["contact"],
                    "reason": "请补充联系人",
                },
                active=True,
            )
        )
    return task_id, saved.version, checkpoint_id, interrupt_id


async def _operator_client() -> tuple[httpx.AsyncClient, dict[str, object]]:
    _require_isolated_compose()
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    client = httpx.AsyncClient(base_url=_API_URL, timeout=20)
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": _ORIGIN},
        json={"login": "demo-operator-north", "password": password},
    )
    assert response.status_code == 200, response.text
    identity = response.json()["data"]
    client.headers.update(
        {
            "Origin": _ORIGIN,
            "X-CSRF-Token": identity["csrf_token"],
        }
    )
    return client, identity


@pytest.mark.asyncio
async def test_task_snapshot_exposes_real_department_scoped_quota_preview() -> None:
    task_id, version, _, _ = await _create_task_snapshot(contract_id=_NORTH_CONTRACT)
    client, _ = await _operator_client()
    try:
        response = await client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["state"] == "WAITING_INPUT"
        assert data["available_actions"] == ["INPUT", "EDIT_DRAFT"]
        assert data["draft"]["version"] == version
        assert data["draft"]["candidate"]["items"][0]["quantity"] == "200.000000"
        # 200 × 3500 元为 700000 元；北区演示合同额度为 150000 元。
        assert data["quota_preview"] == {
            "contract_amount": "150000.00",
            "occupied_amount": "0.00",
            "remaining_amount": "150000.00",
            "requested_amount": "700000.00",
            "excess_amount": "550000.00",
            "balance_available": True,
        }
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_quota_preview_does_not_disclose_cross_department_contract() -> None:
    task_id, _, _, _ = await _create_task_snapshot(contract_id=_SOUTH_CONTRACT)
    client, _ = await _operator_client()
    try:
        response = await client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["quota_preview"] is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_quota_preview_marks_missing_balance_unavailable() -> None:
    task_id, _, _, _ = await _create_task_snapshot(contract_id=_NORTH_CONTRACT)
    async with async_session_factory() as session, session.begin():
        balance = await session.get(ContractBalance, _NORTH_CONTRACT)
        assert balance is not None
        original_values = (
            balance.occupied_amount,
            balance.occupied_order_count,
            balance.version,
        )
        await session.delete(balance)

    client, _ = await _operator_client()
    try:
        response = await client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["quota_preview"] == {
            "contract_amount": "150000.00",
            "occupied_amount": None,
            "remaining_amount": None,
            "requested_amount": "700000.00",
            "excess_amount": None,
            "balance_available": False,
        }
    finally:
        await client.aclose()
        async with async_session_factory() as session, session.begin():
            session.add(
                ContractBalance(
                    contract_id=_NORTH_CONTRACT,
                    occupied_amount=original_values[0],
                    occupied_order_count=original_values[1],
                    version=original_values[2],
                )
            )


@pytest.mark.asyncio
async def test_editor_patch_advances_version_and_recalculates_quota() -> None:
    task_id, version, checkpoint_id, interrupt_id = await _create_task_snapshot(
        contract_id=_NORTH_CONTRACT
    )
    client, _ = await _operator_client()
    key = f"issue018-edit-{uuid4()}"
    try:
        response = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={"Idempotency-Key": key},
            json={
                "expected_version": version,
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "patch": {
                    "items": [
                        {
                            "material_id": "00000000-0000-4000-8000-000000000030",
                            "raw_name": "螺纹钢",
                            "specification": "HRB400 12mm",
                            "quantity": "2",
                            "unit": "吨",
                            "unit_price": "3500",
                        }
                    ]
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["version"] == version + 1
        assert result["state"] == "QUEUED"

        snapshot = await client.get(f"/api/v1/tasks/{task_id}")
        assert snapshot.status_code == 200, snapshot.text
        data = snapshot.json()["data"]
        assert data["draft"]["version"] == version + 1
        assert data["draft"]["total_amount"] == "7000.00"
        assert data["quota_preview"]["requested_amount"] == "7000.00"
        assert data["quota_preview"]["excess_amount"] == "0.00"
    finally:
        await client.aclose()
