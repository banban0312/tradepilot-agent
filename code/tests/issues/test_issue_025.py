"""M4-04 真实物料候选、人工决定、版本证据及审批失效验收。"""

from __future__ import annotations

import os
from datetime import date
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.service import load_draft, save_candidate
from app.drafts.types import (
    CandidateOrder,
    CandidateOrderItem,
    FieldEvidenceInput,
    FieldSource,
)
from app.models import (
    AgentTask,
    AuditEvent,
    Draft,
    FieldEvidence,
    Order,
    TaskApproval,
    TaskCommand,
    TaskSession,
    TaskWait,
)

NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
NORTH_DEPT = UUID("00000000-0000-4000-8000-000000000001")
NORTH_CONTRACT = UUID("00000000-0000-4000-8000-000000000050")
NORTH_CUSTOMER = UUID("00000000-0000-4000-8000-000000000010")
NORTH_PROJECT = UUID("00000000-0000-4000-8000-000000000020")
MATERIAL_12MM = UUID("00000000-0000-4000-8000-000000000030")
MATERIAL_16MM = UUID("00000000-0000-4000-8000-000000000031")
API_URL = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:5173")


def _require_isolated_compose() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_025.ps1 在隔离 PostgreSQL Compose 环境执行")


async def _create_waiting_task(
    *, specification: str | None = None
) -> tuple[UUID, UUID, str, str, str]:
    task_id, session_id, draft_id = uuid4(), uuid4(), uuid4()
    checkpoint_id, interrupt_id = f"issue025-cp-{uuid4()}", f"issue025-int-{uuid4()}"
    actor = AuthContext(
        user_id=NORTH_USER,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({NORTH_DEPT}),
    )
    async with async_session_factory() as session, session.begin():
        session.add(
            TaskSession(id=session_id, owner_id=NORTH_USER, title="物料候选验收")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=NORTH_USER,
                department_id=NORTH_DEPT,
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
            candidate=CandidateOrder(
                order_type="SALES",
                contract_id=NORTH_CONTRACT,
                customer_id=NORTH_CUSTOMER,
                project_id=NORTH_PROJECT,
                currency="CNY",
                delivery_date=date(2026, 11, 1),
                address="华北演示工地",
                contact="张先生",
                items=(
                    CandidateOrderItem(
                        material_id=None,
                        raw_name="螺纹钢",
                        specification=specification,
                        quantity="20",
                        unit="吨",
                        unit_price="3500",
                    ),
                ),
                evidence=(
                    FieldEvidenceInput(
                        field_path="items[0].raw_name",
                        source_type=FieldSource.FILE_PAGE,
                        artifact_id=uuid4(),
                        locator={"type": "page", "page": 2},
                        raw_value="螺纹钢",
                    ),
                ),
            ),
            actor=actor,
        )
        # 让内容变更在同一测试里验证已有批准会被草稿服务端失效。
        approval_command_id = uuid4()
        session.add(
            TaskCommand(
                id=approval_command_id,
                task_id=task_id,
                actor_id=NORTH_USER,
                kind="REVIEW",
                payload={"decision": "APPROVE"},
                expected_version=saved.version,
                request_id=f"issue025-approval-{uuid4()}",
                payload_hash="a" * 64,
                state="APPLIED",
            )
        )
        await session.flush()
        session.add(
            TaskApproval(
                id=uuid4(),
                task_id=task_id,
                command_id=approval_command_id,
                draft_id=draft_id,
                actor_id=NORTH_USER,
                department_id=NORTH_DEPT,
                version=saved.version,
                content_hash=saved.content_hash,
                checkpoint_ref=checkpoint_id,
                interrupt_id=interrupt_id,
                decision="APPROVE",
            )
        )
        session.add(
            TaskWait(
                id=uuid4(),
                task_id=task_id,
                checkpoint_ref=checkpoint_id,
                interrupt_id=interrupt_id,
                kind="INPUT",
                draft_version=saved.version,
                payload={"kind": "input", "missing_fields": []},
                active=True,
            )
        )
    return task_id, draft_id, checkpoint_id, interrupt_id, saved.content_hash


async def _operator_client(login: str = "demo-operator-north") -> httpx.AsyncClient:
    _require_isolated_compose()
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    client = httpx.AsyncClient(base_url=API_URL, timeout=30)
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": login, "password": password},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Origin": ORIGIN, "X-CSRF-Token": response.json()["data"]["csrf_token"]}
    )
    return client


