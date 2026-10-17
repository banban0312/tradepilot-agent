"""按上传者或明确授权任务读取附件元数据和原始内容。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import (
    BUSINESS_APPROVER,
    BUSINESS_OPERATOR,
    AuthContext,
    require_business_access,
    require_role,
)
from app.auth.dependencies import get_current_identity, request_id, require_csrf
from app.config import settings
from app.database import async_session_factory, get_db_session
from app.models import AgentTask, AuditEvent, InputArtifact, TaskArtifact, User
from app.observability.service import append_task_event
from app.parsing.pdf import PdfParseError, parse_pdf, sha256_file
from app.storage.files import (
    FileUploadError,
    content_disposition,
    limits_for_intent,
    storage_path,
    store_upload,
)

router = APIRouter(prefix="/api/v1", tags=["artifacts"])


def _api_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _artifact_data(artifact: InputArtifact) -> dict[str, object]:
    return {
        "artifact_id": str(artifact.id),
        "original_name": artifact.original_name,
        "media_type": artifact.media_type,
        "sha256": artifact.sha256,
        "byte_size": artifact.byte_size,
        "state": artifact.state,
        "metadata": artifact.metadata_json,
        "limits": limits_for_intent(artifact.metadata_json["parse_intent"]),
        "created_at": artifact.created_at.isoformat(),
    }


async def _remove_unreferenced_upload(storage_key: str) -> None:
    """数据库提交失败时，仅删除仍无元数据引用的对象。"""
    try:
        async with async_session_factory() as verification:
            artifact_id = await verification.scalar(
                select(InputArtifact.id).where(InputArtifact.storage_key == storage_key)
            )
        if artifact_id is None:
            path = storage_path(settings.attachments_root, storage_key)
            if not path.is_symlink():
                await anyio.to_thread.run_sync(partial(path.unlink, missing_ok=True))
    except (SQLAlchemyError, OSError, FileUploadError):
        # 数据库状态不可确认时保留对象，后续安全清理不会误删有效附件。
        return


async def _readable_artifact(
    session: AsyncSession, artifact_id: UUID, context: AuthContext
) -> InputArtifact:
    if not context.roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise _api_error(404, "RESOURCE_NOT_FOUND", "附件不存在")
    artifact = await session.get(InputArtifact, artifact_id)
    if artifact is None or artifact.department_id not in context.department_ids:
        raise _api_error(404, "RESOURCE_NOT_FOUND", "附件不存在")
    if artifact.owner_id == context.user_id:
        return artifact
    linked = await session.scalar(
        select(TaskArtifact.task_id)
        .join(AgentTask, AgentTask.id == TaskArtifact.task_id)
        .where(
            TaskArtifact.artifact_id == artifact.id,
            AgentTask.department_id == artifact.department_id,
            AgentTask.department_id.in_(context.department_ids),
        )
        .limit(1)
    )
    if linked is None:
        raise _api_error(404, "RESOURCE_NOT_FOUND", "附件不存在")
    return artifact


@router.post("/artifacts", status_code=201)
async def upload_artifact(
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    file: Annotated[UploadFile, File()],
    parse_intent: Annotated[
        Literal["contract_pdf", "receipt_image", "order_xlsx"], Form()
    ],
    task_id: Annotated[UUID | None, Form()] = None,
) -> dict[str, object]:
    require_role(context, BUSINESS_OPERATOR)
    user = await session.get(User, context.user_id)
    if user is None or user.department_id not in context.department_ids:
        raise _api_error(403, "ACCESS_DENIED", "当前账号没有可用的默认部门")
    department_id = user.department_id
    require_business_access(context, department_id)

    task: AgentTask | None = None
    if task_id is not None:
        task = await session.scalar(
            select(AgentTask)
            .where(
                AgentTask.id == task_id,
                AgentTask.department_id.in_(context.department_ids),
            )
            .with_for_update()
        )
        if task is None or task.department_id != department_id:
            raise _api_error(404, "RESOURCE_NOT_FOUND", "任务不存在")
        if task.state not in {"QUEUED", "WAITING_INPUT", "BLOCKED_RULE"}:
            raise _api_error(409, "TASK_NOT_WAITING", "当前任务不接受附件")

    try:
        stored = await store_upload(
            file,
            root=settings.attachments_root,
            intent=parse_intent,
        )
    except FileUploadError as exc:
        raise _api_error(exc.status_code, exc.code, exc.message) from exc
    finally:
        await file.close()

    artifact = InputArtifact(
        id=uuid4(),
        owner_id=context.user_id,
        department_id=department_id,
        media_type=stored.media_type,
        original_name=stored.original_name,
        storage_key=stored.storage_key,
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        state="STORED",
        metadata_json=stored.metadata,
    )
    try:
        session.add(artifact)
        if task is not None:
            session.add(TaskArtifact(task_id=task.id, artifact_id=artifact.id))
            await append_task_event(
                session,
                task_id=task.id,
                event_type="task.artifact_stored",
                payload={
                    "artifact_id": artifact.id,
                    "sha256": artifact.sha256,
                    "media_type": artifact.media_type,
                    "state": artifact.state,
                },
            )
        session.add(
            AuditEvent(
                id=uuid4(),
                task_id=task.id if task else None,
                actor_id=context.user_id,
                action="artifact.uploaded",
                object_ref={
                    "artifact_id": str(artifact.id),
                    "media_type": artifact.media_type,
                },
                after_ref={"state": artifact.state, "byte_size": artifact.byte_size},
                result="SUCCESS",
                input_hash=artifact.sha256,
            )
        )
        await session.commit()
        await session.refresh(artifact)
    except Exception:
        await session.rollback()
        await _remove_unreferenced_upload(stored.storage_key)
        raise
    return {"data": _artifact_data(artifact), "request_id": request_id(request)}


@router.get("/artifacts/{artifact_id}")
async def get_artifact_metadata(
    artifact_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    artifact = await _readable_artifact(session, artifact_id, context)
    return {"data": _artifact_data(artifact)}


@router.post("/artifacts/{artifact_id}/parse")
async def parse_artifact_pdf(
    artifact_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> JSONResponse:
    """解析需经附件鉴权；内容仅作为原始证据返回，不触发 Agent 或业务动作。"""
    artifact = await _readable_artifact(session, artifact_id, context)
    if artifact.metadata_json.get("parse_intent") != "contract_pdf":
        raise _api_error(415, "FILE_UNSUPPORTED", "当前解析接口仅支持合同 PDF")
    if artifact.state != "STORED":
        raise _api_error(404, "RESOURCE_NOT_FOUND", "附件内容不存在")
    path = storage_path(settings.attachments_root, artifact.storage_key)
    if path.is_symlink() or not path.is_file():
        raise _api_error(503, "FILE_UNREADABLE", "附件存储暂不可用")
    try:
        digest = await anyio.to_thread.run_sync(partial(sha256_file, path))
        if digest != artifact.sha256:
            raise _api_error(503, "FILE_INTEGRITY_FAILED", "附件内容校验失败")
        result = await anyio.to_thread.run_sync(partial(parse_pdf, path, digest))
    except PdfParseError as exc:
        raise _api_error(exc.status_code, exc.code, exc.message) from exc
    session.add(
        AuditEvent(
            id=uuid4(),
            task_id=None,
            actor_id=context.user_id,
            action="artifact.parsed",
            object_ref={
                "artifact_id": str(artifact.id),
                "media_type": artifact.media_type,
            },
            after_ref={"state": result["state"], "page_count": result["page_count"]},
            result="SUCCESS" if result["state"] == "COMPLETE" else "PARTIAL",
            input_hash=artifact.sha256,
        )
    )
    await session.commit()
    return JSONResponse(
        {"data": result, "request_id": request_id(request)},
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _stream_file(path: Path) -> AsyncIterator[bytes]:
    async with await anyio.open_file(path, "rb") as source:
        while chunk := await source.read(64 * 1024):
            yield chunk


@router.get("/artifacts/{artifact_id}/content")
async def get_artifact_content(
    artifact_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> StreamingResponse:
    artifact = await _readable_artifact(session, artifact_id, context)
    if artifact.state != "STORED":
        raise _api_error(404, "RESOURCE_NOT_FOUND", "附件内容不存在")
    path = storage_path(settings.attachments_root, artifact.storage_key)
    if path.is_symlink() or not path.is_file():
        raise _api_error(503, "FILE_UNREADABLE", "附件存储暂不可用")
    return StreamingResponse(
        _stream_file(path),
        media_type=artifact.media_type,
        headers={
            "Content-Length": str(artifact.byte_size),
            "Content-Disposition": content_disposition(artifact.original_name),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/tasks/{task_id}/artifacts")
async def list_task_artifacts(
    task_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    if not context.roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise _api_error(404, "RESOURCE_NOT_FOUND", "任务不存在")
    task = await session.scalar(
        select(AgentTask).where(
            AgentTask.id == task_id,
            AgentTask.department_id.in_(context.department_ids),
        )
    )
    if task is None:
        raise _api_error(404, "RESOURCE_NOT_FOUND", "任务不存在")
    require_business_access(context, task.department_id)
    artifacts = list(
        (
            await session.scalars(
                select(InputArtifact)
                .join(TaskArtifact, TaskArtifact.artifact_id == InputArtifact.id)
                .where(TaskArtifact.task_id == task.id)
                .order_by(InputArtifact.created_at, InputArtifact.id)
            )
        ).all()
    )
    return {
        "data": {"items": [_artifact_data(artifact) for artifact in artifacts]},
    }
