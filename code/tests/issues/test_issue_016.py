"""M3-02 正式订单原子提交、实时额度复核与并发幂等验收。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.config import settings
from app.database import async_session_factory, engine
from app.drafts.service import save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import (
    AgentTask,
    AuditEvent,
    Contract,
    ContractBalance,
    ContractLine,
    ContractLineBalance,
    Draft,
    Order,
    OrderItem,
    QuotaOccupation,
    TaskApproval,
    TaskCommand,
    TaskEvent,
    TaskJob,
    TaskSession,
)
from app.orders.commit import OrderCommitRejected, commit_approved_order
from app.tasks.service import TaskExecutionLease
from app.worker.workflow import DatabaseWorkflowServices

_NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
_NORTH_APPROVER = UUID("00000000-0000-4000-8000-000000000102")
_NORTH_DEPT = UUID("00000000-0000-4000-8000-000000000001")
_ORIGIN = os.environ.get("APP_ORIGIN") or "http://api:8000"
_API_BASE_URL = os.environ.get("TRADEPILOT_TEST_API_URL") or "http://api:8000"
_OWNER = AuthContext(
    user_id=_NORTH_USER,
    login="demo-operator-north",
    roles=frozenset({BUSINESS_OPERATOR}),
    department_ids=frozenset({_NORTH_DEPT}),
)


def _candidate(quantity: Decimal, contract_id: UUID) -> CandidateOrder:
    return CandidateOrder(
        order_type="SALES",
        contract_id=contract_id,
        customer_id=UUID("00000000-0000-4000-8000-000000000010"),
        project_id=UUID("00000000-0000-4000-8000-000000000020"),
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


async def _new_contract(
    *, total_amount: Decimal = Decimal("150000.00")
) -> tuple[UUID, UUID]:
    contract_id, line_id = uuid4(), uuid4()
    async with async_session_factory() as session, session.begin():
        session.add(
            Contract(
                id=contract_id,
                number=f"ISSUE16-{uuid4().hex[:16]}",
                department_id=_NORTH_DEPT,
                type="SALES",
                customer_id=UUID("00000000-0000-4000-8000-000000000010"),
                project_id=UUID("00000000-0000-4000-8000-000000000020"),
                currency="CNY",
                total_amount=total_amount,
                status="ACTIVE",
                valid_from=date(2025, 1, 1),
                valid_to=date(2030, 12, 31),
                version=1,
            )
        )
        session.add(
            ContractLine(
                id=line_id,
                contract_id=contract_id,
                material_id=UUID("00000000-0000-4000-8000-000000000030"),
                unit="吨",
                unit_price=Decimal("3500.000000"),
                max_quantity=Decimal("100.000000"),
            )
        )
        session.add(
            ContractBalance(
                contract_id=contract_id,
                occupied_amount=Decimal("0.00"),
                occupied_order_count=0,
                version=0,
            )
        )
        session.add(
            ContractLineBalance(
                contract_line_id=line_id,
                occupied_quantity=Decimal("0.000000"),
            )
        )
    return contract_id, line_id


async def _prepare(
    contract_id: UUID,
    line_id: UUID,
    *,
    quantity: Decimal = Decimal(2),
    invalidated: bool = False,
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    task_id, draft_id, session_id = uuid4(), uuid4(), uuid4()
    command_id, approval_id = uuid4(), uuid4()
    checkpoint, interrupt = f"issue016-cp-{uuid4()}", f"issue016-int-{uuid4()}"
    async with async_session_factory() as session, session.begin():
        session.add(
            TaskSession(id=session_id, owner_id=_NORTH_USER, title="M3-02 验收")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=_NORTH_USER,
                department_id=_NORTH_DEPT,
                task_type="SALES",
                state="SUBMITTING",
                current_step="commit_order",
                checkpoint_ref=checkpoint,
            )
        )
        session.add(
            Draft(
                id=draft_id, task_id=task_id, current_version=0, lock_state="EDITABLE"
            )
        )
        await session.flush()
        saved = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=0,
            candidate=_candidate(quantity, contract_id),
            actor=_OWNER,
        )
        payload = {
            "decision": "APPROVE",
            "draft_id": str(draft_id),
            "content_hash": saved.content_hash,
            "checkpoint_id": checkpoint,
            "interrupt_id": interrupt,
        }
        command = TaskCommand(
            id=command_id,
            task_id=task_id,
            actor_id=_NORTH_APPROVER,
            kind="REVIEW",
            payload=payload,
            expected_version=saved.version,
            target_interrupt_id=interrupt,
            request_id=f"issue016-{uuid4()}",
            payload_hash=hashlib.sha256(str(payload).encode()).hexdigest(),
        )
        session.add(command)
        await session.flush()
        session.add(
            TaskApproval(
                id=approval_id,
                task_id=task_id,
                command_id=command_id,
                draft_id=draft_id,
                actor_id=_NORTH_APPROVER,
                department_id=_NORTH_DEPT,
                version=saved.version,
                content_hash=saved.content_hash,
                checkpoint_ref=checkpoint,
                interrupt_id=interrupt,
                decision="APPROVE",
                invalidated_at=datetime.now(UTC) if invalidated else None,
            )
        )
        draft = await session.get(Draft, draft_id)
        assert draft is not None
        draft.lock_state = "SUBMITTING"
    return task_id, command_id, approval_id, contract_id, line_id


async def _submit(prepared: tuple[UUID, UUID, UUID, UUID, UUID]):
    task_id, command_id, approval_id, _, _ = prepared
    async with async_session_factory() as session, session.begin():
        return await commit_approved_order(
            session,
            task_id=task_id,
            command_id=command_id,
            approval_id=approval_id,
        )


@pytest.mark.asyncio
async def test_twenty_replays_create_one_order_and_one_quota_occupation() -> None:
    contract_id, line_id = await _new_contract()
    prepared = await _prepare(contract_id, line_id)
    task_id = prepared[0]
    results = await asyncio.gather(*[_submit(prepared) for _ in range(20)])
    assert all(result.state == "COMPLETED" for result in results)
    assert sum(not result.replayed for result in results) == 1
    assert len({result.order_id for result in results}) == 1
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.task_id == task_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrderItem)
                .where(OrderItem.order_id == results[0].order_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id == task_id)
            )
            == 1
        )
        balance = await session.get(ContractBalance, contract_id)
        line_balance = await session.get(ContractLineBalance, line_id)
        draft = await session.scalar(select(Draft).where(Draft.task_id == task_id))
        task = await session.get(AgentTask, task_id)
        audits = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.task_id == task_id, AuditEvent.action == "order.committed"
            )
        )
        events = await session.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(
                TaskEvent.task_id == task_id,
                TaskEvent.event_type == "task.order_committed",
            )
        )
    assert (
        balance is not None
        and balance.occupied_amount == Decimal("7000.00")
        and balance.occupied_order_count == 1
    )
    assert line_balance is not None and line_balance.occupied_quantity == Decimal(
        "2.000000"
    )
    assert draft is not None and draft.lock_state == "SUBMITTED"
    assert task is not None and task.state == "COMPLETED"
    assert audits == 1 and events == 1


@pytest.mark.asyncio
async def test_existing_order_replay_rejects_a_different_approval_snapshot() -> None:
    contract_id, line_id = await _new_contract()
    prepared = await _prepare(contract_id, line_id)
    task_id, command_id = prepared[:2]
    first = await _submit(prepared)
    async with async_session_factory() as session, session.begin():
        command = await session.get(TaskCommand, command_id)
        assert command is not None
        command.payload = {**command.payload, "content_hash": "0" * 64}

    with pytest.raises(OrderCommitRejected, match="不匹配的幂等回放"):
        await _submit(prepared)

    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.task_id == task_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id == task_id)
            )
            == 1
        )
    assert first.state == "COMPLETED"


@pytest.mark.asyncio
async def test_two_hundred_thousand_orders_racing_one_hundred_fifty_thousand_balance_block_one() -> (
    None
):
    contract_id, line_id = await _new_contract()
    first = await _prepare(contract_id, line_id, quantity=Decimal("28.571429"))
    second = await _prepare(contract_id, line_id, quantity=Decimal("28.571429"))
    task_ids = [first[0], second[0]]
    results = await asyncio.gather(_submit(first), _submit(second))
    assert sorted(result.state for result in results) == ["BLOCKED_RULE", "COMPLETED"]
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.task_id.in_(task_ids))
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id.in_(task_ids))
            )
            == 1
        )
        balance = await session.get(ContractBalance, contract_id)
        line_balance = await session.get(ContractLineBalance, line_id)
        blocked_index = next(
            index
            for index, result in enumerate(results)
            if result.state == "BLOCKED_RULE"
        )
        blocked_approval = await session.get(
            TaskApproval, (first, second)[blocked_index][2]
        )
        blocked_draft = await session.scalar(
            select(Draft).where(Draft.task_id == task_ids[blocked_index])
        )
        blocked_audits = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.task_id == task_ids[blocked_index],
                AuditEvent.action == "order.commit_blocked",
            )
        )
    assert balance is not None and Decimal(0) <= balance.occupied_amount <= Decimal(
        150000
    )
    assert line_balance is not None and line_balance.occupied_quantity <= Decimal(100)
    assert blocked_approval is not None and blocked_approval.invalidated_at is not None
    assert blocked_draft is not None and blocked_draft.lock_state == "EDITABLE"
    assert blocked_audits == 1


@pytest.mark.asyncio
async def test_invalidated_approval_cannot_create_order_or_occupy_quota() -> None:
    contract_id, line_id = await _new_contract()
    prepared = await _prepare(contract_id, line_id, invalidated=True)
    task_id = prepared[0]
    with pytest.raises(OrderCommitRejected, match="批准已过期"):
        await _submit(prepared)
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.task_id == task_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id == task_id)
            )
            == 0
        )
        balance = await session.get(ContractBalance, contract_id)
    assert balance is not None and balance.occupied_amount == Decimal("0.00")


@pytest.mark.asyncio
async def test_revoked_approver_cannot_create_order_or_occupy_quota() -> None:
    contract_id, line_id = await _new_contract()
    prepared = await _prepare(contract_id, line_id)
    task_id, command_id, approval_id = prepared[:3]
    # 模拟审批命令签发后，账号被改为只有经办权限的任务所有者。
    async with async_session_factory() as session, session.begin():
        command = await session.get(TaskCommand, command_id)
        approval = await session.get(TaskApproval, approval_id)
        assert command is not None and approval is not None
        command.actor_id = _NORTH_USER
        approval.actor_id = _NORTH_USER

    with pytest.raises(OrderCommitRejected, match="失去当前部门的审批权限"):
        await _submit(prepared)

    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.task_id == task_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id == task_id)
            )
            == 0
        )
        balance = await session.get(ContractBalance, contract_id)
    assert balance is not None and balance.occupied_amount == Decimal("0.00")


@pytest.mark.asyncio
async def test_exact_remaining_contract_balance_is_allowed() -> None:
    contract_id, line_id = await _new_contract(total_amount=Decimal("7000.00"))
    prepared = await _prepare(contract_id, line_id)
    async with async_session_factory() as session, session.begin():
        balance = await session.get(ContractBalance, contract_id)
        assert balance is not None
        balance.occupied_amount = Decimal("0.00")
    result = await _submit(prepared)
    assert result.state == "COMPLETED"
    async with async_session_factory() as session:
        balance = await session.get(ContractBalance, contract_id)
        assert balance is not None and balance.occupied_amount == Decimal("7000.00")


@pytest.mark.asyncio
async def test_worker_service_commits_under_current_epoch_and_task_api_returns_order() -> (
    None
):
    contract_id, line_id = await _new_contract()
    prepared = await _prepare(contract_id, line_id)
    task_id, command_id, approval_id = prepared[:3]
    job_id = uuid4()
    worker_id = f"issue016-{uuid4()}"
    async with async_session_factory() as session, session.begin():
        session.add(
            TaskJob(
                id=job_id,
                task_id=task_id,
                command_id=command_id,
                kind="RUN_TASK",
                state="RUNNING",
                attempts=1,
                worker_id=worker_id,
            )
        )
    async with engine.connect() as connection:
        lease = await TaskExecutionLease.acquire(
            connection, task_id=task_id, job_id=job_id, worker_id=worker_id
        )
        try:
            services = DatabaseWorkflowServices.__new__(DatabaseWorkflowServices)
            services.session_factory = async_session_factory
            services.lease = lease
            services.task = SimpleNamespace(id=task_id)
            services.config = settings
            result = await services.commit_order(
                {
                    "resume_command": {
                        "id": str(command_id),
                        "approval_id": str(approval_id),
                    }
                }
            )
            assert result["state"] == "COMPLETED"
            await lease.finish()
        finally:
            await lease.release()

    password = os.environ.get("DEMO_PASSWORD", "")
    assert os.environ.get("TRADEPILOT_TEST_MODE") == "1" and len(password) >= 12
    async with httpx.AsyncClient(base_url=_API_BASE_URL, timeout=10) as client:
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={"login": "demo-operator-north", "password": password},
        )
        assert login.status_code == 200, login.text
        detail = await client.get(f"/api/v1/tasks/{task_id}")
    assert detail.status_code == 200, detail.text
    order_ref = detail.json()["data"]["order_ref"]
    assert order_ref["order_id"] == result["order_id"]
    assert order_ref["order_number"] == result["order_number"]
    assert order_ref["total_amount"] == "7000.00"
