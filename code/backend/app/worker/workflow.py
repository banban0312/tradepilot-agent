"""把持久队列命令接入真实模型/MCP 与 PostgreSQL checkpoint 图。"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
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
from app.drafts.rules import precheck_candidate
from app.drafts.service import load_draft
from app.models import (
    AgentTask,
    Draft,
    TaskApproval,
    TaskCommand,
    TaskJob,
    TaskWait,
    User,
    UserDepartmentScope,
    UserRole,
)
from app.observability.service import finish_span, start_span
from app.orders.commit import OrderCommitResultUnknown, commit_approved_order
from app.tasks.checkpointer import EpochFencedPostgresSaver
from app.tasks.service import TaskExecutionLease
from app.tasks.waits import (
    bind_wait_command,
    complete_resume_projection,
    project_wait,
)

logger = logging.getLogger("tradepilot.worker.workflow")


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
            issues = list(snapshot.issues)
            if snapshot.is_validated and snapshot.candidate is not None:
                precheck = await precheck_candidate(
                    session,
                    candidate=snapshot.candidate,
                    actor=self.actor,
                    business_date=datetime.now(
                        ZoneInfo(self.config.business_timezone)
                    ).date(),
                )
                issues.extend(precheck.issues)
            return {
                "draft_id": str(snapshot.draft_id),
                "draft_version": snapshot.version,
                "missing_fields": [
                    {"field": item.field, "message": item.message} for item in issues
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
            issues = list(snapshot.issues)
            if snapshot.is_validated and snapshot.candidate is not None:
                precheck = await precheck_candidate(
                    session,
                    candidate=snapshot.candidate,
                    actor=self.actor,
                    business_date=datetime.now(
                        ZoneInfo(self.config.business_timezone)
                    ).date(),
                )
                issues.extend(precheck.issues)
            return {
                "exists": True,
                "draft_id": str(snapshot.draft_id),
                "draft_version": snapshot.version,
                "content_hash": snapshot.content_hash,
                "reviewable": snapshot.is_validated and not issues,
                "issues": [
                    {"field": issue.field, "message": issue.message} for issue in issues
                ],
            }

    async def load_review_command(
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
                or command.state != "READY"
                or wait is None
                or wait.kind != "APPROVAL"
                or wait.pending_command_id != command.id
                or command.target_interrupt_id != wait.interrupt_id
                or command.expected_version != wait.draft_version
            ):
                raise WorkflowResumeConflict("审批或编辑命令与当前等待版本不匹配")
            if (
                command.kind == "INPUT"
                and command.payload.get("operation") == "DRAFT_PATCH"
            ):
                text = command.payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise WorkflowResumeConflict("草稿修改命令缺少有效说明")
                return {
                    "id": str(command.id),
                    "decision": "EDIT",
                    "text": text,
                    "expected_version": command.expected_version,
                    "input_at": command.created_at.isoformat(),
                }
            if command.kind != "REVIEW":
                raise WorkflowResumeConflict("审批等待点只接受审批或草稿编辑命令")
            approval = await session.scalar(
                select(TaskApproval).where(TaskApproval.command_id == command.id)
            )
            blocked_replay = bool(
                command.result_ref and command.result_ref.get("state") == "BLOCKED_RULE"
            )
            if (
                approval is None
                or (approval.invalidated_at is not None and not blocked_replay)
                or approval.task_id != self.task.id
                or str(approval.draft_id) != wait.payload.get("draft_id")
                or approval.version != wait.draft_version
                or approval.content_hash != wait.payload.get("content_hash")
                or approval.checkpoint_ref != wait.checkpoint_ref
                or approval.interrupt_id != wait.interrupt_id
                or approval.actor_id != command.actor_id
                or command.payload.get("decision") != approval.decision
                or command.payload.get("draft_id") != str(approval.draft_id)
                or command.payload.get("content_hash") != approval.content_hash
                or command.payload.get("checkpoint_id") != approval.checkpoint_ref
                or command.payload.get("interrupt_id") != approval.interrupt_id
            ):
                raise WorkflowResumeConflict("持久审批与当前草稿版本不匹配或已失效")
            draft = await session.get(Draft, approval.draft_id)
            snapshot = await load_draft(session, approval.draft_id)
            if (
                draft is None
                or snapshot is None
                or snapshot.version != approval.version
                or snapshot.content_hash != approval.content_hash
            ):
                raise WorkflowResumeConflict("审批草稿已修改或不再满足校验条件")
            return {
                "id": str(command.id),
                "decision": approval.decision,
                "actor_id": str(approval.actor_id),
                "approval_id": str(approval.id),
                "draft_id": str(approval.draft_id),
                "expected_version": approval.version,
                "content_hash": approval.content_hash,
                "comment": approval.comment,
            }

    async def commit_order(self, state: WorkflowState) -> Mapping[str, Any]:
        """提交只接受图中恢复出的人工批准 ID，不接受模型或请求正文提供的订单参数。"""
        resumed = state.get("resume_command")
        if not isinstance(resumed, Mapping):
            raise WorkflowResumeConflict("订单提交缺少人工审批命令")
        try:
            command_id = UUID(str(resumed.get("id")))
            approval_id = UUID(str(resumed.get("approval_id")))
        except (ValueError, TypeError) as exc:
            raise WorkflowResumeConflict("订单提交缺少有效审批标识") from exc
        started = time.monotonic()
        span_id: UUID | None = None
        try:
            async with self.session_factory() as session, session.begin():
                span = await start_span(
                    session,
                    name="order.commit",
                    kind="ORDER",
                    execution_mode="LIVE",
                    task_id=self.task.id,
                    actor=getattr(self, "actor", None),
                    protocol="postgresql",
                    input_ref={"command_id": command_id, "approval_id": approval_id},
                )
                span_id = span.id
            async with self.lease.write_session() as session:
                result = await commit_approved_order(
                    session,
                    task_id=self.task.id,
                    command_id=command_id,
                    approval_id=approval_id,
                    config=self.config,
                )
            async with self.session_factory() as session, session.begin():
                await finish_span(
                    session,
                    span_id=span_id,
                    status="ERROR" if result.state == "BLOCKED_RULE" else "OK",
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    output_ref={
                        "state": result.state,
                        "order_id": result.order_id,
                        "order_number": result.order_number,
                        "replayed": result.replayed,
                    },
                    error=(
                        {
                            "code": result.code or "ORDER_COMMIT_BLOCKED",
                            "retryable": False,
                        }
                        if result.state == "BLOCKED_RULE"
                        else None
                    ),
                )
            return result.as_dict()
        except SQLAlchemyError as exc:
            if span_id is not None:
                try:
                    async with self.session_factory() as session, session.begin():
                        await finish_span(
                            session,
                            span_id=span_id,
                            status="ERROR",
                            duration_ms=max(
                                0, int((time.monotonic() - started) * 1000)
                            ),
                            error={
                                "code": OrderCommitResultUnknown.code,
                                "retryable": True,
                            },
                        )
                except (SQLAlchemyError, ValueError):
                    # 数据库不可读时保留 RUNNING span；任务失败投影会将其标为中断。
                    logger.warning("订单结果未知时无法完成失败 span", exc_info=True)
            raise OrderCommitResultUnknown(
                "订单事务结果未知，重试前必须回查订单"
            ) from exc
        except Exception as exc:
            code = str(getattr(exc, "code", "ORDER_COMMIT_FAILED"))
            if span_id is not None:
                try:
                    async with self.session_factory() as session, session.begin():
                        await finish_span(
                            session,
                            span_id=span_id,
                            status="ERROR",
                            duration_ms=max(
                                0, int((time.monotonic() - started) * 1000)
                            ),
                            error={"code": code, "retryable": False},
                        )
                except (SQLAlchemyError, ValueError):
                    logger.warning("订单失败 span 保存失败", exc_info=True)
            raise

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
                or (
                    command.expected_version is not None
                    and wait.draft_version != command.expected_version
                )
            ):
                raise WorkflowResumeConflict("恢复命令与当前有效等待或草稿版本不匹配")
            text = command.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise WorkflowResumeConflict("恢复命令缺少有效补充内容")
            return {
                "id": str(command.id),
                "text": text,
                "expected_version": command.expected_version,
                "input_at": str(
                    command.payload.get("input_at", command.created_at.isoformat())
                ),
            }


async def create_run_task_handler(
    *, session_factory: async_sessionmaker, config: Settings = settings
):
    """创建持久作业 handler；注册时不暴露审批或正式订单能力。"""

    async def handle(lease: TaskExecutionLease, job: TaskJob) -> None:
        retry_source_command_id: UUID | None = None
        async with session_factory() as session:
            command = await session.get(TaskCommand, job.command_id)
            task = await session.get(AgentTask, lease.task_id)
            if command is None or task is None:
                raise LookupError("任务作业缺少命令或任务")
            if command.kind == "RETRY":
                source_id = command.payload.get("source_command_id")
                if not isinstance(source_id, str):
                    raise WorkflowResumeConflict("恢复命令缺少原始任务命令")
                try:
                    retry_source_command_id = UUID(source_id)
                except ValueError as exc:
                    raise WorkflowResumeConflict(
                        "恢复命令中的原始任务命令无效"
                    ) from exc
                command = await session.get(TaskCommand, retry_source_command_id)
                if command is None or command.task_id != task.id:
                    raise WorkflowResumeConflict("恢复命令引用的原始任务命令不存在")
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
        if command_copy.kind in {"INPUT", "REVIEW"}:
            async with session_factory() as session:
                wait = await session.scalar(
                    select(TaskWait).where(TaskWait.task_id == task.id, TaskWait.active)
                )
                is_current_wait = (
                    wait is not None
                    and command_copy.target_interrupt_id == wait.interrupt_id
                    and (command_copy.kind != "REVIEW" or wait.kind == "APPROVAL")
                )
                if is_current_wait:
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
        if command_copy.kind in {"INPUT", "REVIEW"} and resume_projection is not None:
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
        if retry_source_command_id is not None:
            async with session_factory() as session, session.begin():
                source_command = await session.get(
                    TaskCommand, retry_source_command_id, with_for_update=True
                )
                if source_command is None:
                    raise WorkflowResumeConflict("恢复后找不到原始任务命令")
                if source_command.state == "READY":
                    source_command.state = "APPLIED"

    return handle
