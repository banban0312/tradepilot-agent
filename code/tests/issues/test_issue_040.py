"""M6-05：恢复、并发和安全不变量的专项回归。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import runpy
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.database import async_session_factory
from app.drafts.service import save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import (
    AgentTask,
    AuditEvent,
    Contract,
    ContractBalance,
    Draft,
    ImportBatchItem,
    Order,
    QuotaOccupation,
    TaskApproval,
    TaskCommand,
    TaskJob,
    TaskWait,
)
from app.orders.commit import commit_approved_order

_NORTH_OWNER = UUID("00000000-0000-4000-8000-000000000100")
_NORTH_APPROVER = UUID("00000000-0000-4000-8000-000000000102")
_NORTH_DEPARTMENT = UUID("00000000-0000-4000-8000-000000000001")
_CUSTOMER = UUID("00000000-0000-4000-8000-000000000010")
_PROJECT = UUID("00000000-0000-4000-8000-000000000020")
_MATERIAL = UUID("00000000-0000-4000-8000-000000000030")


async def _prepare_batch_child(task_id: UUID, contract_id: UUID, actor):
    checkpoint, interrupt = f"issue040-cp-{uuid4()}", f"issue040-int-{uuid4()}"
    draft_id, command_id, approval_id = uuid4(), uuid4(), uuid4()
    candidate = CandidateOrder(
        order_type="SALES",
        contract_id=contract_id,
        customer_id=_CUSTOMER,
        project_id=_PROJECT,
        delivery_date=date(2026, 11, 1),
        address="北京市朝阳区专项回归",
        contact="测试联系人",
        items=(
            CandidateOrderItem(
                material_id=_MATERIAL,
                raw_name="螺纹钢",
                specification="HRB400 12mm",
                quantity=Decimal("28.571428"),
                unit="吨",
                unit_price=Decimal("3500.000000"),
            ),
        ),
    )
    async with async_session_factory() as session, session.begin():
        task = await session.get(AgentTask, task_id)
        assert task is not None
        task.state = "SUBMITTING"
        task.current_step = "commit_order"
        task.checkpoint_ref = checkpoint
        start_jobs = list(
            (
                await session.scalars(select(TaskJob).where(TaskJob.task_id == task_id))
            ).all()
        )
        for job in start_jobs:
            job.state = "DONE"
            job.worker_id = None
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
            actor=actor,
        )
        payload = {
            "decision": "APPROVE",
            "draft_id": str(draft_id),
            "content_hash": saved.content_hash,
            "checkpoint_id": checkpoint,
            "interrupt_id": interrupt,
        }
        session.add(
            TaskCommand(
                id=command_id,
                task_id=task_id,
                actor_id=_NORTH_APPROVER,
                kind="REVIEW",
                payload=payload,
                expected_version=saved.version,
                target_interrupt_id=interrupt,
                request_id=f"issue040-review-{uuid4()}",
                payload_hash=hashlib.sha256(str(payload).encode()).hexdigest(),
            )
        )
        await session.flush()
        session.add(
            TaskApproval(
                id=approval_id,
                task_id=task_id,
                command_id=command_id,
                draft_id=draft_id,
                actor_id=_NORTH_APPROVER,
                department_id=_NORTH_DEPARTMENT,
                version=saved.version,
                content_hash=saved.content_hash,
                checkpoint_ref=checkpoint,
                interrupt_id=interrupt,
                decision="APPROVE",
            )
        )
        draft = await session.get(Draft, draft_id)
        assert draft is not None
        draft.lock_state = "SUBMITTING"
    return task_id, command_id, approval_id, draft_id


async def _commit_batch_child(prepared):
    task_id, command_id, approval_id, _ = prepared
    async with async_session_factory() as session, session.begin():
        return await commit_approved_order(
            session,
            task_id=task_id,
            command_id=command_id,
            approval_id=approval_id,
        )


@pytest.mark.skipif(
    os.getenv("TRADEPILOT_TEST_MODE") != "1",
    reason="需由 verify_issue_040.ps1 在隔离 PostgreSQL Compose 环境执行",
)
@pytest.mark.asyncio
async def test_batch_children_competing_for_one_contract_cannot_overoccupy_quota() -> (
    None
):
    """真实导入批次的两个子任务并发提交同一合同，只允许一笔占额成功。"""
    issue016 = runpy.run_path(str(Path(__file__).with_name("test_issue_016.py")))
    issue029 = runpy.run_path(str(Path(__file__).with_name("test_issue_029.py")))
    contract_id, _ = await issue016["_new_contract"](
        total_amount=Decimal("250000.00")
    )
    async with async_session_factory() as session:
        contract = await session.get(Contract, contract_id)
        assert contract is not None
        contract_number = contract.number

    rows = [
        issue029["_row"]("额度竞争组甲", "螺纹钢", Decimal("28.571428")),
        issue029["_row"]("额度竞争组乙", "螺纹钢", Decimal("28.571428")),
    ]
    for row in rows:
        row[2] = contract_number
        row[7] = "螺纹钢"
        row[8] = "HRB400 12mm"
        row[11] = Decimal("3500.000000")

    async with issue029["_client"]() as client:
        artifact_id = await issue029["_upload"](client, issue029["_workbook"](rows))
        response = await issue029["_create_batch"](
            client, artifact_id, f"issue040-batch-race-{uuid4()}"
        )
        assert response.status_code == 202, response.text
        batch_id = UUID(response.json()["data"]["batch_id"])

    async with async_session_factory() as session:
        items = list(
            (
                await session.scalars(
                    select(ImportBatchItem)
                    .where(ImportBatchItem.batch_id == batch_id)
                    .order_by(ImportBatchItem.group_id)
                )
            ).all()
        )
        assert len(items) == 2
        assert all(item.task_id is not None for item in items), [
            (item.group_id, item.preview_status, item.candidate_json)
            for item in items
            if item.task_id is None
        ]
        assert all(item.preview_status == "READY_FOR_REVIEW" for item in items)
        assert {item.candidate_json["contract_id"] for item in items} == {
            str(contract_id)
        }

    # 预览阶段允许两笔各 10 万的订单进入审核；随后额度收紧到 15 万，
    # 验证最终提交事务会重新核验共享合同，而不是信任旧预览结果。
    async with async_session_factory() as session, session.begin():
        contract = await session.get(Contract, contract_id)
        assert contract is not None
        contract.total_amount = Decimal("150000.00")

    prepared = [
        await _prepare_batch_child(item.task_id, contract_id, issue016["_OWNER"])
        for item in items
        if item.task_id is not None
    ]
    results = await asyncio.gather(*[_commit_batch_child(row) for row in prepared])
    assert sorted(result.state for result in results) == ["BLOCKED_RULE", "COMPLETED"]
    # 直接调用事务函数绕过 Worker 图投影；同步写入其正常完成后会投影的任务状态，
    # 以真实 API 回读批次聚合状态与数据库业务事实。
    async with async_session_factory() as session, session.begin():
        for row, result in zip(prepared, results, strict=True):
            task = await session.get(AgentTask, row[0])
            assert task is not None
            task.state = result.state

    task_ids = [row[0] for row in prepared]
    blocked = prepared[
        next(i for i, result in enumerate(results) if result.state == "BLOCKED_RULE")
    ]
    async with async_session_factory() as session:
        orders = await session.scalar(
            select(func.count()).select_from(Order).where(Order.task_id.in_(task_ids))
        )
        occupations = await session.scalar(
            select(func.count())
            .select_from(QuotaOccupation)
            .where(QuotaOccupation.task_id.in_(task_ids))
        )
        balance = await session.get(ContractBalance, contract_id)
        audit_commits = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.task_id.in_(task_ids),
                AuditEvent.action == "order.committed",
            )
        )
        audit_blocks = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.task_id.in_(task_ids),
                AuditEvent.action == "order.commit_blocked",
            )
        )
        blocked_approval = await session.get(TaskApproval, blocked[2])
        blocked_draft = await session.get(Draft, blocked[3])
    assert orders == occupations == audit_commits == 1
    assert audit_blocks == 1
    assert balance is not None and balance.occupied_amount == Decimal("100000.00")
    assert blocked_approval is not None and blocked_approval.invalidated_at is not None
    assert blocked_draft is not None and blocked_draft.lock_state == "EDITABLE"

    async with issue029["_client"]() as client:
        batch_result = await client.get(f"/api/v1/imports/{batch_id}")
        assert batch_result.status_code == 200, batch_result.text
        assert batch_result.json()["data"]["state"] == "PARTIAL"
        assert batch_result.json()["data"]["summary"]["completed_count"] == 1


@pytest.mark.skipif(
    os.getenv("TRADEPILOT_TEST_MODE") != "1",
    reason="需由 verify_issue_040.ps1 在隔离 PostgreSQL Compose 环境执行",
)
@pytest.mark.asyncio
async def test_process_crashes_before_and_after_commit_recover_without_duplicate_order() -> (
    None
):
    """在独立进程的事务提交前后强制退出，核对回滚或幂等回查结果。"""
    issue016 = runpy.run_path(str(Path(__file__).with_name("test_issue_016.py")))
    helper = Path(__file__).with_name("issue_040_crash_worker.py")

    for mode, expected_exit in (("before", 31), ("after", 32)):
        contract_id, line_id = await issue016["_new_contract"]()
        prepared = await issue016["_prepare"](contract_id, line_id)
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(helper),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": "/workspace:/app"},
        )
        stdout, stderr = await asyncio.wait_for(
            child.communicate(
                json.dumps(
                    {
                        "mode": mode,
                        "task_id": str(prepared[0]),
                        "command_id": str(prepared[1]),
                        "approval_id": str(prepared[2]),
                    }
                ).encode(),
            ),
            timeout=30,
        )
        assert child.returncode == expected_exit, (
            f"{mode} 崩溃注入未到达目标事务边界："
            f"stdout={stdout.decode(errors='replace')} "
            f"stderr={stderr.decode(errors='replace')}"
        )

        async with async_session_factory() as session:
            order_count = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.task_id == prepared[0])
            )
            occupation_count = await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id == prepared[0])
            )
            balance = await session.get(ContractBalance, contract_id)
        assert balance is not None

        if mode == "before":
            assert order_count == occupation_count == 0
            assert balance.occupied_amount == Decimal("0.00")
            result = await issue016["_submit"](prepared)
            assert result.state == "COMPLETED" and not result.replayed
        else:
            assert order_count == occupation_count == 1
            assert balance.occupied_amount == Decimal("7000.00")
            result = await issue016["_submit"](prepared)
            assert result.state == "COMPLETED" and result.replayed

        async with async_session_factory() as session:
            order_count = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.task_id == prepared[0])
            )
            occupation_count = await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.task_id == prepared[0])
            )
            balance = await session.get(ContractBalance, contract_id)
        assert order_count == occupation_count == 1
        assert balance is not None and balance.occupied_amount == Decimal("7000.00")


@pytest.mark.skipif(
    os.getenv("TRADEPILOT_TEST_MODE") != "1",
    reason="需由 verify_issue_040.ps1 在隔离 PostgreSQL Compose 环境执行",
)
@pytest.mark.asyncio
async def test_postgres_wait_recovery_survives_ten_worker_process_restarts() -> None:
    """每轮重启 Worker 恢复输入等待，并由新进程回读审批等待，重复十轮。"""
    source = Path(__file__).with_name("test_issue_013.py")
    recovery_test = runpy.run_path(str(source))[
        "test_postgres_checkpoint_and_new_worker_resume_same_task_after_reconnect"
    ]

    completed_cycles = 0
    for _ in range(10):
        # 既有集成用例会创建独立任务，断开旧执行租约，并启动新 Python Worker
        # 进程从 WAITING_INPUT 检查点恢复，并把任务推进到 WAITING_APPROVAL；
        # 随后再启动全新进程回读审批等待投影、版本和 checkpoint 引用。
        await recovery_test()
        async with async_session_factory() as session:
            task = await session.scalar(
                select(AgentTask)
                .join(TaskCommand, TaskCommand.task_id == AgentTask.id)
                .where(
                    AgentTask.state == "WAITING_APPROVAL",
                    TaskCommand.kind == "START",
                    TaskCommand.request_id.startswith("issue13-"),
                )
                .order_by(AgentTask.created_at.desc())
                .limit(1)
            )
            assert task is not None
            wait = await session.scalar(
                select(TaskWait).where(
                    TaskWait.task_id == task.id,
                    TaskWait.active.is_(True),
                )
            )
            assert wait is not None
            task_id = task.id

        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("issue_040_crash_worker.py")),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": "/workspace:/app"},
        )
        stdout, stderr = await asyncio.wait_for(
            child.communicate(
                json.dumps({"mode": "inspect_wait", "task_id": str(task_id)}).encode()
            ),
            timeout=30,
        )
        assert child.returncode == 0, stderr.decode(errors="replace")
        restored = json.loads(stdout)
        assert restored["task_id"] == str(task_id)
        assert restored["task_state"] == "WAITING_APPROVAL"
        assert restored["wait_kind"] == "APPROVAL"
        assert restored["task_checkpoint_ref"] == restored["wait_checkpoint_ref"]
        assert restored["draft_version"] == 1
        completed_cycles += 1

    assert completed_cycles == 10
