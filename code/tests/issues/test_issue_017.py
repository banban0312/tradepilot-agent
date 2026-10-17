"""M3-03 未知提交结果回查与安全恢复验收。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
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
    Contract,
    ContractBalance,
    ContractLine,
    ContractLineBalance,
    Draft,
    Order,
    QuotaOccupation,
    TaskApproval,
    TaskCommand,
    TaskJob,
    TaskSession,
    TraceSpan,
)
from app.orders.commit import commit_approved_order
from app.tasks.service import TaskExecutionLease, recover_abandoned_jobs
from app.worker.workflow import DatabaseWorkflowServices

NORTH_OPERATOR = UUID("00000000-0000-4000-8000-000000000100")
NORTH_APPROVER = UUID("00000000-0000-4000-8000-000000000102")
NORTH_DEPARTMENT = UUID("00000000-0000-4000-8000-000000000001")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000010")
PROJECT_ID = UUID("00000000-0000-4000-8000-000000000020")
MATERIAL_ID = UUID("00000000-0000-4000-8000-000000000030")
APP_ORIGIN = os.environ.get("APP_ORIGIN") or "http://api:8000"
API_URL = os.environ.get("TRADEPILOT_TEST_API_URL") or "http://api:8000"


def _require_compose() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_017.ps1 在隔离 PostgreSQL Compose 环境执行")


async def _new_failed_task(*, retryable: bool) -> tuple[UUID, UUID, UUID, UUID]:
    task_id, session_id, command_id, job_id, span_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    code = "MODEL_UNAVAILABLE" if retryable else "RULE_QUOTA_EXCEEDED"
    error = {"code": code, "retryable": retryable}
    now = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        session.add(
            TaskSession(id=session_id, owner_id=NORTH_OPERATOR, title="M3-03 验收")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=NORTH_OPERATOR,
                department_id=NORTH_DEPARTMENT,
                task_type="SALES",
                state="FAILED",
                projection_version=4,
                last_error={
                    **error,
                    "failed_span_id": str(span_id),
                    "failure": {
                        "span_id": str(span_id),
                        "name": "model.function_calling",
                        "kind": "MODEL",
                        "status": "ERROR",
                        "code": code,
                        "retryable": retryable,
                    },
                },
            )
        )
        session.add(
            TaskCommand(
                id=command_id,
                task_id=task_id,
                actor_id=NORTH_OPERATOR,
                kind="START",
                payload={
                    "text": "螺纹钢 2 吨",
                    "input_type": "TEXT",
                    "order_type": "SALES",
                },
                request_id=f"issue017-start-{uuid4()}",
                payload_hash="a" * 64,
                state="REJECTED",
            )
        )
        session.add(
            TaskJob(
                id=job_id,
                task_id=task_id,
                command_id=command_id,
                kind="RUN_TASK",
                state="FAILED",
                attempts=1,
                available_at=now,
            )
        )
        session.add(
            TraceSpan(
                id=span_id,
                trace_id=uuid4(),
                task_id=task_id,
                actor_id=NORTH_OPERATOR,
                kind="MODEL",
                name="model.function_calling",
                execution_mode="LIVE",
                protocol="test.responses",
                attempt=1,
                start_at=now,
                end_at=now,
                duration_ms=1,
                status="ERROR",
                error=error,
            )
        )
    return task_id, command_id, job_id, span_id


async def _operator_client() -> httpx.AsyncClient:
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    client = httpx.AsyncClient(base_url=API_URL, timeout=20)
    login = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": APP_ORIGIN},
        json={"login": "demo-operator-north", "password": password},
    )
    assert login.status_code == 200, login.text
    client.headers.update(
        {
            "Origin": APP_ORIGIN,
            "X-CSRF-Token": login.json()["data"]["csrf_token"],
        }
    )
    return client


@pytest.mark.asyncio
async def test_retry_reuses_original_job_and_is_idempotent() -> None:
    _require_compose()
    task_id, source_command_id, original_job_id, span_id = await _new_failed_task(
        retryable=True
    )
    client = await _operator_client()
    try:
        headers = {"Idempotency-Key": f"issue017-retry-{uuid4()}"}
        body = {
            "failed_span_id": str(span_id),
            "expected_projection_version": 4,
        }
        response = await client.post(
            f"/api/v1/tasks/{task_id}/retry", headers=headers, json=body
        )
        assert response.status_code == 202, response.text
        result = response.json()["data"]
        assert result["state"] == "QUEUED"
        assert result["job_id"] == str(original_job_id)
        assert result["failed_span_id"] == str(span_id)
        assert result["replayed"] is False

        async with async_session_factory() as session:
            task = await session.get(AgentTask, task_id)
            source = await session.get(TaskCommand, source_command_id)
            job = await session.get(TaskJob, original_job_id)
            retry_command = await session.get(TaskCommand, UUID(result["command_id"]))
            assert task is not None and task.state == "QUEUED"
            assert task.projection_version == 5
            assert source is not None and source.state == "READY"
            assert job is not None and job.state == "READY"
            assert job.id == original_job_id and job.attempts == 1
            assert retry_command is not None and retry_command.kind == "RETRY"
            assert retry_command.payload["source_command_id"] == str(source_command_id)
            assert retry_command.payload["failed_span_id"] == str(span_id)

        replay = await client.post(
            f"/api/v1/tasks/{task_id}/retry", headers=headers, json=body
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["data"]["command_id"] == result["command_id"]
        assert replay.json()["data"]["job_id"] == str(original_job_id)
        assert replay.json()["data"]["replayed"] is True

        async with async_session_factory() as session, session.begin():
            task = await session.get(AgentTask, task_id)
            job = await session.get(TaskJob, original_job_id)
            first_retry = await session.get(TaskCommand, UUID(result["command_id"]))
            assert task is not None and job is not None and first_retry is not None
            task.state = "FAILED"
            task.projection_version += 1
            job.state = "FAILED"
            job.worker_id = None
            first_retry.state = "REJECTED"
            second_expected_version = task.projection_version
        second = await client.post(
            f"/api/v1/tasks/{task_id}/retry",
            headers={"Idempotency-Key": f"issue017-retry-again-{uuid4()}"},
            json={
                "failed_span_id": str(span_id),
                "expected_projection_version": second_expected_version,
            },
        )
        assert second.status_code == 202, second.text
        assert second.json()["data"]["job_id"] == str(original_job_id)
        assert second.json()["data"]["command_id"] != result["command_id"]
        old_key_replay = await client.post(
            f"/api/v1/tasks/{task_id}/retry", headers=headers, json=body
        )
        assert old_key_replay.status_code == 202, old_key_replay.text
        assert old_key_replay.json()["data"]["command_id"] == result["command_id"]
        assert old_key_replay.json()["data"]["job_id"] == str(original_job_id)
        assert old_key_replay.json()["data"]["replayed"] is True
        unsafe = await client.post(
            f"/api/v1/tasks/{task_id}/retry",
            headers={"Idempotency-Key": f"issue017-unsafe-{uuid4()}"},
            json={**body, "goto": "commit_order"},
        )
        assert unsafe.status_code == 422
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_business_block_and_stale_projection_cannot_be_retried() -> None:
    _require_compose()
    task_id, _, job_id, span_id = await _new_failed_task(retryable=False)
    client = await _operator_client()
    try:
        body = {
            "failed_span_id": str(span_id),
            "expected_projection_version": 4,
        }
        headers = {"Idempotency-Key": f"issue017-blocked-{uuid4()}"}
        blocked = await client.post(
            f"/api/v1/tasks/{task_id}/retry", headers=headers, json=body
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "TASK_FAILURE_NOT_RETRYABLE"
        stale = await client.post(
            f"/api/v1/tasks/{task_id}/retry",
            headers={"Idempotency-Key": f"issue017-stale-{uuid4()}"},
            json={**body, "expected_projection_version": 3},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
        async with async_session_factory() as session:
            job = await session.get(TaskJob, job_id)
            assert job is not None and job.state == "FAILED"
            retry_count = await session.scalar(
                select(func.count())
                .select_from(TaskCommand)
                .where(TaskCommand.task_id == task_id, TaskCommand.kind == "RETRY")
            )
            assert retry_count == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_failure_projection_ignores_stale_spans_from_previous_attempt() -> None:
    _require_compose()
    task_id, source_command_id, job_id, old_span_id = await _new_failed_task(
        retryable=True
    )
    worker_id = f"issue017-failure-{uuid4()}"
    async with async_session_factory() as session, session.begin():
        job = await session.get(TaskJob, job_id)
        old_span = await session.get(TraceSpan, old_span_id)
        assert job is not None and old_span is not None
        job.state = "RUNNING"
        job.worker_id = worker_id
        old_span.start_at = datetime.now(UTC) - timedelta(minutes=1)
    async with engine.connect() as connection:
        lease = await TaskExecutionLease.acquire(
            connection,
            task_id=task_id,
            job_id=job_id,
            worker_id=worker_id,
        )
        try:
            await lease.finish(
                error={"code": "CHECKPOINT_RECOVERY_FAILED", "retryable": False}
            )
        finally:
            await lease.release()
    async with async_session_factory() as session:
        task = await session.get(AgentTask, task_id)
        source = await session.get(TaskCommand, source_command_id)
        assert task is not None and source is not None
        failure = task.last_error
        assert isinstance(failure, dict)
        assert failure["code"] == "CHECKPOINT_RECOVERY_FAILED"
        assert failure["retryable"] is False
        assert failure["failed_span_id"] != str(old_span_id)
        assert source.state == "REJECTED"
        span = await session.get(TraceSpan, UUID(failure["failed_span_id"]))
        assert span is not None
        assert span.name == "worker.execution"
        assert span.error == {
            "code": "CHECKPOINT_RECOVERY_FAILED",
            "retryable": False,
        }


@pytest.mark.asyncio
async def test_abandoned_worker_marks_running_spans_interrupted() -> None:
    _require_compose()
    task_id, _, job_id, _ = await _new_failed_task(retryable=True)
    span_id = uuid4()
    now = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        task = await session.get(AgentTask, task_id)
        job = await session.get(TaskJob, job_id)
        assert task is not None and job is not None
        task.state = "RUNNING"
        job.state = "RUNNING"
        job.worker_id = "issue017-abandoned"
        job.heartbeat_at = now - timedelta(minutes=10)
        session.add(
            TraceSpan(
                id=span_id,
                trace_id=uuid4(),
                task_id=task_id,
                actor_id=NORTH_OPERATOR,
                kind="MODEL",
                name="model.pending_call",
                execution_mode="LIVE",
                protocol="test.responses",
                attempt=1,
                start_at=now - timedelta(minutes=10),
                status="RUNNING",
            )
        )
    async with engine.begin() as connection:
        recovered = await recover_abandoned_jobs(
            connection, stale_before=now - timedelta(minutes=5)
        )
    assert recovered == 1
    async with async_session_factory() as session:
        task = await session.get(AgentTask, task_id)
        job = await session.get(TaskJob, job_id)
        span = await session.get(TraceSpan, span_id)
        assert task is not None and task.state == "QUEUED"
        assert job is not None and job.state == "READY"
        assert span is not None and span.status == "INTERRUPTED"
        assert span.end_at is not None
        assert span.error == {
            "code": "EXECUTION_INTERRUPTED",
            "retryable": True,
        }


async def _create_approved_order() -> tuple[UUID, UUID, UUID, UUID, UUID]:
    contract_id, line_id = uuid4(), uuid4()
    task_id, draft_id, session_id = uuid4(), uuid4(), uuid4()
    command_id, approval_id, job_id = uuid4(), uuid4(), uuid4()
    checkpoint_id, interrupt_id = f"issue017-cp-{uuid4()}", f"issue017-int-{uuid4()}"
    candidate = CandidateOrder(
        order_type="SALES",
        contract_id=contract_id,
        customer_id=CUSTOMER_ID,
        project_id=PROJECT_ID,
        delivery_date=date(2026, 11, 1),
        address="北京市朝阳区示例工地",
        contact="张先生",
        items=(
            CandidateOrderItem(
                material_id=MATERIAL_ID,
                raw_name="螺纹钢",
                specification="HRB400 12mm",
                quantity=Decimal("2.000000"),
                unit="吨",
                unit_price=Decimal("3500.000000"),
            ),
        ),
    )
    async with async_session_factory() as session, session.begin():
        session.add(
            Contract(
                id=contract_id,
                number=f"ISSUE17-{uuid4().hex[:16]}",
                department_id=NORTH_DEPARTMENT,
                type="SALES",
                customer_id=CUSTOMER_ID,
                project_id=PROJECT_ID,
                currency="CNY",
                total_amount=Decimal("150000.00"),
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
                material_id=MATERIAL_ID,
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
        session.add(
            TaskSession(id=session_id, owner_id=NORTH_OPERATOR, title="M3-03 订单回查")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=NORTH_OPERATOR,
                department_id=NORTH_DEPARTMENT,
                task_type="SALES",
                state="SUBMITTING",
                projection_version=4,
                current_step="commit_order",
                checkpoint_ref=checkpoint_id,
            )
        )
        session.add(
            Draft(
                id=draft_id,
                task_id=task_id,
                current_version=0,
                lock_state="EDITABLE",
            )
        )
        await session.flush()
        saved = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=0,
            candidate=candidate,
            actor=AuthContext(
                user_id=NORTH_OPERATOR,
                login="demo-operator-north",
                roles=frozenset({BUSINESS_OPERATOR}),
                department_ids=frozenset({NORTH_DEPARTMENT}),
            ),
        )
        approval_payload = {
            "decision": "APPROVE",
            "draft_id": str(draft_id),
            "content_hash": saved.content_hash,
            "checkpoint_id": checkpoint_id,
            "interrupt_id": interrupt_id,
        }
        session.add(
            TaskCommand(
                id=command_id,
                task_id=task_id,
                actor_id=NORTH_APPROVER,
                kind="REVIEW",
                payload=approval_payload,
                expected_version=saved.version,
                target_interrupt_id=interrupt_id,
                request_id=f"issue017-approve-{uuid4()}",
                payload_hash="b" * 64,
                state="REJECTED",
            )
        )
        await session.flush()
        session.add(
            TaskApproval(
                id=approval_id,
                task_id=task_id,
                command_id=command_id,
                draft_id=draft_id,
                actor_id=NORTH_APPROVER,
                department_id=NORTH_DEPARTMENT,
                version=saved.version,
                content_hash=saved.content_hash,
                checkpoint_ref=checkpoint_id,
                interrupt_id=interrupt_id,
                decision="APPROVE",
            )
        )
        draft = await session.get(Draft, draft_id)
        assert draft is not None
        draft.lock_state = "SUBMITTING"
    async with async_session_factory() as session, session.begin():
        first = await commit_approved_order(
            session,
            task_id=task_id,
            command_id=command_id,
            approval_id=approval_id,
            config=settings,
        )
    assert first.state == "COMPLETED" and first.order_id is not None
    span_id = uuid4()
    now = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        task = await session.get(AgentTask, task_id)
        command = await session.get(TaskCommand, command_id)
        draft = await session.get(Draft, draft_id)
        assert task is not None and command is not None and draft is not None
        task.state = "FAILED"
        task.projection_version += 1
        task.last_error = {
            "code": "ORDER_COMMIT_RESULT_UNKNOWN",
            "retryable": True,
            "failed_span_id": str(span_id),
        }
        command.state = "REJECTED"
        session.add(
            TaskJob(
                id=job_id,
                task_id=task_id,
                command_id=command_id,
                kind="RUN_TASK",
                state="FAILED",
                attempts=1,
            )
        )
        session.add(
            TraceSpan(
                id=span_id,
                trace_id=uuid4(),
                task_id=task_id,
                actor_id=NORTH_OPERATOR,
                kind="ORDER",
                name="order.commit",
                execution_mode="LIVE",
                protocol="postgresql",
                attempt=1,
                start_at=now,
                end_at=now,
                duration_ms=1,
                status="ERROR",
                error={"code": "ORDER_COMMIT_RESULT_UNKNOWN", "retryable": True},
            )
        )
    return task_id, command_id, approval_id, job_id, span_id


@pytest.mark.asyncio
async def test_committed_order_retry_replays_original_without_occupying_quota_twice() -> (
    None
):
    _require_compose()
    task_id, command_id, approval_id, job_id, span_id = await _create_approved_order()
    client = await _operator_client()
    try:
        response = await client.post(
            f"/api/v1/tasks/{task_id}/retry",
            headers={"Idempotency-Key": f"issue017-order-retry-{uuid4()}"},
            json={
                "failed_span_id": str(span_id),
                "expected_projection_version": 6,
            },
        )
        assert response.status_code == 202, response.text
        assert response.json()["data"]["job_id"] == str(job_id)
        async with async_session_factory() as session:
            task = await session.get(AgentTask, task_id)
            order_before = await session.scalar(
                select(Order).where(Order.task_id == task_id)
            )
            assert order_before is not None
            balance_before = await session.scalar(
                select(ContractBalance).where(
                    ContractBalance.contract_id == order_before.contract_id
                )
            )
            occupation_before = await session.scalar(
                select(QuotaOccupation).where(QuotaOccupation.task_id == task_id)
            )
            assert task is not None and task.state == "QUEUED"
            assert balance_before is not None
            assert occupation_before is not None
            original = (
                order_before.id,
                order_before.number,
                balance_before.occupied_amount,
                occupation_before.amount,
            )
        worker_id = f"issue017-recovery-{uuid4()}"
        async with async_session_factory() as session, session.begin():
            retry_job = await session.get(TaskJob, job_id)
            assert retry_job is not None and retry_job.state == "READY"
            retry_job.state = "RUNNING"
            retry_job.worker_id = worker_id
            retry_job.attempts += 1
        async with engine.connect() as connection:
            lease = await TaskExecutionLease.acquire(
                connection,
                task_id=task_id,
                job_id=job_id,
                worker_id=worker_id,
            )
            try:
                services = DatabaseWorkflowServices.__new__(DatabaseWorkflowServices)
                services.session_factory = async_session_factory
                services.lease = lease
                services.task = SimpleNamespace(id=task_id)
                services.config = settings
                replay = await services.commit_order(
                    {
                        "resume_command": {
                            "id": str(command_id),
                            "approval_id": str(approval_id),
                        }
                    }
                )
                assert replay["state"] == "COMPLETED" and replay["replayed"]
                assert (replay["order_id"], replay["order_number"]) == tuple(
                    str(value) for value in original[:2]
                )
                await lease.project_state("COMPLETED", current_step="completed")
                await lease.finish()
            finally:
                await lease.release()
        async with async_session_factory() as session, session.begin():
            source_command = await session.get(TaskCommand, command_id)
            assert source_command is not None
            source_command.state = "APPLIED"
        async with async_session_factory() as session:
            order_count = await session.scalar(
                select(func.count()).select_from(Order).where(Order.task_id == task_id)
            )
            occupation_count = await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id == task_id)
            )
            order = await session.scalar(select(Order).where(Order.task_id == task_id))
            assert order is not None
            balance = await session.get(ContractBalance, order.contract_id)
            occupation = await session.scalar(
                select(QuotaOccupation).where(QuotaOccupation.task_id == task_id)
            )
            assert order_count == occupation_count == 1
            assert balance is not None and occupation is not None
            assert (
                order.id,
                order.number,
                balance.occupied_amount,
                occupation.amount,
            ) == original
    finally:
        await client.aclose()
