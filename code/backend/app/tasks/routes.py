"""任务 API、乐观并发控制和可重连 SSE。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import String, and_, func, or_, select
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
from app.drafts.types import (
    CandidateOrder,
    CandidateOrderItem,
    FieldEvidenceInput,
    FieldSource,
    candidate_amounts,
)
from app.mcp_server.contracts import MatchMaterialInput
from app.mcp_server.queries import match_materials
from app.models import (
    AgentTask,
    AuditEvent,
    AuthSession,
    Contract,
    ContractBalance,
    Customer,
    Department,
    Draft,
    DraftRevision,
    FieldEvidence,
    ImportBatch,
    ImportBatchItem,
    InputArtifact,
    Material,
    Order,
    Project,
    TaskApproval,
    TaskArtifact,
    TaskCommand,
    TaskEvent,
    TaskEventCursor,
    TaskJob,
    TaskSession,
    TaskWait,
    TraceSpan,
    User,
    UserDepartmentScope,
    UserRole,
)
from app.observability.service import append_task_event, list_task_events_after
from app.tasks.service import (
    IdempotencyConflict,
    command_job,
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


class FileFieldConfirmation(StrictModel):
    """用户明确采纳的解析值；身份和确认时间只由服务端记录。"""

    field_path: str = Field(min_length=1, max_length=512)
    artifact_id: UUID
    page_number: int = Field(ge=1, le=1000)
    locator: dict[str, Any] = Field(min_length=1, max_length=8)
    raw_value: Any


class DraftPatchRequest(StrictModel):
    expected_version: int = Field(ge=1)
    interrupt_id: str = Field(min_length=1, max_length=256)
    checkpoint_id: str = Field(min_length=1, max_length=256)
    patch: dict[str, Any] = Field(min_length=1, max_length=32)
    file_confirmations: list[FileFieldConfirmation] = Field(
        default_factory=list, max_length=64
    )


class MaterialMatchRequest(StrictModel):
    expected_version: int = Field(ge=1)
    item_index: int = Field(ge=0, le=99)


class MaterialDecisionRequest(StrictModel):
    expected_version: int = Field(ge=1)
    item_index: int = Field(ge=0, le=99)
    candidate_material_id: UUID
    decision: Literal["REJECT"]


class TaskReviewRequest(StrictModel):
    decision: Literal["APPROVE", "RETURN", "REJECT"]
    draft_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1, max_length=256)
    interrupt_id: str = Field(min_length=1, max_length=256)
    reason: str | None = Field(default=None, max_length=1000)


class RetryTaskRequest(StrictModel):
    failed_span_id: UUID
    expected_projection_version: int = Field(ge=1)


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


async def _editable_task_draft(
    session: AsyncSession,
    task_id: UUID,
    context: AuthContext,
    expected_version: int,
    *,
    lock: bool = False,
):
    """只允许经办人查看当前有效等待点下的物料匹配和决定候选。"""
    task = await _visible_task(session, task_id, context, lock=lock)
    _require_task_operator(context)
    if task.state not in {"WAITING_INPUT", "BLOCKED_RULE", "QUEUED"}:
        raise _api_error(409, "TASK_NOT_WAITING", "当前任务不允许确认物料")
    wait_query = select(TaskWait).where(TaskWait.task_id == task.id, TaskWait.active)
    if lock:
        wait_query = wait_query.with_for_update()
    wait = await session.scalar(wait_query)
    input_wait = (
        wait is not None
        and wait.kind == "INPUT"
        and task.state in {"WAITING_INPUT", "BLOCKED_RULE"}
    )
    pending_review = (
        wait is not None
        and wait.kind == "APPROVAL"
        and task.state == "QUEUED"
        and wait.pending_command_id is not None
    )
    if not (input_wait or pending_review):
        raise _api_error(409, "TASK_NOT_WAITING", "当前任务不允许确认物料")
    if pending_review:
        command = await session.get(TaskCommand, wait.pending_command_id)
        job = await session.scalar(
            select(TaskJob).where(
                TaskJob.command_id == wait.pending_command_id,
                TaskJob.kind == "RUN_TASK",
            )
        )
        if (
            command is None
            or command.kind != "REVIEW"
            or command.state != "READY"
            or job is None
            or job.state != "READY"
        ):
            raise _api_error(409, "WAIT_CONFLICT", "审批已开始处理，请刷新任务")
    draft_query = select(Draft).where(Draft.task_id == task.id)
    if lock:
        draft_query = draft_query.with_for_update()
    draft = await session.scalar(draft_query)
    snapshot = await load_draft(session, draft.id) if draft is not None else None
    if (
        snapshot is None
        or snapshot.candidate is None
        or wait is None
        or snapshot.version != expected_version
        or wait.draft_version != expected_version
    ):
        raise _api_error(409, "VERSION_CONFLICT", "草稿版本已变化，请刷新任务")
    return task, wait, draft, snapshot


def _material_input(item) -> MatchMaterialInput:
    if item.raw_name is None or not item.raw_name.strip():
        raise _api_error(422, "MATERIAL_NAME_REQUIRED", "请先补充物料原始名称")
    return MatchMaterialInput(
        raw_name=item.raw_name,
        specification=item.specification,
        unit=item.unit,
    )


@router.post("/{task_id}/material-matches")
async def list_task_material_matches(
    task_id: UUID,
    body: MaterialMatchRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    """通过 M2-03 的真实数据库匹配查询，供经办人明确选择物料候选。"""
    _, _, _, snapshot = await _editable_task_draft(
        session, task_id, context, body.expected_version
    )
    if body.item_index >= len(snapshot.candidate.items):
        raise _api_error(404, "RESOURCE_NOT_FOUND", "草稿明细不存在")
    result = await match_materials(
        session, _material_input(snapshot.candidate.items[body.item_index])
    )
    return {
        "data": result.model_dump(mode="json"),
        "request_id": request_id(request),
    }


@router.post("/{task_id}/material-decisions")
async def record_task_material_decision(
    task_id: UUID,
    body: MaterialDecisionRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    """拒绝动作只写追加审计；不会把任一候选应用到草稿。"""
    task, _, draft, snapshot = await _editable_task_draft(
        session, task_id, context, body.expected_version, lock=True
    )
    if body.item_index >= len(snapshot.candidate.items):
        raise _api_error(404, "RESOURCE_NOT_FOUND", "草稿明细不存在")
    item = snapshot.candidate.items[body.item_index]
    result = await match_materials(session, _material_input(item))
    candidate = next(
        (
            row
            for row in result.data.candidates
            if row.material_id == body.candidate_material_id
        ),
        None,
    )
    if candidate is None:
        raise _api_error(
            409, "MATERIAL_CANDIDATE_STALE", "该物料已不在当前候选中，请重新查询"
        )
    input_digest = hashlib.sha256(
        "\x1f".join(
            (item.raw_name or "", item.specification or "", item.unit or "")
        ).encode()
    ).hexdigest()
    session.add(
        AuditEvent(
            id=uuid4(),
            task_id=task.id,
            actor_id=context.user_id,
            action="material.candidate.rejected",
            object_ref={
                "draft_id": str(draft.id),
                "item_index": body.item_index,
                "draft_version": snapshot.version,
            },
            before_ref={"raw_name_sha256": input_digest},
            after_ref={
                "decision": body.decision,
                "candidate_material_id": str(candidate.material_id),
                "candidate_code": candidate.code,
            },
            result="SUCCEEDED",
            input_hash=input_digest,
        )
    )
    await session.commit()
    return {
        "data": {"decision": body.decision, "draft_version": snapshot.version},
        "request_id": request_id(request),
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
    if not text:
        raise _api_error(422, "INPUT_INVALID", "请先描述订单需求；附件用于补充来源证据")
    if len(set(body.input.artifact_ids)) != len(body.input.artifact_ids):
        raise _api_error(422, "INPUT_INVALID", "附件不能重复")
    user = await session.get(User, context.user_id)
    if (
        user is None
        or not user.active
        or user.department_id not in context.department_ids
    ):
        raise _api_error(403, "ACCESS_DENIED", "当前账号没有可用的默认部门")
    require_business_access(context, user.department_id)
    _require_task_operator(context)

    artifacts: list[InputArtifact] = []
    if body.input.artifact_ids:
        found_artifacts = list(
            (
                await session.scalars(
                    select(InputArtifact).where(
                        InputArtifact.id.in_(body.input.artifact_ids),
                        InputArtifact.owner_id == context.user_id,
                        InputArtifact.department_id == user.department_id,
                    )
                )
            ).all()
        )
        if len(found_artifacts) != len(body.input.artifact_ids):
            raise _api_error(404, "RESOURCE_NOT_FOUND", "附件不存在或无权用于此任务")
        by_id = {item.id: item for item in found_artifacts}
        artifacts = [by_id[artifact_id] for artifact_id in body.input.artifact_ids]
        if any(
            item.state != "STORED"
            or item.metadata_json.get("parse_intent")
            not in {"contract_pdf", "receipt_image"}
            for item in artifacts
        ):
            raise _api_error(415, "FILE_UNSUPPORTED", "任务附件仅支持 PDF 和单据图片")

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
        "input_type": "TEXT_AND_FILES" if artifacts else "TEXT",
        "order_type": body.order_type,
        "session_id": str(body.session_id) if body.session_id else None,
    }
    if artifacts:
        payload["artifact_ids"] = [str(item.id) for item in artifacts]
    if existing is None:
        try:
            task = await create_task(
                session,
                owner_id=context.user_id,
                department_id=user.department_id,
                task_type=body.order_type,
                title=(text or artifacts[0].original_name)[:120],
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

    if existing is None:
        session.add_all(
            TaskArtifact(task_id=task.id, artifact_id=item.id) for item in artifacts
        )
    else:
        linked_ids = set(
            (
                await session.scalars(
                    select(TaskArtifact.artifact_id).where(
                        TaskArtifact.task_id == task.id
                    )
                )
            ).all()
        )
        if linked_ids != set(body.input.artifact_ids):
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同附件")

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
    order_type: str | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=120),
    created_after: Annotated[datetime | None, Query()] = None,
    created_before: Annotated[datetime | None, Query()] = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, object]:
    if not context.roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise _api_error(403, "ACCESS_DENIED", "当前账号无权访问任务")
    if state is not None and state not in TASK_STATES:
        raise _api_error(422, "INPUT_INVALID", "任务状态筛选值无效")
    if input_type is not None and input_type not in {
        "TEXT",
        "TEXT_AND_FILES",
        "EXCEL_GROUP",
    }:
        raise _api_error(422, "INPUT_INVALID", "任务输入类型筛选值无效")
    if order_type is not None and order_type not in {"SALES", "PURCHASE"}:
        raise _api_error(422, "INPUT_INVALID", "订单类型筛选值无效")
    if any(
        value is not None and value.tzinfo is None
        for value in (created_after, created_before)
    ):
        raise _api_error(422, "INPUT_INVALID", "时间筛选必须包含时区")
    if created_after and created_before and created_after > created_before:
        raise _api_error(422, "INPUT_INVALID", "开始时间不能晚于结束时间")
    if not context.department_ids:
        return {
            "data": {"items": [], "next_cursor": None},
            "request_id": request_id(request),
        }

    start_input_type = (
        select(TaskCommand.payload["input_type"].as_string())
        .where(TaskCommand.task_id == AgentTask.id, TaskCommand.kind == "START")
        .order_by(TaskCommand.created_at, TaskCommand.id)
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(AgentTask, TaskSession.id, Draft.current_version, start_input_type)
        .join(TaskSession, TaskSession.id == AgentTask.session_id)
        .outerjoin(Draft, Draft.task_id == AgentTask.id)
        .where(AgentTask.department_id.in_(context.department_ids))
    )
    if state:
        query = query.where(AgentTask.state == state)
    if input_type:
        query = query.where(start_input_type == input_type)
    if order_type:
        query = query.where(AgentTask.task_type == order_type)
    if created_after:
        query = query.where(AgentTask.created_at >= created_after)
    if created_before:
        query = query.where(AgentTask.created_at <= created_before)
    if q:
        pattern = f"%{q.strip()}%"
        if pattern != "%%":
            draft_search = (
                select(DraftRevision.draft_id)
                .join(Draft, Draft.id == DraftRevision.draft_id)
                .outerjoin(Contract, Contract.id == DraftRevision.contract_id)
                .outerjoin(Customer, Customer.id == DraftRevision.customer_id)
                .outerjoin(Project, Project.id == DraftRevision.project_id)
                .where(
                    Draft.task_id == AgentTask.id,
                    DraftRevision.version == Draft.current_version,
                    or_(
                        DraftRevision.contract_id.cast(String).ilike(pattern),
                        DraftRevision.customer_id.cast(String).ilike(pattern),
                        DraftRevision.project_id.cast(String).ilike(pattern),
                        Contract.number.ilike(pattern),
                        Customer.code.ilike(pattern),
                        Customer.name.ilike(pattern),
                        Project.code.ilike(pattern),
                        Project.name.ilike(pattern),
                    ),
                    or_(
                        Contract.department_id.in_(context.department_ids),
                        Customer.department_id.in_(context.department_ids),
                        Project.department_id.in_(context.department_ids),
                    ),
                )
                .exists()
            )
            query = query.where(
                or_(
                    AgentTask.id.cast(String).ilike(pattern),
                    TaskSession.title.ilike(pattern),
                    draft_search,
                    select(TaskCommand.id)
                    .where(
                        TaskCommand.task_id == AgentTask.id,
                        TaskCommand.payload["text"].as_string().ilike(pattern),
                    )
                    .exists(),
                )
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
            input_type=task_input_type or "TEXT",
        )
        for task, session_id, draft_version, task_input_type in rows
    ]
    page_task_ids = [task.id for task, _, _, _ in rows]
    batch_links = (
        list(
            (
                await session.execute(
                    select(ImportBatchItem).where(
                        ImportBatchItem.task_id.in_(page_task_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if page_task_ids
        else []
    )
    batch_ids = {link.batch_id for link in batch_links}
    batch_by_task = {link.task_id: link for link in batch_links if link.task_id}
    task_summary_by_id = {item["task_id"]: item for item in items}
    for task_id, link in batch_by_task.items():
        summary = task_summary_by_id.get(str(task_id))
        if summary is not None:
            summary["batch_id"] = str(link.batch_id)
            summary["batch_group_id"] = link.group_id
    batches: list[dict[str, object]] = []
    if batch_ids:
        batch_records = list(
            (
                await session.execute(
                    select(ImportBatch).where(
                        ImportBatch.id.in_(batch_ids),
                        ImportBatch.department_id.in_(context.department_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        batch_items = list(
            (
                await session.execute(
                    select(ImportBatchItem)
                    .where(
                        ImportBatchItem.batch_id.in_(batch_ids),
                        or_(
                            ImportBatchItem.task_id.is_(None),
                            ImportBatchItem.task_id.in_(
                                select(AgentTask.id).where(
                                    AgentTask.department_id.in_(context.department_ids)
                                )
                            ),
                        ),
                    )
                    .order_by(ImportBatchItem.created_at, ImportBatchItem.id)
                )
            )
            .scalars()
            .all()
        )
        child_ids = [row.task_id for row in batch_items if row.task_id]
        child_tasks = (
            list(
                (
                    await session.execute(
                        select(AgentTask.id, AgentTask.state).where(
                            AgentTask.id.in_(child_ids),
                            AgentTask.department_id.in_(context.department_ids),
                        )
                    )
                ).all()
            )
            if child_ids
            else []
        )
        child_state = {str(task_id): state for task_id, state in child_tasks}
        batch_record_by_id = {record.id: record for record in batch_records}
        batches = [
            {
                "batch_id": str(batch_id),
                "created_at": batch_record_by_id[batch_id].created_at.isoformat(),
                "state": batch_record_by_id[batch_id].state,
                "groups": [
                    {
                        "group_id": row.group_id,
                        "sheet_name": row.sheet_name,
                        "source_rows": row.source_rows,
                        "preview_status": row.preview_status,
                        "stop_result": row.stop_result,
                        "task_id": str(row.task_id) if row.task_id else None,
                        "task_state": child_state.get(str(row.task_id))
                        if row.task_id
                        else None,
                    }
                    for row in batch_items
                    if row.batch_id == batch_id
                ],
            }
            for batch_id in batch_ids
            if batch_id in batch_record_by_id
        ]
    next_cursor = (
        _encode_cursor(rows[-1][0].created_at, rows[-1][0].id)
        if has_more and rows
        else None
    )
    return {
        "data": {"items": items, "next_cursor": next_cursor, "batches": batches},
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
    quota_preview: dict[str, object] | None = None
    if snapshot is not None and snapshot.candidate is not None:
        candidate = snapshot.candidate
        if candidate.contract_id is not None:
            # 经办和审批角色没有主数据管理接口权限；在任务可见范围内只读返回额度摘要。
            # 同时按任务部门过滤合同，避免候选中恶意填入跨部门合同 ID 后泄露额度。
            contract = await session.scalar(
                select(Contract).where(
                    Contract.id == candidate.contract_id,
                    Contract.department_id == task.department_id,
                )
            )
            if contract is not None:
                balance = await session.get(ContractBalance, contract.id)
                _, requested_amount = candidate_amounts(candidate)
                occupied_amount = balance.occupied_amount if balance else None
                remaining_amount = (
                    contract.total_amount - occupied_amount
                    if occupied_amount is not None
                    else None
                )
                excess_amount = (
                    max(requested_amount - remaining_amount, Decimal("0.00"))
                    if requested_amount is not None and remaining_amount is not None
                    else None
                )
                quota_preview = {
                    "contract_amount": str(contract.total_amount),
                    "occupied_amount": (
                        str(occupied_amount) if occupied_amount is not None else None
                    ),
                    "remaining_amount": (
                        str(remaining_amount) if remaining_amount is not None else None
                    ),
                    "requested_amount": (
                        str(requested_amount) if requested_amount is not None else None
                    ),
                    "excess_amount": (
                        str(excess_amount) if excess_amount is not None else None
                    ),
                    "balance_available": balance is not None,
                }
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
        task.state == "QUEUED"
        and wait is not None
        and wait.kind == "APPROVAL"
        and wait.pending_command_id is not None
        and BUSINESS_OPERATOR in context.roles
    ):
        # 只有待处理审批命令和任务作业仍可安全替换时，才向界面开放草稿编辑。
        pending_review = await session.get(TaskCommand, wait.pending_command_id)
        pending_job = await session.scalar(
            select(TaskJob).where(
                TaskJob.command_id == wait.pending_command_id,
                TaskJob.kind == "RUN_TASK",
            )
        )
        if (
            pending_review is not None
            and pending_review.kind == "REVIEW"
            and pending_review.state == "READY"
            and pending_job is not None
            and pending_job.state == "READY"
        ):
            available_actions.append("EDIT_DRAFT")
    if (
        task.state == "WAITING_APPROVAL"
        and wait is not None
        and wait.kind == "APPROVAL"
    ):
        available_actions.extend(("APPROVE", "RETURN", "REJECT"))
    order = await session.scalar(select(Order).where(Order.task_id == task.id))
    if (
        task.state == "FAILED"
        and isinstance(task.last_error, dict)
        and task.last_error.get("retryable") is True
        and isinstance(task.last_error.get("failed_span_id"), str)
    ):
        available_actions.append("RETRY")
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
            "quota_preview": quota_preview,
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


@router.get("/{task_id}/trace/{span_id}")
async def get_failed_task_span(
    task_id: UUID,
    span_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    """返回任务内一个真实失败 span 的定位元数据，不暴露模型思维链或工具载荷。"""
    await _visible_task(session, task_id, context)
    span = await session.scalar(
        select(TraceSpan).where(
            TraceSpan.id == span_id,
            TraceSpan.task_id == task_id,
            TraceSpan.status.in_(("ERROR", "INTERRUPTED")),
        )
    )
    if span is None:
        raise _api_error(404, "RESOURCE_NOT_FOUND", "失败轨迹不存在")
    failure = span.error if isinstance(span.error, dict) else {}
    return {
        "data": {
            "span_id": str(span.id),
            "trace_id": str(span.trace_id),
            "task_id": str(task_id),
            "parent_span_id": str(span.parent_span_id) if span.parent_span_id else None,
            "name": span.name,
            "kind": span.kind,
            "attempt": span.attempt,
            "execution_mode": span.execution_mode,
            "protocol": span.protocol,
            "status": span.status,
            "start_at": span.start_at.isoformat(),
            "end_at": span.end_at.isoformat() if span.end_at else None,
            "duration_ms": span.duration_ms,
            "error": {
                "code": failure.get("code"),
                "retryable": bool(failure.get("retryable")),
            },
        },
        "request_id": request_id(request),
    }


def _trace_usage(usage: object) -> dict[str, int] | None:
    """只把完整且非负的真实 usage 标为可用，缺失或损坏时保留 unavailable。"""
    if not isinstance(usage, dict):
        return None
    values = [
        usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")
    ]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        return None
    return dict(
        zip(("input_tokens", "output_tokens", "total_tokens"), values, strict=True)
    )


def _trace_code(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9_.-]{1,64}", value) is None:
        return None
    return value


def _trace_ref(value: object) -> dict[str, object] | None:
    """只返回轨迹写入器约定的结构化元数据，过滤正文、参数值和思维链。"""
    if not isinstance(value, dict):
        return None
    allowed = {
        "provider",
        "model",
        "round",
        "input_hash",
        "tool_names",
        "tool",
        "response_id",
        "attempts",
        "output_text_length",
        "tool_calls",
        "call_id",
        "name",
        "argument_bytes",
        "argument_fields",
        "argument_types",
        "result_fields",
        "result_types",
        "result_count",
        "result_summary",
        "response_fields",
        "data_fields",
        "candidates_count",
        "lines_count",
        "issue_count",
        "source_ref_count",
        "schema_version",
        "protocol",
        "checkpoint_id",
        "interrupt_id",
        "draft_version",
        "order_id",
        "order_number",
        "approval_id",
        "version",
    }

    def clean(item: object) -> object | None:
        if isinstance(item, dict):
            return {
                key: cleaned
                for key, nested in item.items()
                if key in allowed and (cleaned := clean(nested)) is not None
            }
        if isinstance(item, list):
            return [
                cleaned for nested in item if (cleaned := clean(nested)) is not None
            ][:100]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return None

    cleaned = clean(value)
    return cleaned if isinstance(cleaned, dict) else None


@router.get("/{task_id}/trace")
async def get_task_trace(
    task_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, object]:
    """分页返回任务执行节点和关联事实，不暴露模型正文、密钥或原始工具载荷。"""
    task = await _visible_task(session, task_id, context)
    query = select(TraceSpan).where(TraceSpan.task_id == task.id)
    if cursor:
        created_at, span_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                TraceSpan.start_at > created_at,
                and_(TraceSpan.start_at == created_at, TraceSpan.id > span_id),
            )
        )
    page = list(
        (
            await session.execute(
                query.order_by(TraceSpan.start_at, TraceSpan.id).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(page) > limit
    page = page[:limit]
    all_spans = list(
        (await session.execute(select(TraceSpan).where(TraceSpan.task_id == task.id)))
        .scalars()
        .all()
    )
    model_spans = [span for span in all_spans if span.kind == "MODEL"]
    observed = [_trace_usage(span.usage) for span in model_spans]
    usage_complete = bool(model_spans) and all(value is not None for value in observed)
    usage_total = (
        {
            key: sum(value[key] for value in observed if value is not None)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        if usage_complete
        else None
    )
    events = list(
        (
            await session.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.id)
                .order_by(TaskEvent.seq)
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )
    status_events = list(
        (
            await session.execute(
                select(TaskEvent)
                .where(
                    TaskEvent.task_id == task.id,
                    TaskEvent.event_type == "task.status_changed",
                )
                .order_by(TaskEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    approvals = list(
        (
            await session.execute(
                select(TaskApproval)
                .where(TaskApproval.task_id == task.id)
                .order_by(TaskApproval.created_at, TaskApproval.id)
            )
        )
        .scalars()
        .all()
    )
    checkpoints = list(
        (
            await session.execute(
                select(TaskWait)
                .where(TaskWait.task_id == task.id)
                .order_by(TaskWait.created_at, TaskWait.checkpoint_ref)
            )
        )
        .scalars()
        .all()
    )
    audit_rows = list(
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.task_id == task.id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )
    order = await session.scalar(select(Order).where(Order.task_id == task.id))
    waits: list[dict[str, object]] = []
    wait_started: datetime | None = None
    wait_state: str | None = None

    def close_wait(end_at: datetime) -> None:
        nonlocal wait_started, wait_state
        if wait_started is None:
            return
        waits.append(
            {
                "state": wait_state,
                "start_at": wait_started.isoformat(),
                "end_at": end_at.isoformat(),
                "duration_ms": max(
                    0, int((end_at - wait_started).total_seconds() * 1000)
                ),
                "active": False,
            }
        )
        wait_started, wait_state = None, None

    for event in status_events:
        state = (
            event.payload.get("state")
            if event.event_type == "task.status_changed"
            else None
        )
        if state in {"WAITING_INPUT", "WAITING_APPROVAL", "BLOCKED_RULE"}:
            if wait_started is not None and state != wait_state:
                close_wait(event.emitted_at)
            if wait_started is None:
                wait_started, wait_state = event.emitted_at, state
        elif isinstance(state, str) and wait_started is not None:
            close_wait(event.emitted_at)
    if wait_started is not None:
        now = datetime.now(UTC)
        waits.append(
            {
                "state": wait_state,
                "start_at": wait_started.isoformat(),
                "end_at": None,
                "duration_ms": max(0, int((now - wait_started).total_seconds() * 1000)),
                "active": True,
            }
        )
    event_by_span = {
        str(event.span_id): event for event in events if event.span_id is not None
    }
    spans = []
    for span in page:
        failure = span.error if isinstance(span.error, dict) else {}
        event = event_by_span.get(str(span.id))
        spans.append(
            {
                "span_id": str(span.id),
                "trace_id": str(span.trace_id),
                "parent_span_id": str(span.parent_span_id)
                if span.parent_span_id
                else None,
                "kind": span.kind,
                "name": span.name,
                "attempt": span.attempt,
                "execution_mode": span.execution_mode,
                "protocol": span.protocol,
                "status": span.status,
                "start_at": span.start_at.isoformat(),
                "end_at": span.end_at.isoformat() if span.end_at else None,
                "duration_ms": span.duration_ms,
                "input": _trace_ref(span.input_ref),
                "output": _trace_ref(span.output_ref),
                "usage": _trace_usage(span.usage),
                "usage_status": "available"
                if _trace_usage(span.usage) is not None
                else "unavailable",
                "error": {
                    "code": _trace_code(failure.get("code")),
                    "retryable": bool(failure.get("retryable")),
                },
                "event_seq": event.seq if event else None,
            }
        )
    metrics = {
        "model_calls": len(model_spans),
        "usage_available_calls": sum(value is not None for value in observed),
        "usage_coverage": (
            sum(value is not None for value in observed) / len(model_spans)
        )
        if model_spans
        else None,
        "usage_status": "available" if usage_complete else "unavailable",
        "tokens": usage_total,
        "model_duration_ms": sum(
            span.duration_ms for span in model_spans if span.duration_ms is not None
        ),
        "tool_duration_ms": sum(
            span.duration_ms
            for span in all_spans
            if span.kind == "TOOL" and span.duration_ms is not None
        ),
        "manual_waits": waits,
    }
    next_cursor = (
        _encode_cursor(page[-1].start_at, page[-1].id) if has_more and page else None
    )
    return {
        "data": {
            "task_id": str(task.id),
            "state": task.state,
            "spans": spans,
            "next_cursor": next_cursor,
            "metrics": metrics,
            "approvals": [
                {
                    "approval_id": str(item.id),
                    "actor_id": str(item.actor_id),
                    "decision": item.decision,
                    "version": item.version,
                    "content_hash": item.content_hash,
                    "invalidated": item.invalidated_at is not None,
                    "created_at": item.created_at.isoformat(),
                }
                for item in approvals
            ],
            "checkpoints": [
                {
                    "checkpoint_id": item.checkpoint_ref,
                    "interrupt_id": item.interrupt_id,
                    "kind": item.kind,
                    "draft_version": item.draft_version,
                    "active": item.active,
                    "created_at": item.created_at.isoformat(),
                }
                for item in checkpoints
            ],
            "audit_events": [
                {
                    "action": item.action,
                    "result": item.result,
                    "actor_id": str(item.actor_id) if item.actor_id else None,
                    "created_at": item.created_at.isoformat(),
                    "object_ref": {
                        key: value
                        for key, value in (item.object_ref or {}).items()
                        if key
                        in {
                            "draft_id",
                            "version",
                            "decision",
                            "order_id",
                            "approval_id",
                        }
                    },
                }
                for item in audit_rows
            ],
            "audit_events_truncated": len(audit_rows) == 1000,
            "order": (
                {
                    "order_id": str(order.id),
                    "order_number": order.number,
                    "state": order.state,
                }
                if order
                else None
            ),
            "events": [
                {
                    "seq": item.seq,
                    "type": item.event_type,
                    "span_id": str(item.span_id) if item.span_id else None,
                    "payload": {
                        key: value
                        for key, value in item.payload.items()
                        if key
                        in {
                            "state",
                            "projection_version",
                            "status",
                            "code",
                            "span_id",
                            "kind",
                            "name",
                        }
                        and (key != "code" or _trace_code(value) is not None)
                    },
                    "emitted_at": item.emitted_at.isoformat(),
                }
                for item in events
            ],
            "events_truncated": len(events) == 1000,
        },
        "request_id": request_id(request),
    }


@router.post("/{task_id}/retry", status_code=202)
async def retry_failed_task(
    task_id: UUID,
    body: RetryTaskRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, object]:
    """只重放当前失败作业的持久 checkpoint，不接受客户端指定图节点。"""
    key = _idempotency_key(idempotency_key)
    task = await _visible_task(session, task_id, context, lock=True)
    _require_task_operator(context)

    prior = await session.scalar(
        select(TaskCommand).where(
            TaskCommand.task_id == task.id, TaskCommand.request_id == key
        )
    )
    if prior is not None:
        if (
            prior.kind != "RETRY"
            or prior.expected_version != body.expected_projection_version
            or prior.payload.get("failed_span_id") != str(body.failed_span_id)
        ):
            raise _api_error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同恢复请求")
        prior_job = await command_job(session, prior)
        if prior_job is None:
            raise _api_error(409, "RETRY_UNAVAILABLE", "恢复作业记录不存在")
        return {
            "data": {
                "task_id": str(task.id),
                "command_id": str(prior.id),
                "job_id": str(prior_job.id),
                "failed_span_id": str(body.failed_span_id),
                "state": task.state,
                "replayed": True,
                "status_url": _status_url(task.id),
            },
            "request_id": request_id(request),
        }

    if task.state != "FAILED":
        raise _api_error(409, "TASK_NOT_FAILED", "当前任务没有可重试的失败")
    if task.projection_version != body.expected_projection_version:
        raise _api_error(409, "VERSION_CONFLICT", "任务状态已变化，请刷新后重试")

    last_error = task.last_error if isinstance(task.last_error, dict) else {}
    if last_error.get("failed_span_id") != str(body.failed_span_id):
        raise _api_error(409, "FAILED_SPAN_STALE", "失败位置已变化，请刷新任务详情")
    span = await session.scalar(
        select(TraceSpan).where(
            TraceSpan.id == body.failed_span_id, TraceSpan.task_id == task.id
        )
    )
    if span is None or span.status not in {"ERROR", "INTERRUPTED"}:
        raise _api_error(409, "FAILED_SPAN_UNAVAILABLE", "失败轨迹不存在或尚未结束")
    span_error = span.error if isinstance(span.error, dict) else {}
    retryable = (
        bool(last_error.get("retryable"))
        or bool(span_error.get("retryable"))
        or (
            span.status == "INTERRUPTED"
            and span_error.get("code") == "EXECUTION_INTERRUPTED"
        )
    )
    if not retryable:
        raise _api_error(
            409, "TASK_FAILURE_NOT_RETRYABLE", "该失败属于业务阻断或不可重试错误"
        )

    failed_job = await session.scalar(
        select(TaskJob)
        .join(TaskCommand, TaskCommand.id == TaskJob.command_id)
        .where(
            TaskJob.task_id == task.id,
            TaskJob.kind == "RUN_TASK",
            TaskJob.state == "FAILED",
        )
        .order_by(TaskCommand.created_at.desc(), TaskCommand.id.desc())
        .with_for_update(of=TaskJob)
        .limit(1)
    )
    if failed_job is None or failed_job.command_id is None:
        raise _api_error(409, "RETRY_UNAVAILABLE", "没有可安全恢复的失败作业")
    failed_command = await session.get(TaskCommand, failed_job.command_id)
    if failed_command is None:
        raise _api_error(409, "RETRY_UNAVAILABLE", "失败作业缺少原始命令")
    source_command_id = failed_command.payload.get("source_command_id")
    if failed_command.kind == "RETRY":
        if not isinstance(source_command_id, str):
            raise _api_error(409, "RETRY_UNAVAILABLE", "恢复记录缺少原始命令引用")
        try:
            source_command_id = UUID(source_command_id)
        except ValueError as exc:
            raise _api_error(
                409, "RETRY_UNAVAILABLE", "恢复记录中的原始命令无效"
            ) from exc
        source_command = await session.get(TaskCommand, source_command_id)
    else:
        source_command = failed_command
    if source_command is None or source_command.task_id != task.id:
        raise _api_error(409, "RETRY_UNAVAILABLE", "原始任务命令不存在")

    active_wait = await session.scalar(
        select(TaskWait)
        .where(TaskWait.task_id == task.id, TaskWait.active)
        .with_for_update()
    )
    if active_wait is not None and active_wait.pending_command_id not in (
        None,
        source_command.id,
    ):
        raise _api_error(409, "WAIT_CONFLICT", "任务已有其他等待命令，不能安全重试")

    source_command.state = "READY"
    payload = {
        "failed_span_id": str(body.failed_span_id),
        "source_command_id": str(source_command.id),
    }
    try:
        enqueued = await enqueue_command(
            session,
            task_id=task.id,
            actor_id=context.user_id,
            kind="RETRY",
            payload=payload,
            request_id=key,
            expected_version=body.expected_projection_version,
            target_interrupt_id=source_command.target_interrupt_id,
            reuse_job_id=failed_job.id,
        )
    except IdempotencyConflict as exc:
        raise _api_error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
    except ValueError as exc:
        raise _api_error(409, "RETRY_UNAVAILABLE", str(exc)) from exc
    await session.refresh(task)
    if not enqueued.replayed:
        await append_task_event(
            session,
            task_id=task.id,
            event_type="task.retry_queued",
            payload={
                "state": "QUEUED",
                "projection_version": task.projection_version,
                "failed_span_id": body.failed_span_id,
                "source_command_id": source_command.id,
                "job_id": enqueued.job_id,
            },
        )
    await session.commit()
    return {
        "data": {
            "task_id": str(task.id),
            "command_id": str(enqueued.command_id),
            "job_id": str(enqueued.job_id),
            "failed_span_id": str(body.failed_span_id),
            "state": task.state,
            "replayed": enqueued.replayed,
            "status_url": _status_url(task.id),
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


def _file_confirmation_value(candidate: CandidateOrder, field_path: str) -> object:
    """只返回证据面板允许映射至草稿的字段。"""
    if field_path == "delivery_date":
        return candidate.delivery_date.isoformat() if candidate.delivery_date else None
    if field_path in {"address", "contact"}:
        return getattr(candidate, field_path)
    match = re.fullmatch(
        r"items\[(\d+)]\.(raw_name|specification|quantity|unit|unit_price)",
        field_path,
    )
    if match:
        index = int(match.group(1))
        if index >= len(candidate.items):
            return None
        value = getattr(candidate.items[index], match.group(2))
        return str(value) if isinstance(value, Decimal) else value
    raise _api_error(422, "EVIDENCE_FIELD_FORBIDDEN", "该识别字段不能直接写入草稿")


async def _append_file_confirmation_evidence(
    session: AsyncSession,
    task: AgentTask,
    candidate: CandidateOrder,
    confirmations: list[FileFieldConfirmation],
) -> CandidateOrder:
    if not confirmations:
        return candidate
    paths = [item.field_path for item in confirmations]
    artifact_ids = {item.artifact_id for item in confirmations}
    if len(set(paths)) != len(paths):
        raise _api_error(422, "EVIDENCE_CONFLICT", "同一字段只能确认一个来源候选")
    artifacts = list(
        (
            await session.scalars(
                select(InputArtifact)
                .join(TaskArtifact, TaskArtifact.artifact_id == InputArtifact.id)
                .where(
                    TaskArtifact.task_id == task.id,
                    InputArtifact.id.in_(artifact_ids),
                    InputArtifact.department_id == task.department_id,
                )
            )
        ).all()
    )
    by_id = {item.id: item for item in artifacts}
    if len(by_id) != len(artifact_ids):
        raise _api_error(404, "RESOURCE_NOT_FOUND", "来源附件不存在或未关联当前任务")

    evidence = list(candidate.evidence)
    for confirmation in confirmations:
        artifact = by_id[confirmation.artifact_id]
        if artifact.state != "STORED" or artifact.metadata_json.get(
            "parse_intent"
        ) not in {"contract_pdf", "receipt_image"}:
            raise _api_error(415, "FILE_UNSUPPORTED", "该附件不能作为字段来源")
        locator = confirmation.locator
        locator_type = locator.get("type")
        if locator_type == "text_range":
            valid_locator = (
                type(locator.get("start")) is int
                and type(locator.get("end")) is int
                and locator["start"] >= 0
                and locator["end"] > locator["start"]
            )
        elif locator_type == "bbox":
            valid_locator = (
                all(
                    type(locator.get(key)) is int
                    for key in ("x", "y", "width", "height")
                )
                and locator["x"] >= 0
                and locator["y"] >= 0
                and locator["width"] > 0
                and locator["height"] > 0
            )
        else:
            valid_locator = False
        if not valid_locator:
            raise _api_error(422, "EVIDENCE_LOCATOR_INVALID", "来源页内定位信息无效")
        if (
            not isinstance(confirmation.raw_value, str)
            or not confirmation.raw_value.strip()
            or len(confirmation.raw_value) > 4000
        ):
            raise _api_error(422, "EVIDENCE_VALUE_INVALID", "识别原值格式无效")
        accepted_value = _file_confirmation_value(candidate, confirmation.field_path)
        if accepted_value is None:
            raise _api_error(
                422, "EVIDENCE_VALUE_INVALID", "请先将已确认值填入对应草稿字段"
            )
        evidence.append(
            FieldEvidenceInput(
                field_path=confirmation.field_path,
                source_type=FieldSource.USER_EDIT,
                artifact_id=artifact.id,
                locator={**locator, "page_number": confirmation.page_number},
                raw_value=confirmation.raw_value,
                accepted_value=accepted_value,
            )
        )
    return candidate.model_copy(update={"evidence": tuple(evidence)})


async def _validate_material_adoptions(
    session: AsyncSession,
    current: CandidateOrder,
    updated: CandidateOrder,
) -> list[tuple[int, CandidateOrderItem, Material]]:
    """只接受当前真实匹配结果中的有效物料，并使用主数据规范规格和单位。"""
    adoptions: list[tuple[int, CandidateOrderItem, Material]] = []
    for index, item in enumerate(updated.items):
        previous = current.items[index] if index < len(current.items) else None
        if item.material_id is None or (
            previous
            and previous.material_id == item.material_id
            and previous.raw_name == item.raw_name
            and previous.specification == item.specification
            and previous.unit == item.unit
        ):
            continue
        result = await match_materials(session, _material_input(item))
        matched = next(
            (
                row
                for row in result.data.candidates
                if row.material_id == item.material_id
            ),
            None,
        )
        if matched is None:
            raise _api_error(
                422,
                "MATERIAL_CANDIDATE_INVALID",
                "所选物料不属于当前名称、规格和单位的匹配候选",
            )
        material = await session.scalar(
            select(Material).where(Material.id == item.material_id).with_for_update()
        )
        if (
            material is None
            or not material.active
            or item.specification != material.specification
            or item.unit != material.unit
        ):
            raise _api_error(
                409, "MATERIAL_CANDIDATE_STALE", "物料状态或规范规格已变化，请重新匹配"
            )
        adoptions.append((index, previous, material))
    return adoptions


def _append_material_adoption_evidence(
    candidate: CandidateOrder,
    adoptions: list[tuple[int, CandidateOrderItem, Material]],
) -> CandidateOrder:
    if not adoptions:
        return candidate
    evidence = list(candidate.evidence)
    for index, previous, material in adoptions:
        expected_paths = {f"items[{index}].raw_name", f"items.{index}.raw_name"}
        source = next(
            (
                row
                for row in reversed(candidate.evidence)
                if row.artifact_id is not None
                and row.field_path in expected_paths
                and row.raw_value == previous.raw_name
            ),
            None,
        )
        evidence.append(
            FieldEvidenceInput(
                field_path=f"items[{index}].material_id",
                source_type=FieldSource.USER_EDIT,
                artifact_id=source.artifact_id if source else None,
                message_id=source.message_id if source else None,
                locator=source.locator if source else None,
                raw_value={
                    "raw_name": previous.raw_name,
                    "specification": previous.specification,
                },
                accepted_value={
                    "material_id": str(material.id),
                    "code": material.code,
                    "name": material.name,
                    "specification": material.specification,
                    "unit": material.unit,
                },
            )
        )
    return candidate.model_copy(update={"evidence": tuple(evidence)})


def _patch_command_payload(body: DraftPatchRequest) -> dict[str, object]:
    patch_text = json.dumps(
        body.patch, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "operation": "DRAFT_PATCH",
        "text": f"用户已通过草稿编辑器保存字段修改：{patch_text}。请基于已保存草稿重新查询必要业务事实并校验；不要创建正式订单。",
        "patch": body.patch,
        "file_confirmations": [
            item.model_dump(mode="json") for item in body.file_confirmations
        ],
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
    candidate = await _append_file_confirmation_evidence(
        session, task, candidate, body.file_confirmations
    )
    material_adoptions = await _validate_material_adoptions(
        session, snapshot.candidate, candidate
    )
    candidate = _append_material_adoption_evidence(candidate, material_adoptions)
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
    for confirmation in body.file_confirmations:
        accepted_value = _file_confirmation_value(candidate, confirmation.field_path)
        if not saved.created:
            session.add(
                FieldEvidence(
                    id=uuid4(),
                    draft_id=draft.id,
                    version=saved.version,
                    field_path=confirmation.field_path,
                    source_type=FieldSource.USER_EDIT.value,
                    artifact_id=confirmation.artifact_id,
                    locator={
                        **confirmation.locator,
                        "page_number": confirmation.page_number,
                    },
                    raw_value=confirmation.raw_value,
                    accepted_value=accepted_value,
                    confirmed_by=context.user_id,
                )
            )
        session.add(
            AuditEvent(
                id=uuid4(),
                task_id=task.id,
                actor_id=context.user_id,
                action="draft.file_field_confirmed",
                object_ref={
                    "draft_id": str(draft.id),
                    "version": saved.version,
                    "artifact_id": str(confirmation.artifact_id),
                    "field_path": confirmation.field_path,
                    "page_number": confirmation.page_number,
                },
                result="SUCCEEDED",
            )
        )
    for item_index, previous, material in material_adoptions:
        input_digest = hashlib.sha256(
            "\x1f".join(
                (
                    previous.raw_name or "",
                    previous.specification or "",
                    previous.unit or "",
                )
            ).encode()
        ).hexdigest()
        session.add(
            AuditEvent(
                id=uuid4(),
                task_id=task.id,
                actor_id=context.user_id,
                action="material.candidate.accepted",
                object_ref={
                    "draft_id": str(draft.id),
                    "item_index": item_index,
                    "draft_version": saved.version,
                },
                before_ref={"raw_name_sha256": input_digest},
                after_ref={
                    "material_id": str(material.id),
                    "code": material.code,
                    "name": material.name,
                    "specification": material.specification,
                    "unit": material.unit,
                },
                result="SUCCEEDED",
                input_hash=input_digest,
            )
        )
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