@pytest.mark.asyncio
async def test_material_match_returns_real_ambiguous_candidates_and_scopes_task() -> (
    None
):
    task_id, _, _, _, _ = await _create_waiting_task()
    client = await _operator_client()
    try:
        async with async_session_factory() as session:
            before_orders = await session.scalar(
                select(func.count()).select_from(Order)
            )
        response = await client.post(
            f"/api/v1/tasks/{task_id}/material-matches",
            json={"expected_version": 1, "item_index": 0},
        )
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        candidates = result["data"]["candidates"]
        assert {row["material_id"] for row in candidates} == {
            str(MATERIAL_12MM),
            str(MATERIAL_16MM),
        }
        assert all(row["requires_confirmation"] for row in candidates)
        assert any(
            issue["code"] == "MATERIAL_CONFIRMATION_REQUIRED"
            for issue in result["issues"]
        )
        assert all(
            row["specification"] in {"HRB400 12mm", "HRB400 16mm"} for row in candidates
        )

        specific_task, _, _, _, _ = await _create_waiting_task(
            specification="HRB400 12mm"
        )
        specific = await client.post(
            f"/api/v1/tasks/{specific_task}/material-matches",
            json={"expected_version": 1, "item_index": 0},
        )
        assert specific.status_code == 200, specific.text
        specific_candidates = specific.json()["data"]["data"]["candidates"]
        assert [row["material_id"] for row in specific_candidates] == [
            str(MATERIAL_12MM)
        ]
        assert not specific_candidates[0]["requires_confirmation"]

        denied_client = await _operator_client("demo-operator-south")
        try:
            denied = await denied_client.post(
                f"/api/v1/tasks/{task_id}/material-matches",
                json={"expected_version": 1, "item_index": 0},
            )
            assert denied.status_code == 404
        finally:
            await denied_client.aclose()
        async with async_session_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(Order))
                == before_orders
            )
            draft = await session.scalar(select(Draft).where(Draft.task_id == task_id))
            assert draft is not None
            snapshot = await load_draft(session, draft.id)
            assert snapshot is not None and snapshot.version == 1
            assert snapshot.candidate is not None
            assert snapshot.candidate.items[0].material_id is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_reject_and_accept_write_audit_evidence_new_version_and_invalidate_approval() -> (
    None
):
    (
        task_id,
        draft_id,
        checkpoint_id,
        interrupt_id,
        original_hash,
    ) = await _create_waiting_task()
    client = await _operator_client()
    try:
        csrf_missing = await client.post(
            f"/api/v1/tasks/{task_id}/material-matches",
            headers={"X-CSRF-Token": ""},
            json={"expected_version": 1, "item_index": 0},
        )
        assert csrf_missing.status_code == 403
        matches = await client.post(
            f"/api/v1/tasks/{task_id}/material-matches",
            json={"expected_version": 1, "item_index": 0},
        )
        assert matches.status_code == 200, matches.text
        rows = matches.json()["data"]["data"]["candidates"]
        rejected = rows[1]
        rejection = await client.post(
            f"/api/v1/tasks/{task_id}/material-decisions",
            json={
                "expected_version": 1,
                "item_index": 0,
                "candidate_material_id": rejected["material_id"],
                "decision": "REJECT",
            },
        )
        assert rejection.status_code == 200, rejection.text
        async with async_session_factory() as session:
            reject_audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.task_id == task_id,
                    AuditEvent.action == "material.candidate.rejected",
                )
            )
            assert reject_audit is not None
            assert reject_audit.actor_id == NORTH_USER
            assert reject_audit.after_ref["candidate_code"] == rejected["code"]
            assert reject_audit.created_at is not None
            unchanged = await load_draft(session, draft_id)
            assert unchanged is not None and unchanged.version == 1
            assert (
                unchanged.candidate is not None
                and unchanged.candidate.items[0].material_id is None
            )

        body = {
            "expected_version": 1,
            "checkpoint_id": checkpoint_id,
            "interrupt_id": interrupt_id,
            "patch": {
                "items": [
                    {
                        "material_id": str(MATERIAL_12MM),
                        "raw_name": "螺纹钢",
                        "specification": "HRB400 12mm",
                        "quantity": "20",
                        "unit": "吨",
                        "unit_price": "3500",
                    }
                ]
            },
        }
        invalid = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={"Idempotency-Key": f"issue025-invalid-{uuid4()}"},
            json={
                **body,
                "patch": {
                    "items": [
                        {
                            **body["patch"]["items"][0],
                            "specification": "HRB400 12mm",
                            "material_id": str(MATERIAL_16MM),
                        }
                    ]
                },
            },
        )
        assert invalid.status_code == 422, f"HTTP {invalid.status_code}: {invalid.text}"
        assert invalid.json()["error"]["code"] == "MATERIAL_CANDIDATE_INVALID"

        accepted = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={"Idempotency-Key": f"issue025-accept-{uuid4()}"},
            json=body,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["data"]["version"] == 2

        async with async_session_factory() as session:
            snapshot = await load_draft(session, draft_id)
            assert snapshot is not None and snapshot.version == 2
            assert snapshot.candidate is not None
            adopted = snapshot.candidate.items[0]
            assert adopted.material_id == MATERIAL_12MM
            assert adopted.raw_name == "螺纹钢"
            assert adopted.specification == "HRB400 12mm"
            approval = await session.scalar(
                select(TaskApproval).where(TaskApproval.task_id == task_id)
            )
            assert approval is not None and approval.invalidated_at is not None
            evidence = await session.scalar(
                select(FieldEvidence).where(
                    FieldEvidence.draft_id == draft_id,
                    FieldEvidence.version == 2,
                    FieldEvidence.field_path == "items[0].material_id",
                )
            )
            assert evidence is not None
            assert evidence.source_type == "USER_EDIT"
            assert evidence.confirmed_by == NORTH_USER
            assert evidence.artifact_id is not None
            assert evidence.locator == {"type": "page", "page": 2}
            assert evidence.raw_value == {
                "raw_name": "螺纹钢",
                "specification": None,
            }
            assert evidence.accepted_value["code"] == "TP-DEMO-RB12"
            assert evidence.accepted_value["specification"] == "HRB400 12mm"
            accepted_audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.task_id == task_id,
                    AuditEvent.action == "material.candidate.accepted",
                )
            )
            assert accepted_audit is not None
            assert accepted_audit.actor_id == NORTH_USER
            assert accepted_audit.after_ref["code"] == "TP-DEMO-RB12"
            assert accepted_audit.created_at is not None
            assert accepted_audit.object_ref["draft_version"] == 2
            assert accepted_audit.input_hash is not None
            assert accepted_audit.input_hash != original_hash
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.task_id == task_id)
                )
                == 0
            )
    finally:
        await client.aclose()
