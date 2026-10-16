"""PostgreSQL 集成验证：持久命令、同任务锁、epoch 防护与原任务恢复。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from app.database import async_session_factory, engine
from app.models import AgentTask, TaskCommand, TaskSession
from app.tasks.checkpointer import EpochFencedPostgresSaver
from app.tasks.service import (
    IdempotencyConflict,
    StaleExecutionEpoch,
    TaskExecutionLease,
    TaskLockUnavailable,
    _payload_hash,
    create_task,
    enqueue_command,
    recover_abandoned_jobs,
    try_claim_job,
)
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

OWNER_ID = UUID("00000000-0000-4000-8000-000000000100")
DEPARTMENT_ID = UUID("00000000-0000-4000-8000-000000000001")


def test_request_hash_is_stable_and_covers_command_version_and_interrupt() -> None:
    baseline = _payload_hash(
        kind="INPUT", payload={"b": 2, "a": 1}, expected_version=3,
        target_interrupt_id="interrupt-1",
    )
    assert baseline == _payload_hash(
        kind="INPUT", payload={"a": 1, "b": 2}, expected_version=3,
        target_interrupt_id="interrupt-1",
    )
    assert baseline != _payload_hash(
        kind="INPUT", payload={"a": 1, "b": 2}, expected_version=4,
        target_interrupt_id="interrupt-1",
    )
    assert baseline != _payload_hash(
        kind="INPUT", payload={"a": 1, "b": 2}, expected_version=3,
        target_interrupt_id="interrupt-2",
    )


async def _create_task_job(task_type: str = "SALES", *, session_id: UUID | None = None):
    async with async_session_factory() as session, session.begin():
        task = await create_task(
            session, owner_id=OWNER_ID, department_id=DEPARTMENT_ID,
            task_type=task_type, task_id=uuid4(), session_id=session_id,
        )
        queued = await enqueue_command(
            session, task_id=task.id, actor_id=OWNER_ID, kind="START",
            payload={"message": "恢复同一任务"}, request_id=f"req-{uuid4()}",
        )
        return task.id, queued


@pytest.mark.asyncio
async def test_sales_purchase_tasks_are_distinct_and_commands_are_durable_and_idempotent() -> None:
    sales_id, sales_command = await _create_task_job("SALES")
    async with async_session_factory() as session:
        sales = await session.get(AgentTask, sales_id)
        assert sales is not None
        shared_session_id = sales.session_id
    purchase_id, purchase_command = await _create_task_job(
        "PURCHASE", session_id=shared_session_id
    )
    assert sales_id != purchase_id
    assert sales_command.task_id == sales_id
    assert purchase_command.task_id == purchase_id
    async with async_session_factory() as session:
        purchase = await session.get(AgentTask, purchase_id)
        nav = await session.get(TaskSession, shared_session_id)
        assert purchase is not None and purchase.session_id == shared_session_id
        assert nav is not None and nav.last_task_id == purchase_id

    async with async_session_factory() as session, session.begin():
        original = await session.get(TaskCommand, sales_command.command_id)
        assert original is not None
        replay = await enqueue_command(
            session, task_id=sales_id, actor_id=OWNER_ID, kind="START",
            payload={"message": "恢复同一任务"},
            request_id=original.request_id,
        )
        assert replay.replayed
        assert replay.task_id == sales_id
        assert replay.job_id == sales_command.job_id
        with pytest.raises(IdempotencyConflict):
            await enqueue_command(
                session, task_id=sales_id, actor_id=OWNER_ID, kind="START",
                payload={"message": "不同内容"}, request_id=original.request_id,
            )
        with pytest.raises(IdempotencyConflict):
            await enqueue_command(
                session, task_id=sales_id, actor_id=OWNER_ID, kind="START",
                payload={"message": "恢复同一任务"}, request_id=original.request_id,
                expected_version=2,
            )

    async with async_session_factory() as session:
        command = await session.get(TaskCommand, sales_command.command_id)
        assert command is not None and command.state == "READY"
        assert command.task_id == sales_id


@pytest.mark.asyncio
async def test_worker_lock_recovery_keeps_task_id_and_fences_stale_worker_and_checkpointer() -> None:
    task_id, queued = await _create_task_job()
    async with async_session_factory() as session, session.begin():
        job = await try_claim_job(
            session, worker_id="worker-old", supported_kinds=frozenset({"RUN_TASK"})
        )
        assert job is not None and job.id == queued.job_id and job.task_id == task_id
        job_id = job.id

    first_connection = await engine.connect()
    second_connection = await engine.connect()
    lease_one = await TaskExecutionLease.acquire(
        first_connection, task_id=task_id, job_id=job_id, worker_id="worker-old"
    )
    assert lease_one.epoch == 1
    with pytest.raises(TaskLockUnavailable):
        await TaskExecutionLease.acquire(
            second_connection, task_id=task_id, job_id=job_id,
            worker_id="worker-racing",
        )

    class WorkflowState(TypedDict):
        count: int

    def advance(state: WorkflowState) -> dict[str, int]:
        return {"count": state["count"] + 1}

    builder = StateGraph(WorkflowState)
    builder.add_node("advance", advance)
    builder.add_edge(START, "advance")
    builder.add_edge("advance", END)
    saver = EpochFencedPostgresSaver(lease_one)
    await saver.setup()
    graph = builder.compile(checkpointer=saver)
    graph_config = {"configurable": {"thread_id": str(task_id)}}
    result = await graph.ainvoke({"count": 0}, graph_config)
    assert result["count"] == 1
    saved = await graph.aget_state(graph_config)
    assert saved.values == {"count": 1}
    with pytest.raises(ValueError, match="thread_id"):
        await graph.ainvoke({"count": 1}, {"configurable": {"thread_id": str(uuid4())}})

    # 心跳超时只能发现候选；活 Worker 仍持有 session lock 时协调器不得接管。
    async with second_connection.begin():
        await second_connection.execute(
            text("UPDATE jobs SET heartbeat_at=:old WHERE id=:job_id"),
            {"old": datetime.now(UTC) - timedelta(hours=1), "job_id": job_id},
        )
    async with second_connection.begin():
        assert await recover_abandoned_jobs(
            second_connection, stale_before=datetime.now(UTC) - timedelta(minutes=1)
        ) == 0

    # 模拟 Worker 进程崩溃导致 PostgreSQL 专属连接断开；服务器释放 session 锁。
    await first_connection.invalidate()
    async with second_connection.begin():
        assert await recover_abandoned_jobs(
            second_connection, stale_before=datetime.now(UTC) - timedelta(minutes=1)
        ) == 1
    async with first_connection.begin():
        with pytest.raises(StaleExecutionEpoch):
            await lease_one.assert_current()
    async with async_session_factory() as session, session.begin():
        recovered_job = await try_claim_job(
            session, worker_id="worker-new", supported_kinds=frozenset({"RUN_TASK"})
        )
        assert recovered_job is not None
        assert recovered_job.id == job_id
        assert recovered_job.task_id == task_id

    lease_two = await TaskExecutionLease.acquire(
        second_connection, task_id=task_id, job_id=job_id, worker_id="worker-new"
    )
    assert lease_two.epoch == 3
    async with lease_two.write_session() as business_session:
        await business_session.execute(
            text("UPDATE sessions SET title='当前 epoch 写入' WHERE id=(SELECT session_id FROM agent_tasks WHERE id=:task_id)"),
            {"task_id": task_id},
        )
    async with first_connection.begin():
        with pytest.raises(StaleExecutionEpoch):
            await lease_one.assert_current()
    with pytest.raises(StaleExecutionEpoch):
        await lease_one.project_state("FAILED")
    with pytest.raises(StaleExecutionEpoch):
        async with lease_one.write_transaction() as stale_connection:
            await stale_connection.execute(
                text("UPDATE sessions SET title='旧执行者写入' WHERE id=(SELECT session_id FROM agent_tasks WHERE id=:task_id)"),
                {"task_id": task_id},
            )
    with pytest.raises(StaleExecutionEpoch):
        await graph.ainvoke({"count": 1}, graph_config)

    await lease_two.project_state("WAITING_INPUT", checkpoint_ref=f"task:{task_id}")
    async with async_session_factory() as session:
        task = await session.get(AgentTask, task_id)
        assert task is not None
        assert task.state == "WAITING_INPUT"
        assert task.execution_epoch == 3
        assert task.checkpoint_ref == f"task:{task_id}"
        nav = await session.get(TaskSession, task.session_id)
        assert nav is not None and nav.title == "当前 epoch 写入"

    await lease_two.release()
    await first_connection.close()
    await second_connection.close()
