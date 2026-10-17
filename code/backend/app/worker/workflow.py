"""把持久队列命令接入真实模型/MCP 与 PostgreSQL checkpoint 图。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.function_calling import create_database_runner
from app.agent.workflow import (
    GRAPH_VERSION,
    WorkflowResumeConflict,
    WorkflowState,
    build_task_graph,
    resume_or_continue_task_graph,
    start_or_continue_task_graph,
    task_graph_config,
    wait_details,
)
from app.auth.context import AuthContext
from app.config import Settings, settings
from app.drafts.service import load_draft
from app.models import (
    AgentTask,
    Draft,
    TaskCommand,
    TaskJob,
    TaskWait,
    User,
    UserDepartmentScope,
    UserRole,
)
from app.tasks.checkpointer import EpochFencedPostgresSaver
from app.tasks.service import TaskExecutionLease
from app.tasks.waits import (
    bind_wait_command,
    complete_resume_projection,
    project_wait,
)


class DatabaseWorkflowServices:
    """生产图节点只使用数据库身份、真实 Function Calling/MCP 与持久草稿。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        lease: TaskExecutionLease,
        job: TaskJob,
        task: AgentTask,
        actor: AuthContext,
        config: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.lease = lease
        self.job = job
        self.task = task
        self.actor = actor
        self.config = config
        self.runner = create_database_runner(
            session_factory=session_factory, config=config
        )

    async def execute_tools(self, state: WorkflowState) -> Mapping[str, Any]:
        result = await self.runner.run(
            task_id=self.task.id,
            job_id=self.job.id,
            task_type=self.task.task_type,
            actor=self.actor,
            department_id=self.task.department_id,
            lease=self.lease,
            user_text=state["parsed_input"],
            input_at=datetime.fromisoformat(state["input_at"]),
        )
        return {
            "provider": result.provider,
            "model": result.response_model,
            "trace_id": str(result.trace_id),
            "model_rounds": result.model_rounds,
            "tool_calls": result.tool_calls,
        }

    async def validate_draft(self, state: WorkflowState) -> Mapping[str, Any]:
        async with self.session_factory() as session:
            draft = await session.scalar(
                select(Draft).where(Draft.task_id == self.task.id)
            )
            if draft is None or draft.current_version == 0:
                return {
                    "missing_fields": [
                        {
                            "field": "draft",
                            "message": "尚未形成可校验的订单草稿，请补充客户、合同和物料信息。",
                        }
                    ]
                }
            snapshot = await load_draft(session, draft.id)
            if snapshot is None:
                raise RuntimeError("草稿头存在但当前版本缺失")
            return {
                "draft_id": str(snapshot.draft_id),
                "draft_version": snapshot.version,
                "missing_fields": [
                    {"field": item.field, "message": item.message}
                    for item in snapshot.issues
                ],
            }

    async def verify_persisted_draft(self, state: WorkflowState) -> Mapping[str, Any]:
        async with self.session_factory() as session:
            draft = await session.scalar(
                select(Draft).where(Draft.task_id == self.task.id)
            )
            if draft is None or draft.current_version == 0:
                return {"exists": False}
            snapshot = await load_draft(session, draft.id)
            if snapshot is None:
                return {"exists": False}
            return {
                "exists": True,
                "draft_id": str(snapshot.draft_id),
                "draft_version": snapshot.version,
                "reviewable": snapshot.is_validated,
            }

    async def load_resume_command(
        self, command_id: UUID, *, state: WorkflowState
    ) -> Mapping[str, Any]:
        async with self.session_factory() as session:
            command = await session.get(TaskCommand, command_id)
            wait = await session.scalar(
                select(TaskWait).where(
                    TaskWait.task_id == self.task.id, TaskWait.active
                )
            )
            if (
                command is None
                or command.task_id != self.task.id
                or command.kind != "INPUT"
                or command.state != "READY"
                or wait is None
                or wait.pending_command_id != command.id
                or command.target_interrupt_id != wait.interrupt_id
                or wait.draft_version != command.expected_version
            ):
                raise WorkflowResumeConflict("恢复命令与当前有效等待或草稿版本不匹配")
            text = command.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise WorkflowResumeConflict("恢复命令缺少有效补充内容")
            return {
                "id": str(command.id),
                "text": text,
                "input_at": str(
                    command.payload.get("input_at", command.created_at.isoformat())
                ),
            }


