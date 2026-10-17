"""任务 API、乐观并发控制和可重连 SSE。"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import (
    BUSINESS_APPROVER,
    BUSINESS_OPERATOR,
    AuthContext,
    require_business_access,
)
from app.auth.dependencies import get_current_identity, request_id, require_csrf
from app.config import settings
from app.database import async_session_factory, get_db_session
from app.drafts.rules import precheck_candidate
from app.drafts.service import (
    DraftConflictError,
    load_draft,
    save_candidate,
)
from app.drafts.types import CandidateOrder, FieldEvidenceInput, FieldSource
from app.models import (
    AgentTask,
    AuthSession,
    Contract,
    Department,
    Draft,
    Order,
    TaskApproval,
    TaskCommand,
    TaskEvent,
    TaskEventCursor,
    TaskJob,
    TaskSession,
    TaskWait,
    User,
    UserDepartmentScope,
    UserRole,
)
from app.observability.service import append_task_event, list_task_events_after
from app.tasks.service import (
    IdempotencyConflict,
    create_task,
    enqueue_command,
)
from app.tasks.waits import bind_wait_command

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
MAX_PAGE_SIZE = 100
PATCHABLE_DRAFT_FIELDS = frozenset(
    {
        "contract_id",
        "customer_id",
        "project_id",
        "currency",
        "delivery_date",
        "address",
        "contact",
        "business_fields",
        "items",
    }
)
TASK_STATES = frozenset(
    {
        "QUEUED",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "SUBMITTING",
        "BLOCKED_RULE",
        "FAILED",
        "COMPLETED",
        "REJECTED",
    }
)
TERMINAL_STATES = frozenset({"FAILED", "COMPLETED", "REJECTED"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskInput(StrictModel):
    text: str | None = Field(default=None, min_length=1, max_length=12_000)
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=20)


class CreateTaskRequest(StrictModel):
    session_id: UUID | None = None
    order_type: Literal["SALES", "PURCHASE"]
    input: TaskInput


class TaskSupplementRequest(StrictModel):
    interrupt_id: str = Field(min_length=1, max_length=256)
    checkpoint_id: str = Field(min_length=1, max_length=256)
    expected_draft_version: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=12_000)
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=20)


class DraftPatchRequest(StrictModel):
    expected_version: int = Field(ge=1)
    interrupt_id: str = Field(min_length=1, max_length=256)
    checkpoint_id: str = Field(min_length=1, max_length=256)
    patch: dict[str, Any] = Field(min_length=1, max_length=32)


class TaskReviewRequest(StrictModel):
    decision: Literal["APPROVE", "RETURN", "REJECT"]
    draft_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1, max_length=256)
    interrupt_id: str = Field(min_length=1, max_length=256)
    reason: str | None = Field(default=None, max_length=1000)


def _api_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _idempotency_key(value: str) -> str:
    key = value.strip()
    if not key or len(key) > 128:
        raise _api_error(422, "INPUT_INVALID", "幂等键长度必须为 1 至 128 个字符")
    return key


def _visible_task_query(task_id: UUID, context: AuthContext):
    if not context.roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise _api_error(403, "ACCESS_DENIED", "当前账号无权访问任务")
    return select(AgentTask).where(
        AgentTask.id == task_id,
        AgentTask.department_id.in_(context.department_ids),
    )


async def _visible_task(
    session: AsyncSession, task_id: UUID, context: AuthContext, *, lock: bool = False
) -> AgentTask:
    query = _visible_task_query(task_id, context)
    if lock:
        query = query.with_for_update()
    task = await session.scalar(query)
    if task is None:
        raise _api_error(404, "RESOURCE_NOT_FOUND", "资源不存在")
    require_business_access(context, task.department_id)
    return task


def _require_task_operator(context: AuthContext) -> None:
    """创建、续接和编辑任务只能由经办角色执行。"""
    if BUSINESS_OPERATOR not in context.roles:
        raise _api_error(403, "ACCESS_DENIED", "当前账号无权修改任务")


def _require_task_approver(context: AuthContext) -> None:
    if BUSINESS_APPROVER not in context.roles:
        raise _api_error(403, "APPROVAL_FORBIDDEN", "当前账号没有审批权限")


def _status_url(task_id: UUID) -> str:
    return f"/api/v1/tasks/{task_id}"


def _accepted_response(
    task_id: UUID, command_id: UUID, *, state: str
) -> dict[str, object]:
    return {
        "data": {
            "task_id": str(task_id),
            "command_id": str(command_id),
            "state": state,
            "status_url": _status_url(task_id),
        }
    }


def _review_accepted_response(
    task_id: UUID, approval_id: UUID, command_id: UUID, *, state: str
) -> dict[str, object]:
    return {
        "data": {
            "task_id": str(task_id),
            "approval_id": str(approval_id),
            "command_id": str(command_id),
            "state": state,
            "status_url": _status_url(task_id),
        }
    }


def _encode_cursor(created_at: datetime, task_id: UUID) -> str:
    value = json.dumps(
        {"created_at": created_at.isoformat(), "task_id": str(task_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(data["created_at"])
        task_id = UUID(data["task_id"])
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must include timezone")
        return created_at, task_id
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise _api_error(422, "INPUT_INVALID", "任务列表游标无效") from exc


def _task_summary(
    task: AgentTask,
    *,
    draft_version: int | None,
    session_id: UUID,
    input_type: str,
) -> dict[str, object]:
    return {
        "task_id": str(task.id),
        "session_id": str(session_id),
        "order_type": task.task_type,
        "state": task.state,
        "projection_version": task.projection_version,
        "draft_version": draft_version,
        "input_type": input_type,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "status_url": _status_url(task.id),
    }


@router.post("", status_code=202)
async def create_task_endpoint(
    body: CreateTaskRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, object]:
    key = _idempotency_key(idempotency_key)
    text = body.input.text.strip() if body.input.text else ""
    if not text and not body.input.artifact_ids:
        raise _api_error(422, "INPUT_INVALID", "请提供文本或附件")
    if body.input.artifact_ids:
        raise _api_error(422, "ARTIFACT_FORBIDDEN", "当前版本暂不支持附件任务")
    user = await session.get(User, context.user_id)
    if (
        user is None
        or not user.active
        or user.department_id not in context.department_ids
    ):
        raise _api_error(403, "ACCESS_DENIED", "当前账号没有可用的默认部门")
    require_business_access(context, user.department_id)
    _require_task_operator(context)

    # 确定性服务端 ID 配合事务 advisory lock，使同一账号的创建重试不生成第二个任务。
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"task-create:{context.user_id}:{key}", 0)
            )
        )
    )
    task_id = uuid5(context.user_id, f"tradepilot/task-create/{key}")
    existing = await session.get(AgentTask, task_id)
    payload: dict[str, object] = {
        "text": text,
        "input_type": "TEXT",
        "order_type": body.order_type,
        "session_id": str(body.session_id) if body.session_id else None,
    }
    if existing is None:
        try:
            task = await create_task(
                session,
                owner_id=context.user_id,
                department_id=user.department_id,
                task_type=body.order_type,
                title=text[:120],
                task_id=task_id,
                session_id=body.session_id,
            )
        except LookupError as exc:
            raise _api_error(404, "RESOURCE_NOT_FOUND", "资源不存在") from exc
    else:
        if existing.owner_id != context.user_id:
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", "幂等键与已有任务冲突")
        if existing.task_type != body.order_type:
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同任务类型")
        task = existing

    try:
        command = await enqueue_command(
            session,
            task_id=task.id,
            actor_id=context.user_id,
            kind="START",
            payload=payload,
            request_id=key,
        )
    except IdempotencyConflict as exc:
        raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
    await session.refresh(task)
    if not command.replayed:
        await append_task_event(
            session,
            task_id=task.id,
            event_type="task.status_changed",
            payload={"state": "QUEUED", "projection_version": task.projection_version},
        )
    await session.flush()
    await session.commit()
    return {
        "data": {
            **_accepted_response(task.id, command.command_id, state=task.state)["data"],
            "session_id": str(task.session_id),
        },
        "request_id": request_id(request),
    }


@router.get("")
async def list_tasks(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    state: str | None = Query(default=None),
    input_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, object]:
    if not context.roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise _api_error(403, "ACCESS_DENIED", "当前账号无权访问任务")
    if state is not None and state not in TASK_STATES:
        raise _api_error(422, "INPUT_INVALID", "任务状态筛选值无效")
    if input_type is not None and input_type != "TEXT":
        raise _api_error(422, "INPUT_INVALID", "当前版本只支持按文本任务筛选")
    if not context.department_ids:
        return {
            "data": {"items": [], "next_cursor": None},
            "request_id": request_id(request),
        }

    query = (
        select(AgentTask, TaskSession.id, Draft.current_version)
        .join(TaskSession, TaskSession.id == AgentTask.session_id)
        .outerjoin(Draft, Draft.task_id == AgentTask.id)
        .where(AgentTask.department_id.in_(context.department_ids))
    )
    if state:
        query = query.where(AgentTask.state == state)
    if input_type:
        query = query.where(
            select(TaskCommand.id)
            .where(
                TaskCommand.task_id == AgentTask.id,
                TaskCommand.kind == "START",
                TaskCommand.payload["input_type"].as_string() == input_type,
            )
            .exists()
        )
    if cursor:
        created_at, task_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                AgentTask.created_at < created_at,
                and_(AgentTask.created_at == created_at, AgentTask.id < task_id),
            )
        )
    rows = list(
        (
            await session.execute(
                query.order_by(AgentTask.created_at.desc(), AgentTask.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        _task_summary(
            task,
            draft_version=draft_version if draft_version else None,
            session_id=session_id,
            input_type="TEXT",
        )
        for task, session_id, draft_version in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1][0].created_at, rows[-1][0].id)
        if has_more and rows
        else None
    )
    return {
        "data": {"items": items, "next_cursor": next_cursor},
        "request_id": request_id(request),
    }


@router.get("/{task_id}")
async def get_task(
    task_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    task = await _visible_task(session, task_id, context)
    draft = await session.scalar(select(Draft).where(Draft.task_id == task.id))
    snapshot = await load_draft(session, draft.id) if draft is not None else None
    wait = await session.scalar(
        select(TaskWait).where(TaskWait.task_id == task.id, TaskWait.active)
    )
    commands = await session.scalars(
        select(TaskCommand)
        .where(TaskCommand.task_id == task.id, TaskCommand.kind.in_(("START", "INPUT")))
        .order_by(TaskCommand.created_at, TaskCommand.id)
    )
    latest_approval = await session.scalar(
        select(TaskApproval)
        .where(TaskApproval.task_id == task.id)
        .order_by(TaskApproval.created_at.desc(), TaskApproval.id.desc())
        .limit(1)
    )
    inputs = [
        {
            "command_id": str(command.id),
            "role": "user",
            "text": command.payload.get("text", ""),
            "created_at": command.created_at.isoformat(),
        }
        for command in commands
        if command.payload.get("operation") != "DRAFT_PATCH"
        and isinstance(command.payload.get("text"), str)
    ]
    active_wait = None
    if wait is not None:
        active_wait = {
            "kind": wait.kind,
            "checkpoint_id": wait.checkpoint_ref,
            "interrupt_id": wait.interrupt_id,
            "draft_version": wait.draft_version,
            "content_hash": wait.payload.get("content_hash"),
            "missing_fields": wait.payload.get("missing_fields", []),
            "reason": wait.payload.get("reason"),
        }
    available_actions: list[str] = []
    if (
        task.state in {"WAITING_INPUT", "BLOCKED_RULE"}
        and wait is not None
        and wait.kind == "INPUT"
    ):
        available_actions.extend(("INPUT", "EDIT_DRAFT"))
    if (
        task.state == "WAITING_APPROVAL"
        and wait is not None
        and wait.kind == "APPROVAL"
    ):
        available_actions.extend(("APPROVE", "RETURN", "REJECT"))
    order = await session.scalar(select(Order).where(Order.task_id == task.id))
    return {
        "data": {
            "task_id": str(task.id),
            "session_id": str(task.session_id),
            "order_type": task.task_type,
            "state": task.state,
            "projection_version": task.projection_version,
            "current_step": task.current_step,
            "inputs": inputs,
            "draft": (
                {
                    "draft_id": str(snapshot.draft_id),
                    "version": snapshot.version,
                    "content_hash": snapshot.content_hash,
                    "total_amount": str(snapshot.total_amount)
                    if snapshot.total_amount is not None
                    else None,
                    "validated": snapshot.is_validated,
                    "issues": [
                        issue.model_dump(mode="json") for issue in snapshot.issues
                    ],
                    "candidate": snapshot.candidate.model_dump(mode="json")
                    if snapshot.candidate is not None
                    else None,
                }
                if snapshot is not None
                else None
            ),
            "active_wait": active_wait,
            "latest_review": (
                {
                    "decision": latest_approval.decision,
                    "actor_id": str(latest_approval.actor_id),
                    "department_id": str(latest_approval.department_id),
                    "draft_id": str(latest_approval.draft_id),
                    "version": latest_approval.version,
                    "content_hash": latest_approval.content_hash,
                    "comment": latest_approval.comment,
                    "invalidated": latest_approval.invalidated_at is not None,
                    "created_at": latest_approval.created_at.isoformat(),
                }
                if latest_approval is not None
                else None
            ),
            "order_ref": (
                {
                    "order_id": str(order.id),
                    "order_number": order.number,
                    "state": order.state,
                    "total_amount": str(order.total_amount),
                    "currency": order.currency,
                    "approval_id": str(order.approval_id),
                    "approved_version": order.approved_version,
                    "approved_content_hash": order.approved_content_hash,
                }
                if order is not None
                else None
            ),
            "last_error": task.last_error,
            "available_actions": available_actions,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        },
        "request_id": request_id(request),
    }


@router.post("/{task_id}/inputs", status_code=202)
async def submit_task_input(
    task_id: UUID,
    body: TaskSupplementRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, object]:
    key = _idempotency_key(idempotency_key)
    text = body.text.strip()
    if not text and not body.artifact_ids:
        raise _api_error(422, "INPUT_INVALID", "补充内容不能为空")
    if body.artifact_ids:
        raise _api_error(422, "ARTIFACT_FORBIDDEN", "当前版本暂不支持附件补充")
    task = await _visible_task(session, task_id, context, lock=True)
    _require_task_operator(context)
    payload: dict[str, object] = {
        "text": text,
        "checkpoint_id": body.checkpoint_id,
        "input_type": "TEXT",
    }
    existing = await session.scalar(
        select(TaskCommand).where(
            TaskCommand.task_id == task.id, TaskCommand.request_id == key
        )
    )
    if existing is not None:
        try:
            command = await enqueue_command(
                session,
                task_id=task.id,
                actor_id=context.user_id,
                kind="INPUT",
                payload=payload,
                request_id=key,
                expected_version=body.expected_draft_version,
                target_interrupt_id=body.interrupt_id,
            )
        except IdempotencyConflict as exc:
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
        return {
            **_accepted_response(task.id, command.command_id, state=task.state),
            "request_id": request_id(request),
        }

    if task.state not in {"WAITING_INPUT", "BLOCKED_RULE"}:
        raise _api_error(409, "TASK_NOT_WAITING", "任务当前不等待输入")
    wait = await session.scalar(
        select(TaskWait)
        .where(TaskWait.task_id == task.id, TaskWait.active)
        .with_for_update()
    )
    if (
        wait is None
        or wait.kind != "INPUT"
        or wait.checkpoint_ref != body.checkpoint_id
        or wait.interrupt_id != body.interrupt_id
    ):
        raise _api_error(409, "WAIT_CONFLICT", "任务等待点已变化，请刷新任务")
    if (
        body.expected_draft_version is not None
        and wait.draft_version != body.expected_draft_version
    ):
        raise _api_error(409, "VERSION_CONFLICT", "草稿版本已变化，请刷新任务")
    try:
        command = await enqueue_command(
            session,
            task_id=task.id,
            actor_id=context.user_id,
            kind="INPUT",
            payload=payload,
            request_id=key,
            expected_version=body.expected_draft_version,
            target_interrupt_id=body.interrupt_id,
        )
        await bind_wait_command(
            session,
            task_id=task.id,
            checkpoint_ref=body.checkpoint_id,
            interrupt_id=body.interrupt_id,
            command_id=command.command_id,
            expected_version=body.expected_draft_version,
        )
    except IdempotencyConflict as exc:
        raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
    except ValueError as exc:
        raise _api_error(409, "WAIT_CONFLICT", str(exc)) from exc
    await append_task_event(
        session,
        task_id=task.id,
        event_type="task.status_changed",
        payload={"state": "QUEUED", "projection_version": task.projection_version},
    )
    await session.commit()
    return {
        **_accepted_response(task.id, command.command_id, state="QUEUED"),
        "request_id": request_id(request),
    }


@router.post("/{task_id}/review", status_code=202)
async def review_task(
    task_id: UUID,
    body: TaskReviewRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, object]:
    """持久化当前版本的人工决定；只有 Worker 能恢复对应 LangGraph 中断。"""
    _require_task_approver(context)
    key = _idempotency_key(idempotency_key)
    task = await _visible_task(session, task_id, context, lock=True)
    payload: dict[str, object] = {
        "decision": body.decision,
        "comment": body.reason,
        "draft_id": "pending-validation",
        "content_hash": body.content_hash,
        "checkpoint_id": body.checkpoint_id,
        "interrupt_id": body.interrupt_id,
    }
    existing = await session.scalar(
        select(TaskCommand).where(
            TaskCommand.task_id == task.id, TaskCommand.request_id == key
        )
    )
    if existing is not None:
        if existing.kind != "REVIEW" or existing.state == "SUPERSEDED":
            raise _api_error(
                409, "IDEMPOTENCY_CONFLICT", "审批请求已失效或幂等键已用于其他操作"
            )
        if (
            any(
                existing.payload.get(field) != value
                for field, value in {
                    "decision": body.decision,
                    "comment": body.reason,
                    "content_hash": body.content_hash,
                    "checkpoint_id": body.checkpoint_id,
                    "interrupt_id": body.interrupt_id,
                }.items()
            )
            or existing.expected_version != body.draft_version
        ):
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同的审批决定")
        try:
            command = await enqueue_command(
                session,
                task_id=task.id,
                actor_id=context.user_id,
                kind="REVIEW",
                payload=existing.payload,
                request_id=key,
                expected_version=existing.expected_version,
                target_interrupt_id=existing.target_interrupt_id,
            )
        except IdempotencyConflict as exc:
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
        approval = await session.scalar(
            select(TaskApproval).where(TaskApproval.command_id == command.command_id)
        )
        if approval is None:
            raise _api_error(409, "WAIT_CONFLICT", "原审批记录无法回查")
        return {
            **_review_accepted_response(
                task.id, approval.id, command.command_id, state=task.state
            ),
            "request_id": request_id(request),
        }

    if task.state != "WAITING_APPROVAL":
        raise _api_error(409, "TASK_NOT_WAITING", "任务当前不等待审批")
    if context.user_id == task.owner_id and not (
        settings.demo_mode and settings.demo_allow_self_approval
    ):
        raise _api_error(403, "APPROVAL_FORBIDDEN", "经办人不能批准自己的草稿")
    wait = await session.scalar(
        select(TaskWait)
        .where(TaskWait.task_id == task.id, TaskWait.active)
        .with_for_update()
    )
    if (
        wait is None
        or wait.kind != "APPROVAL"
        or wait.checkpoint_ref != body.checkpoint_id
        or wait.interrupt_id != body.interrupt_id
        or wait.draft_version != body.draft_version
        or wait.payload.get("content_hash") != body.content_hash
    ):
        raise _api_error(
            409, "VERSION_CONFLICT", "审批等待点或草稿版本已变化，请刷新任务"
        )

    draft = await session.scalar(
        select(Draft).where(Draft.task_id == task.id).with_for_update()
    )
    if draft is None or str(draft.id) != wait.payload.get("draft_id"):
        raise _api_error(409, "DRAFT_NOT_FOUND", "当前审批草稿不存在")
    snapshot = await load_draft(session, draft.id)
    if (
        snapshot is None
        or snapshot.version != body.draft_version
        or snapshot.content_hash != body.content_hash
    ):
        raise _api_error(
            409, "VERSION_CONFLICT", "草稿版本或内容摘要已变化，请刷新任务"
        )
    if body.decision == "APPROVE":
        if not snapshot.is_validated or snapshot.candidate is None:
            raise _api_error(
                422, "APPROVAL_BLOCKED", "草稿存在缺失字段或校验问题，不能批准"
            )
        try:
            precheck = await precheck_candidate(
                session,
                candidate=snapshot.candidate,
                actor=context,
                business_date=datetime.now(ZoneInfo(settings.business_timezone)).date(),
            )
        except ValueError as exc:
            raise _api_error(
                422, "APPROVAL_BLOCKED", "草稿缺少合同或无法通过业务规则预检"
            ) from exc
        if precheck.issues:
            raise _api_error(
                422, "APPROVAL_BLOCKED", "草稿存在合同、物料或额度规则问题，不能批准"
            )

    payload["draft_id"] = str(draft.id)
    try:
        command = await enqueue_command(
            session,
            task_id=task.id,
            actor_id=context.user_id,
            kind="REVIEW",
            payload=payload,
            request_id=key,
            expected_version=body.draft_version,
            target_interrupt_id=body.interrupt_id,
        )
        await bind_wait_command(
            session,
            task_id=task.id,
            checkpoint_ref=body.checkpoint_id,
            interrupt_id=body.interrupt_id,
            command_id=command.command_id,
            expected_version=body.draft_version,
        )
    except IdempotencyConflict as exc:
        raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
    except ValueError as exc:
        raise _api_error(409, "WAIT_CONFLICT", str(exc)) from exc

    approval = await session.scalar(
        select(TaskApproval).where(TaskApproval.command_id == command.command_id)
    )
    if approval is None:
        approval = TaskApproval(
            id=uuid5(task.id, f"approval:{command.command_id}"),
            task_id=task.id,
            command_id=command.command_id,
            draft_id=draft.id,
            actor_id=context.user_id,
            department_id=task.department_id,
            version=body.draft_version,
            content_hash=body.content_hash,
            checkpoint_ref=body.checkpoint_id,
            interrupt_id=body.interrupt_id,
            decision=body.decision,
            comment=body.reason,
        )
        session.add(approval)
        await append_task_event(
            session,
            task_id=task.id,
            event_type="task.approval_submitted",
            payload={
                "decision": body.decision,
                "actor_id": str(context.user_id),
                "draft_id": str(draft.id),
                "version": body.draft_version,
                "content_hash": body.content_hash,
                "command_id": str(command.command_id),
            },
        )
    if approval.decision == "APPROVE" and approval.invalidated_at is None:
        draft.lock_state = "SUBMITTING"
    await session.commit()
    return {
        **_review_accepted_response(
            task.id, approval.id, command.command_id, state="QUEUED"
        ),
        "request_id": request_id(request),
    }


def _patch_candidate(current: CandidateOrder, patch: dict[str, Any]) -> CandidateOrder:
    unknown = set(patch) - PATCHABLE_DRAFT_FIELDS
    if unknown:
        names = "、".join(sorted(unknown))
        raise _api_error(422, "PATCH_FIELD_FORBIDDEN", f"不允许修改字段：{names}")
    values = current.model_dump(mode="python")
    values.update(patch)
    evidence = list(current.evidence)
    for field, value in patch.items():
        evidence.append(
            FieldEvidenceInput(
                field_path=field,
                source_type=FieldSource.USER_EDIT,
                raw_value=value,
                accepted_value=value,
            )
        )
    values["evidence"] = evidence
    try:
        return CandidateOrder.model_validate(values)
    except ValidationError as exc:
        raise _api_error(422, "INPUT_INVALID", "草稿修改字段格式无效") from exc


def _patch_command_payload(body: DraftPatchRequest) -> dict[str, object]:
    patch_text = json.dumps(
        body.patch, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "operation": "DRAFT_PATCH",
        "text": f"用户已通过草稿编辑器保存字段修改：{patch_text}。请基于已保存草稿重新查询必要业务事实并校验；不要创建正式订单。",
        "patch": body.patch,
        "expected_version": body.expected_version,
        "checkpoint_id": body.checkpoint_id,
    }


@router.patch("/{task_id}/draft")
async def patch_task_draft(
    task_id: UUID,
    body: DraftPatchRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, object]:
    key = _idempotency_key(idempotency_key)
    task = await _visible_task(session, task_id, context, lock=True)
    _require_task_operator(context)
    try:
        draft_payload = _patch_command_payload(body)
    except (TypeError, ValueError) as exc:
        raise _api_error(422, "INPUT_INVALID", "草稿修改内容无法序列化") from exc

    existing = await session.scalar(
        select(TaskCommand).where(
            TaskCommand.task_id == task.id, TaskCommand.request_id == key
        )
    )
    if existing is not None:
        if existing.state == "SUPERSEDED":
            raise _api_error(409, "VERSION_CONFLICT", "该草稿修改已被更新的操作取代")
        try:
            command = await enqueue_command(
                session,
                task_id=task.id,
                actor_id=context.user_id,
                kind="INPUT",
                payload=draft_payload,
                request_id=key,
                expected_version=existing.expected_version,
                target_interrupt_id=body.interrupt_id,
            )
        except IdempotencyConflict as exc:
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
        if existing.result_ref is not None:
            return {"data": existing.result_ref, "request_id": request_id(request)}
        raise _api_error(409, "IDEMPOTENCY_CONFLICT", "草稿修改请求结果不可回查")

    if task.state not in {"WAITING_INPUT", "BLOCKED_RULE", "QUEUED"}:
        raise _api_error(409, "TASK_NOT_WAITING", "当前任务不允许修改草稿")
    wait = await session.scalar(
        select(TaskWait)
        .where(TaskWait.task_id == task.id, TaskWait.active)
        .with_for_update()
    )
    if (
        wait is None
        or wait.checkpoint_ref != body.checkpoint_id
        or wait.interrupt_id != body.interrupt_id
    ):
        raise _api_error(409, "WAIT_CONFLICT", "任务等待点已变化，请刷新任务")
    is_input_wait = (
        task.state in {"WAITING_INPUT", "BLOCKED_RULE"} and wait.kind == "INPUT"
    )
    is_pending_review = (
        task.state == "QUEUED"
        and wait.kind == "APPROVAL"
        and wait.pending_command_id is not None
    )
    if not (is_input_wait or is_pending_review):
        raise _api_error(409, "TASK_NOT_WAITING", "当前任务不允许修改草稿")

    pending_command: TaskCommand | None = None
    if is_pending_review:
        pending_command = await session.get(
            TaskCommand, wait.pending_command_id, with_for_update=True
        )
        pending_job = await session.scalar(
            select(TaskJob)
            .where(
                TaskJob.command_id == wait.pending_command_id,
                TaskJob.kind == "RUN_TASK",
            )
            .with_for_update()
        )
        if (
            pending_command is None
            or pending_command.kind != "REVIEW"
            or pending_command.state != "READY"
            or pending_job is None
            or pending_job.state != "READY"
        ):
            raise _api_error(409, "WAIT_CONFLICT", "审批已开始处理，当前不能修改草稿")
    draft = await session.scalar(select(Draft).where(Draft.task_id == task.id))
    if draft is None:
        raise _api_error(409, "DRAFT_NOT_FOUND", "当前任务尚无可编辑草稿")
    snapshot = await load_draft(session, draft.id)
    if snapshot is None or snapshot.candidate is None:
        raise _api_error(409, "DRAFT_NOT_FOUND", "当前任务草稿版本不存在")
    if (
        snapshot.version != body.expected_version
        or wait.draft_version != body.expected_version
    ):
        raise _api_error(409, "VERSION_CONFLICT", "草稿版本已变化，请刷新任务")

    if pending_command is not None:
        pending_command.state = "SUPERSEDED"
        pending_command.result_ref = {"state": "SUPERSEDED", "reason": "draft_edited"}
        assert pending_job is not None
        pending_job.state = "DONE"
        pending_approval = await session.scalar(
            select(TaskApproval)
            .where(TaskApproval.command_id == pending_command.id)
            .with_for_update()
        )
        if pending_approval is not None and pending_approval.invalidated_at is None:
            pending_approval.invalidated_at = datetime.now(UTC)
        wait.pending_command_id = None

    candidate = _patch_candidate(snapshot.candidate, body.patch)
    if candidate.contract_id is not None:
        # 仅同步校验合同部门范围；客户、物料和额度等规则由恢复后的图异步复核。
        contract = await session.get(Contract, candidate.contract_id)
        if contract is None:
            raise _api_error(404, "RESOURCE_NOT_FOUND", "资源不存在")
        require_business_access(context, contract.department_id)
    try:
        saved = await save_candidate(
            session,
            draft_id=draft.id,
            expected_version=body.expected_version,
            candidate=candidate,
            actor=context,
        )
    except DraftConflictError as exc:
        raise _api_error(409, exc.code, str(exc)) from exc
    wait.draft_version = saved.version
    wait.payload = {
        **wait.payload,
        "draft_version": saved.version,
        "content_hash": saved.content_hash,
        "missing_fields": [
            {"field": issue.field, "message": issue.message} for issue in saved.issues
        ],
    }
    try:
        command = await enqueue_command(
            session,
            task_id=task.id,
            actor_id=context.user_id,
            kind="INPUT",
            payload=draft_payload,
            request_id=key,
            expected_version=saved.version,
            target_interrupt_id=body.interrupt_id,
        )
        await bind_wait_command(
            session,
            task_id=task.id,
            checkpoint_ref=body.checkpoint_id,
            interrupt_id=body.interrupt_id,
            command_id=command.command_id,
            expected_version=saved.version,
        )
    except IdempotencyConflict as exc:
        raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
    except ValueError as exc:
        raise _api_error(409, "WAIT_CONFLICT", str(exc)) from exc

    result_ref: dict[str, object] = {
        "task_id": str(task.id),
        "command_id": str(command.command_id),
        "draft_id": str(saved.draft_id),
        "version": saved.version,
        "content_hash": saved.content_hash,
        "validated": saved.is_validated,
        "issues": [issue.model_dump(mode="json") for issue in saved.issues],
        "state": "QUEUED",
    }
    stored_command = await session.get(TaskCommand, command.command_id)
    assert stored_command is not None
    stored_command.result_ref = result_ref
    await append_task_event(
        session,
        task_id=task.id,
        event_type="task.status_changed",
        payload={"state": "QUEUED", "projection_version": task.projection_version},
    )
    await append_task_event(
        session,
        task_id=task.id,
        event_type="draft.updated",
        payload={
            "version": saved.version,
            "content_hash": saved.content_hash,
            "superseded_command_id": str(pending_command.id)
            if pending_command
            else None,
        },
    )
    await session.commit()
    return {"data": result_ref, "request_id": request_id(request)}


def _sse(event_id: int, event_type: str, payload: dict[str, object]) -> str:
    safe_type = re.sub(r"[^A-Za-z0-9_.-]", "_", event_type) or "message"
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {safe_type}\ndata: {data}\n\n"


async def _event_stream(
    request: Request,
    *,
    task_id: UUID,
    session_hash: str,
    context: AuthContext,
    after_seq: int,
) -> AsyncIterator[str]:
    last_seq = after_seq
    last_heartbeat = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        async with async_session_factory() as session:
            auth_session = await session.get(AuthSession, session_hash)
            if (
                auth_session is None
                or auth_session.revoked_at is not None
                or auth_session.expires_at <= datetime.now(UTC)
            ):
                return
            user = await session.get(User, context.user_id)
            if user is None or not user.active:
                return
            roles = frozenset(
                (
                    await session.scalars(
                        select(UserRole.role).where(UserRole.user_id == user.id)
                    )
                ).all()
            )
            departments = frozenset(
                (
                    await session.scalars(
                        select(UserDepartmentScope.department_id)
                        .join(
                            Department,
                            Department.id == UserDepartmentScope.department_id,
                        )
                        .where(
                            UserDepartmentScope.user_id == user.id,
                            Department.active.is_(True),
                        )
                    )
                ).all()
            )
            task = await session.scalar(
                select(AgentTask)
                .join(Department, Department.id == AgentTask.department_id)
                .where(
                    AgentTask.id == task_id,
                    AgentTask.department_id.in_(departments),
                    Department.active.is_(True),
                )
            )
            if (
                task is None
                or not roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER})
                or task.department_id not in departments
            ):
                return
            cursor = await session.get(TaskEventCursor, task_id)
            latest_seq = max(0, (cursor.next_seq - 1) if cursor is not None else 0)
            oldest_seq = await session.scalar(
                select(func.min(TaskEvent.seq)).where(TaskEvent.task_id == task_id)
            )
            expired = (oldest_seq is not None and last_seq < oldest_seq - 1) or (
                oldest_seq is None and last_seq < latest_seq
            )
            ahead = last_seq > latest_seq
            events = (
                await list_task_events_after(
                    session, task_id=task_id, after_seq=last_seq, limit=100
                )
                if not expired and not ahead
                else ()
            )
            task_state = task.state
            projection_version = task.projection_version

        if expired or ahead:
            yield _sse(
                latest_seq,
                "task.resync_required",
                {
                    "task_id": str(task_id),
                    "state": task_state,
                    "projection_version": projection_version,
                    "reason": "cursor_expired" if expired else "cursor_ahead",
                },
            )
            return
        if events:
            for event in events:
                yield _sse(
                    event.seq,
                    event.event_type,
                    {
                        "task_id": str(task_id),
                        "seq": event.seq,
                        "data": event.payload,
                        "emitted_at": event.emitted_at.isoformat(),
                    },
                )
                last_seq = event.seq
            continue
        if task_state in TERMINAL_STATES:
            return
        now = time.monotonic()
        if now - last_heartbeat >= 15:
            yield ": heartbeat\n\n"
            last_heartbeat = now
        await asyncio.sleep(1)


@router.get("/{task_id}/events")
async def task_events(
    task_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    after_id: int | None = Query(default=None, ge=0),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    task = await _visible_task(session, task_id, context)
    if after_id is not None and last_event_id is not None:
        try:
            header_seq = int(last_event_id)
        except ValueError as exc:
            raise _api_error(422, "INPUT_INVALID", "Last-Event-ID 格式无效") from exc
        if header_seq != after_id:
            raise _api_error(422, "INPUT_INVALID", "事件游标参数不一致")
        initial_seq = after_id
    elif last_event_id is not None:
        try:
            initial_seq = int(last_event_id)
        except ValueError as exc:
            raise _api_error(422, "INPUT_INVALID", "Last-Event-ID 格式无效") from exc
        if initial_seq < 0:
            raise _api_error(422, "INPUT_INVALID", "Last-Event-ID 不能小于 0")
    else:
        initial_seq = after_id or 0
    session_hash = getattr(request.state, "auth_session_id_hash", "")
    # StreamingResponse 可能运行数小时，不能把初始鉴权查询占用的连接留到流结束。
    await session.close()
    return StreamingResponse(
        _event_stream(
            request,
            task_id=task.id,
            session_hash=session_hash,
            context=context,
            after_seq=initial_seq,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
