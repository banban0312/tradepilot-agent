"""M2-06 LangGraph 中断与持久恢复的离线契约测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.workflow import (
    GRAPH_VERSION,
    WorkflowResumeConflict,
    build_task_graph,
    resume_or_continue_task_graph,
    resume_task_graph,
    start_or_continue_task_graph,
    task_graph_config,
    wait_details,
)


class FixedWorkflowServices:
    """离线替身只验证图语义；PostgreSQL 真恢复由 PowerShell 入口执行。"""

    def __init__(self) -> None:
        self.executed_inputs: list[str] = []
        self.saved_versions = 0

    async def execute_tools(self, state):
        self.executed_inputs.append(state["parsed_input"])
        return {"tool_calls": 1}

    async def validate_draft(self, state):
        complete = "规格" in state["parsed_input"]
        return {
            "draft_id": "00000000-0000-0000-0000-000000000013",
            "draft_version": 1,
            "missing_fields": []
            if complete
            else [{"field": "items[0].specification", "message": "请补充物料规格"}],
        }

    async def verify_persisted_draft(self, state):
        self.saved_versions += 1
        return {
            "exists": True,
            "draft_id": state["draft_id"],
            "draft_version": state["draft_version"],
            "content_hash": "a" * 64,
            "reviewable": True,
        }

    async def load_resume_command(self, command_id: UUID, *, state):
        return {"id": str(command_id), "text": "螺纹钢规格 HRB400"}


async def _waiting_graph():
    task_id = uuid4()
    services = FixedWorkflowServices()
    graph = build_task_graph(services, checkpointer=InMemorySaver())
    state = await graph.ainvoke(
        {
            "task_id": str(task_id),
            "graph_version": GRAPH_VERSION,
            "command_id": str(uuid4()),
            "command_kind": "CREATE",
            "input_text": "螺纹钢 200 吨",
            "input_at": "2026-10-17T09:00:00+08:00",
        },
        config=task_graph_config(task_id),
    )
    snapshot = await graph.aget_state(task_graph_config(task_id))
    details = wait_details(snapshot)
    assert details is not None
    return task_id, services, graph, state, details


@pytest.mark.asyncio
async def test_missing_spec_interrupts_with_specific_wait_and_no_draft_finalize():
    _, services, _, state, details = await _waiting_graph()

    assert state["projection_state"] == "WAITING_INPUT"
    assert details["kind"] == "input"
    assert details["missing_fields"] == [
        {"field": "items[0].specification", "message": "请补充物料规格"}
    ]
    assert details["draft_version"] == 1
    assert services.saved_versions == 0
    assert services.executed_inputs == ["原始请求：\n螺纹钢 200 吨"]


@pytest.mark.asyncio
async def test_replayed_start_command_reuses_existing_wait_checkpoint():
    task_id = uuid4()
    command_id = uuid4()
    services = FixedWorkflowServices()
    graph = build_task_graph(services, checkpointer=InMemorySaver())
    input_state = {
        "task_id": str(task_id),
        "graph_version": GRAPH_VERSION,
        "command_id": str(command_id),
        "command_kind": "START",
        "input_text": "螺纹钢 200 吨",
        "input_at": "2026-10-17T09:00:00+08:00",
    }

    first = await start_or_continue_task_graph(graph, input_state=input_state)
    second = await start_or_continue_task_graph(graph, input_state=input_state)
    assert first["projection_state"] == second["projection_state"] == "WAITING_INPUT"
    assert services.executed_inputs == ["原始请求：\n螺纹钢 200 吨"]
    assert services.saved_versions == 0


@pytest.mark.asyncio
async def test_resume_requires_current_task_checkpoint_interrupt_and_graph_version():
    task_id, services, graph, _, details = await _waiting_graph()
    assert details["checkpoint_id"]

    with pytest.raises(WorkflowResumeConflict, match="等待点"):
        await resume_task_graph(
            graph,
            task_id=uuid4(),
            command_id=uuid4(),
            checkpoint_id=details["checkpoint_id"],
            interrupt_id=details["interrupt_id"],
            graph_version=GRAPH_VERSION,
        )
    with pytest.raises(WorkflowResumeConflict, match="图版本"):
        await resume_task_graph(
            graph,
            task_id=task_id,
            command_id=uuid4(),
            checkpoint_id=details["checkpoint_id"],
            interrupt_id=details["interrupt_id"],
            graph_version="old-v0",
        )
    with pytest.raises(WorkflowResumeConflict, match="不匹配"):
        await resume_task_graph(
            graph,
            task_id=task_id,
            command_id=uuid4(),
            checkpoint_id="stale-checkpoint",
            interrupt_id=details["interrupt_id"],
            graph_version=GRAPH_VERSION,
        )
    with pytest.raises(WorkflowResumeConflict, match="不匹配"):
        await resume_task_graph(
            graph,
            task_id=task_id,
            command_id=uuid4(),
            checkpoint_id=details["checkpoint_id"],
            interrupt_id="stale-interrupt",
            graph_version=GRAPH_VERSION,
        )
    assert services.saved_versions == 0


@pytest.mark.asyncio
async def test_postgres_checkpoint_and_new_worker_resume_same_task_after_reconnect():
    """用两代 PostgreSQL saver/执行租约验证服务重启边界，数据只用隔离示例。"""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select, text

    from app.database import async_session_factory, engine
    from app.models import AgentTask, TaskWait
    from app.tasks.checkpointer import EpochFencedPostgresSaver
    from app.tasks.service import (
        TaskExecutionLease,
        create_task,
        enqueue_command,
        recover_abandoned_jobs,
        try_claim_job,
    )
    from app.tasks.waits import bind_wait_command

    owner_id = UUID("00000000-0000-4000-8000-000000000100")
    department_id = UUID("00000000-0000-4000-8000-000000000001")
    async with async_session_factory() as session, session.begin():
        task = await create_task(
            session,
            owner_id=owner_id,
            department_id=department_id,
            task_type="SALES",
            task_id=uuid4(),
        )
        task_id = task.id
        start = await enqueue_command(
            session,
            task_id=task_id,
            actor_id=owner_id,
            kind="START",
            payload={"text": "螺纹钢 200 吨"},
            request_id=f"issue13-{uuid4()}",
        )
    worker_id = f"issue13-{uuid4()}"
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE jobs SET available_at=now() + interval '1 day' WHERE state='READY' AND id != :job"
            ),
            {"job": start.job_id},
        )
        job1 = await try_claim_job(
            session, worker_id=worker_id, supported_kinds=frozenset({"RUN_TASK"})
        )
        assert job1 is not None and job1.id == start.job_id

    schema = f"tp_issue013_{uuid4().hex}"
    connection1 = await engine.connect()
    lease1 = await TaskExecutionLease.acquire(
        connection1,
        task_id=task_id,
        job_id=start.job_id,
        worker_id=worker_id,
    )
    saver1 = EpochFencedPostgresSaver(lease1, checkpoint_schema=schema)
    await saver1.setup()
    services1 = FixedWorkflowServices()
    graph1 = build_task_graph(services1, checkpointer=saver1)
    state1 = await graph1.ainvoke(
        {
            "task_id": str(task_id),
            "graph_version": GRAPH_VERSION,
            "command_id": str(start.command_id),
            "command_kind": "CREATE",
            "input_text": "螺纹钢 200 吨",
            "input_at": datetime.now(UTC).isoformat(),
        },
        config=task_graph_config(task_id),
    )
    snapshot1 = await graph1.aget_state(task_graph_config(task_id))
    details = wait_details(snapshot1)
    assert details is not None
    assert state1["projection_state"] == "WAITING_INPUT"
    # 模拟 worker 在 checkpoint 已提交、业务等待投影尚未提交时崩溃。
    await lease1.release()
    await connection1.close()

    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE jobs SET heartbeat_at=now() - interval '1 hour' WHERE id=:job"
            ),
            {"job": start.job_id},
        )
    async with engine.connect() as recovery_connection, recovery_connection.begin():
        recovered = await recover_abandoned_jobs(
            recovery_connection, stale_before=datetime.now(UTC) - timedelta(minutes=2)
        )
    assert recovered == 1

    # 新进程重领同一个 START 作业，只从 checkpoint 重建等待投影，不重复工具副作用。
    helper = Path(__file__).with_name("issue_013_restart_process.py")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(helper),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        child.communicate(
            json.dumps(
                {
                    "task_id": str(task_id),
                    "job_id": str(start.job_id),
                    "command_id": str(start.command_id),
                    "worker_id": worker_id,
                    "checkpoint_schema": schema,
                    "graph_version": GRAPH_VERSION,
                    "command_kind": "START",
                    "input_text": "螺纹钢 200 吨",
                    "input_at": datetime.now(UTC).isoformat(),
                }
            ).encode()
        ),
        timeout=90,
    )
    assert child.returncode == 0, (
        f"重启 Worker 未能重建等待投影：{stdout.decode()}\n{stderr.decode()}"
    )
    async with async_session_factory() as session:
        projected = await session.scalar(
            select(TaskWait).where(TaskWait.task_id == task_id, TaskWait.active)
        )
        task_projection = await session.get(AgentTask, task_id)
        assert projected is not None and task_projection is not None
        assert projected.checkpoint_ref == details["checkpoint_id"]
        assert projected.interrupt_id == details["interrupt_id"]
        assert projected.draft_version == 1
        assert projected.payload["missing_fields"] == details["missing_fields"]
        assert task_projection.state == "WAITING_INPUT"
        assert task_projection.checkpoint_ref == details["checkpoint_id"]

    # 新命令和新 Worker/连接模拟服务重启；同一 task_id 读取旧 Postgres checkpoint。
    async with async_session_factory() as session, session.begin():
        resume = await enqueue_command(
            session,
            task_id=task_id,
            actor_id=owner_id,
            kind="INPUT",
            payload={"text": "螺纹钢规格 HRB400"},
            request_id=f"issue13-resume-{uuid4()}",
            expected_version=1,
            target_interrupt_id=details["interrupt_id"],
        )
        await bind_wait_command(
            session,
            task_id=task_id,
            checkpoint_ref=details["checkpoint_id"],
            interrupt_id=details["interrupt_id"],
            command_id=resume.command_id,
            expected_version=1,
        )
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE jobs SET available_at=now() + interval '1 day' WHERE state='READY' AND id != :job"
            ),
            {"job": resume.job_id},
        )

    # 由独立 Python 进程重新领取作业并新建 saver，验证真正的进程边界。
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(helper),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        child.communicate(
            json.dumps(
                {
                    "task_id": str(task_id),
                    "job_id": str(resume.job_id),
                    "command_id": str(resume.command_id),
                    "worker_id": worker_id,
                    "checkpoint_schema": schema,
                    "checkpoint_id": details["checkpoint_id"],
                    "interrupt_id": details["interrupt_id"],
                    "graph_version": GRAPH_VERSION,
                    "command_kind": "INPUT",
                }
            ).encode()
        ),
        timeout=90,
    )
    assert child.returncode == 0, (
        f"新 Worker 进程恢复失败：{stdout.decode()}\n{stderr.decode()}"
    )

    async with async_session_factory() as session:
        waits = list(
            (
                await session.scalars(
                    select(TaskWait).where(TaskWait.task_id == task_id)
                )
            ).all()
        )
        task_projection = await session.get(AgentTask, task_id)
        previous_wait = next(wait for wait in waits if wait.interrupt_id == details["interrupt_id"])
        approval_wait = next(wait for wait in waits if wait.interrupt_id != details["interrupt_id"])
        assert len(waits) == 2
        assert not previous_wait.active
        assert previous_wait.consumed_command_id == resume.command_id
        assert approval_wait.active and approval_wait.kind == "APPROVAL"
        assert approval_wait.payload["content_hash"] == "a" * 64
        assert (
            task_projection is not None and task_projection.state == "WAITING_APPROVAL"
        )


@pytest.mark.asyncio
async def test_matching_resume_reuses_original_thread_and_continues_original_graph():
    task_id, services, graph, _, details = await _waiting_graph()
    with pytest.raises(WorkflowResumeConflict, match="图版本"):
        await resume_or_continue_task_graph(
            graph,
            task_id=task_id,
            command_id=uuid4(),
            checkpoint_id=details["checkpoint_id"],
            interrupt_id=details["interrupt_id"],
            graph_version="old-v0",
        )
    result = await resume_task_graph(
        graph,
        task_id=task_id,
        command_id=uuid4(),
        checkpoint_id=details["checkpoint_id"],
        interrupt_id=details["interrupt_id"],
        graph_version=GRAPH_VERSION,
    )

    assert result["task_id"] == str(task_id)
    assert result["graph_version"] == GRAPH_VERSION
    assert result["input_history"][-1]["text"] == "螺纹钢规格 HRB400"
    assert result["projection_state"] == "WAITING_APPROVAL"
    assert services.executed_inputs == [
        "原始请求：\n螺纹钢 200 吨",
        "原始请求：\n螺纹钢 200 吨\n\n用户补充：\n螺纹钢规格 HRB400\n\n当前草稿版本：1",
    ]
    assert services.saved_versions == 1

    retry_result = await resume_or_continue_task_graph(
        graph,
        task_id=task_id,
        command_id=UUID(result["command_id"]),
        checkpoint_id=details["checkpoint_id"],
        interrupt_id=details["interrupt_id"],
        graph_version=GRAPH_VERSION,
    )
    assert retry_result["draft_version"] == 1
    assert services.executed_inputs == [
        "原始请求：\n螺纹钢 200 吨",
        "原始请求：\n螺纹钢 200 吨\n\n用户补充：\n螺纹钢规格 HRB400\n\n当前草稿版本：1",
    ]
    assert services.saved_versions == 1