async def create_run_task_handler(
    *, session_factory: async_sessionmaker, config: Settings = settings
):
    """创建持久作业 handler；注册时不暴露审批或正式订单能力。"""

    async def handle(lease: TaskExecutionLease, job: TaskJob) -> None:
        async with session_factory() as session:
            command = await session.get(TaskCommand, job.command_id)
            task = await session.get(AgentTask, lease.task_id)
            if command is None or task is None:
                raise LookupError("任务作业缺少命令或任务")
            user = await session.get(User, task.owner_id)
            roles = frozenset(
                await session.scalars(
                    select(UserRole.role).where(UserRole.user_id == task.owner_id)
                )
            )
            departments = frozenset(
                await session.scalars(
                    select(UserDepartmentScope.department_id).where(
                        UserDepartmentScope.user_id == task.owner_id
                    )
                )
            )
            if user is None or not user.active or task.department_id not in departments:
                raise PermissionError("任务所有者已失效或部门范围已撤销")
            task_copy = SimpleNamespace(
                id=task.id,
                owner_id=task.owner_id,
                task_type=task.task_type,
                department_id=task.department_id,
                graph_version=task.graph_version,
            )
            command_copy = SimpleNamespace(
                id=command.id,
                kind=command.kind,
                payload=dict(command.payload),
                created_at=command.created_at,
                expected_version=command.expected_version,
                target_interrupt_id=command.target_interrupt_id,
            )
            actor = AuthContext(
                user_id=user.id,
                login=user.login,
                roles=roles,
                department_ids=departments,
            )

        if task_copy.graph_version != GRAPH_VERSION:
            raise WorkflowResumeConflict("当前 Worker 不支持该任务的固定图版本")
        services = DatabaseWorkflowServices(
            session_factory=session_factory,
            lease=lease,
            job=job,
            task=task_copy,
            actor=actor,
            config=config,
        )
        saver = EpochFencedPostgresSaver(lease)
        await saver.setup()
        graph = build_task_graph(services, checkpointer=saver)
        resume_projection: tuple[str, str] | None = None
        if command_copy.kind == "INPUT":
            async with session_factory() as session:
                wait = await session.scalar(
                    select(TaskWait).where(TaskWait.task_id == task.id, TaskWait.active)
                )
                if (
                    wait is not None
                    and command_copy.target_interrupt_id == wait.interrupt_id
                ):
                    wait_checkpoint, wait_interrupt = (
                        wait.checkpoint_ref,
                        wait.interrupt_id,
                    )
                    async with lease.write_session() as write_session:
                        await bind_wait_command(
                            write_session,
                            task_id=task.id,
                            checkpoint_ref=wait_checkpoint,
                            interrupt_id=wait_interrupt,
                            command_id=command_copy.id,
                            expected_version=command_copy.expected_version,
                        )
                else:
                    consumed = await session.scalar(
                        select(TaskWait).where(
                            TaskWait.task_id == task.id,
                            TaskWait.consumed_command_id == command_copy.id,
                        )
                    )
                    if consumed is None:
                        raise WorkflowResumeConflict("输入命令引用了过期等待点")
                    wait_checkpoint, wait_interrupt = (
                        consumed.checkpoint_ref,
                        consumed.interrupt_id,
                    )
                resume_projection = (wait_checkpoint, wait_interrupt)
            result = await resume_or_continue_task_graph(
                graph,
                task_id=task.id,
                command_id=command_copy.id,
                checkpoint_id=wait_checkpoint,
                interrupt_id=wait_interrupt,
                graph_version=task_copy.graph_version,
            )
        else:
            payload = command_copy.payload
            input_text = payload.get("text", payload.get("message"))
            if not isinstance(input_text, str) or not input_text.strip():
                raise ValueError("任务命令缺少文本输入")
            input_at = payload.get("input_at", command_copy.created_at.isoformat())
            if not isinstance(input_at, str):
                raise ValueError("任务输入时间格式无效")
            result = await start_or_continue_task_graph(
                graph,
                input_state={
                    "task_id": str(task.id),
                    "graph_version": task_copy.graph_version,
                    "command_id": str(command_copy.id),
                    "command_kind": command_copy.kind,
                    "input_text": input_text,
                    "input_at": input_at,
                },
            )
        snapshot = await graph.aget_state(task_graph_config(task.id))
        details = wait_details(snapshot)
        if command_copy.kind == "INPUT" and resume_projection is not None:
            await complete_resume_projection(
                lease,
                command_id=command_copy.id,
                checkpoint_ref=resume_projection[0],
                interrupt_id=resume_projection[1],
                result={
                    **result,
                    "checkpoint_ref": snapshot.config.get("configurable", {}).get(
                        "checkpoint_id"
                    ),
                },
                next_wait=details,
            )
        elif details is not None:
            await project_wait(lease, details)
        else:
            checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
            await lease.project_state(
                result.get("projection_state", "WAITING_INPUT"),
                checkpoint_ref=checkpoint_id,
                current_step="persist_draft",
            )

    return handle
