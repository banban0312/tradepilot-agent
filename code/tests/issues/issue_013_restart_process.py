"""由集成测试启动的第二代 Worker 进程，读取同一 PostgreSQL checkpoint。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from typing import Any
from uuid import UUID

sys.path.insert(0, "/app")

from sqlalchemy import select

from app.agent.workflow import (
    GRAPH_VERSION,
    WorkflowResumeConflict,
    WorkflowState,
    build_task_graph,
    resume_or_continue_task_graph,
    start_or_continue_task_graph,
    wait_details,
)
from app.database import async_session_factory, engine
from app.models import AgentTask, TaskCommand, TaskWait
from app.tasks.checkpointer import EpochFencedPostgresSaver
from app.tasks.service import TaskExecutionLease, try_claim_job
from app.tasks.waits import complete_resume_projection, project_wait


class RestartServices:
    """集成夹具只替代模型与 MCP；恢复命令和 checkpoint 均来自真实 PostgreSQL。"""

    async def execute_tools(self, state: WorkflowState) -> Mapping[str, Any]:
        return {"tool_calls": 1}

    async def validate_draft(self, state: WorkflowState) -> Mapping[str, Any]:
        return {
            "draft_id": "00000000-0000-0000-0000-000000000013",
            "draft_version": 1,
            "missing_fields": [],
        }

    async def verify_persisted_draft(self, state: WorkflowState) -> Mapping[str, Any]:
        return {
            "exists": True,
            "draft_id": state["draft_id"],
            "draft_version": state["draft_version"],
            "reviewable": True,
        }

    async def load_resume_command(
        self, command_id: UUID, *, state: WorkflowState
    ) -> Mapping[str, Any]:
        async with async_session_factory() as session:
            command = await session.get(TaskCommand, command_id)
            wait = await session.scalar(
                select(TaskWait).where(
                    TaskWait.task_id == UUID(state["task_id"]), TaskWait.active
                )
            )
            if (
                command is None
                or command.task_id != UUID(state["task_id"])
                or command.kind != "INPUT"
                or wait is None
                or wait.pending_command_id != command.id
                or wait.interrupt_id != command.target_interrupt_id
                or wait.draft_version != command.expected_version
            ):
                raise WorkflowResumeConflict("重启进程拒绝不匹配的恢复命令")
            text = command.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise WorkflowResumeConflict("恢复命令缺少补充文本")
            return {"id": str(command.id), "text": text}


async def run(payload: dict[str, str]) -> None:
    task_id = UUID(payload["task_id"])
    job_id = UUID(payload["job_id"])
    command_id = UUID(payload["command_id"])
    worker_id = payload["worker_id"]
    async with async_session_factory() as session, session.begin():
        job = await try_claim_job(
            session, worker_id=worker_id, supported_kinds=frozenset({"RUN_TASK"})
        )
        if job is None or job.id != job_id or job.task_id != task_id:
            raise AssertionError("新 Worker 未领取预期的同任务恢复作业")
        task = await session.get(AgentTask, task_id)
        if task is None or task.graph_version != GRAPH_VERSION:
            raise AssertionError("原任务图版本在重启后发生变化")

    connection = await engine.connect()
    lease = await TaskExecutionLease.acquire(
        connection, task_id=task_id, job_id=job_id, worker_id=worker_id
    )
    try:
        saver = EpochFencedPostgresSaver(
            lease, checkpoint_schema=payload["checkpoint_schema"]
        )
        await saver.setup()
        graph = build_task_graph(RestartServices(), checkpointer=saver)
        if payload["command_kind"] == "START":
            result = await start_or_continue_task_graph(
                graph,
                input_state={
                    "task_id": str(task_id),
                    "graph_version": payload["graph_version"],
                    "command_id": str(command_id),
                    "command_kind": "START",
                    "input_text": payload["input_text"],
                    "input_at": payload["input_at"],
                },
            )
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": str(task_id)}}
            )
            details = wait_details(snapshot)
            if details is None or result["projection_state"] != "WAITING_INPUT":
                raise AssertionError("重放初始作业未从已有中断检查点恢复")
            await project_wait(lease, details)
            await lease.finish()
            return

        result = await resume_or_continue_task_graph(
            graph,
            task_id=task_id,
            command_id=command_id,
            checkpoint_id=payload["checkpoint_id"],
            interrupt_id=payload["interrupt_id"],
            graph_version=payload["graph_version"],
        )
        if (
            result["task_id"] != str(task_id)
            or result["graph_version"] != GRAPH_VERSION
            or result["draft_version"] != 1
            or result["projection_state"] != "WAITING_APPROVAL"
        ):
            raise AssertionError("新 Worker 恢复后的任务或草稿投影不正确")
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(task_id)}})
        details = wait_details(snapshot)
        await complete_resume_projection(
            lease,
            command_id=command_id,
            checkpoint_ref=payload["checkpoint_id"],
            interrupt_id=payload["interrupt_id"],
            result={
                **result,
                "checkpoint_ref": snapshot.config.get("configurable", {}).get(
                    "checkpoint_id"
                ),
            },
            next_wait=details,
        )
        await lease.finish()
    finally:
        try:
            await lease.release()
        finally:
            await connection.close()


if __name__ == "__main__":
    asyncio.run(run(json.load(sys.stdin)))
