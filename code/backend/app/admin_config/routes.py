"""管理员运行配置版本及受限操作审计查询。"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import Text, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import SYSTEM_ADMIN, AuthContext
from app.auth.dependencies import get_current_identity, require_csrf
from app.config import settings
from app.database import get_db_session
from app.models import AuditEvent, ConfigSnapshot, Department, RuntimeConfigSecret, User
from app.observability.service import record_audit_event
from app.runtime_config import (
    CONFIG_FIELDS,
    content_hash,
    current_snapshot,
    encrypt_secret,
    environment_config,
    new_snapshot,
    secret_storage_ready,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin-runtime-config"])


class RuntimeConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    model_provider: Literal["openai", "deepseek"] | None = None
    model_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    model_base_url: str | None = Field(default=None, max_length=512)
    model_timeout_seconds: float = Field(gt=0, le=300)
    model_max_retries: int = Field(ge=0, le=5)
    model_max_rounds: int = Field(ge=1, le=50)
    model_max_tool_calls: int = Field(ge=1, le=100)
    max_concurrency: int = Field(ge=1, le=5)
    ocr_provider: Literal["local_tesseract", "openai", "deepseek"] | None = None
    mcp_url: str = Field(min_length=8, max_length=512)
    business_timezone: str = Field(min_length=1, max_length=64)
    trace_retention_days: int = Field(ge=1, le=3650)
    artifact_retention_days: int = Field(ge=1, le=3650)
    approval_mode: Literal["human_required", "demo_self_approval"] = "human_required"
    model_api_key: SecretStr | None = None
    ocr_api_key: SecretStr | None = None

    @field_validator("mcp_url", "model_base_url")
    @classmethod
    def validate_http_url(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "服务地址必须为不含凭据、查询参数或片段的 HTTP(S) 地址"
                )
        return value

    @field_validator("model_api_key", "ocr_api_key")
    @classmethod
    def validate_secret_size(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) > 4096:
            raise ValueError("凭据长度不能超过 4096 个字符")
        return value


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _admin(context: AuthContext) -> None:
    if SYSTEM_ADMIN not in context.roles:
        raise _fail(403, "ACCESS_DENIED", "当前账号没有运行配置或审计查询权限")


def _cursor_encode(created_at: datetime, event_id: UUID) -> str:
    payload = json.dumps([created_at.isoformat(), str(event_id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _cursor_decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created, event_id = json.loads(raw)
        parsed = datetime.fromisoformat(created)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, UUID(event_id)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise _fail(422, "INPUT_INVALID", "审计游标格式无效") from exc


@router.get("/config")
async def read_runtime_config(
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _admin(context)
    snapshot = await current_snapshot(session)
    content = snapshot.content if snapshot else environment_config()
    actor = await session.get(User, snapshot.created_by) if snapshot else None
    configured_secrets = (
        {
            row.name: row
            for row in (
                await session.scalars(
                    select(RuntimeConfigSecret).where(
                        RuntimeConfigSecret.snapshot_id == snapshot.id
                    )
                )
            )
        }
        if snapshot
        else {}
    )
    return {
        "data": {
            "version": snapshot.version if snapshot else 0,
            "content_hash": snapshot.content_hash
            if snapshot
            else content_hash(content),
            "content": content,
            "model_api_key_configured": (
                not configured_secrets["model_api_key"].cleared
                and configured_secrets["model_api_key"].ciphertext is not None
            )
            if "model_api_key" in configured_secrets
            else bool(
                settings.model_api_key
                and settings.model_api_key.get_secret_value().strip()
            ),
            "ocr_api_key_configured": (
                not configured_secrets["ocr_api_key"].cleared
                and configured_secrets["ocr_api_key"].ciphertext is not None
            )
            if "ocr_api_key" in configured_secrets
            else bool(
                settings.ocr_api_key and settings.ocr_api_key.get_secret_value().strip()
            ),
            "secret_storage_available": secret_storage_ready(),
            "created_by": actor.login if actor else "环境配置",
            "created_at": snapshot.created_at.isoformat() if snapshot else None,
        }
    }


@router.put("/config")
async def update_runtime_config(
    body: RuntimeConfigUpdate,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _admin(context)
    async with session.begin_nested():
        await session.execute(text("SELECT pg_advisory_xact_lock(74192832)"))
        old = await session.scalar(
            select(ConfigSnapshot)
            .where(ConfigSnapshot.is_current.is_(True))
            .with_for_update()
        )
        current_version = old.version if old else 0
        if body.expected_version != current_version:
            raise _fail(
                409, "VERSION_CONFLICT", "运行配置已被其他管理员更新，请刷新后重试"
            )
        if body.approval_mode == "demo_self_approval" and not settings.demo_mode:
            raise _fail(
                422, "INPUT_INVALID", "只有显式启用演示模式后才能选择演示审批模式"
            )
        submitted = body.model_dump(
            exclude={"expected_version", "model_api_key", "ocr_api_key"}
        )
        secret_names = ("model_api_key", "ocr_api_key")
        secret_updates = [
            name for name in secret_names if name in body.model_fields_set
        ]
        # 字段白名单阻止凭据进入配置 JSON、输入摘要和普通审计字段。
        runtime_fields = {
            key: submitted[key]
            for key in CONFIG_FIELDS
            if key in submitted and key != "approval_mode"
        }
        try:
            from app.config import Settings

            Settings.model_validate({**settings.model_dump(), **runtime_fields})
        except Exception as exc:
            raise _fail(422, "INPUT_INVALID", "运行配置字段或相互约束校验失败") from exc
        if old:
            old.is_current = False
            await session.flush()
        snapshot = new_snapshot(
            content=submitted, version=current_version + 1, actor_id=context.user_id
        )
        session.add(snapshot)
        previous_secrets = (
            {
                row.name: row
                for row in (
                    await session.scalars(
                        select(RuntimeConfigSecret).where(
                            RuntimeConfigSecret.snapshot_id == old.id
                        )
                    )
                )
            }
            if old
            else {}
        )
        for name in secret_names:
            if name not in secret_updates:
                previous = previous_secrets.get(name)
                if previous is not None:
                    session.add(
                        RuntimeConfigSecret(
                            id=uuid4(),
                            snapshot_id=snapshot.id,
                            name=name,
                            ciphertext=previous.ciphertext,
                            cleared=previous.cleared,
                        )
                    )
                continue
            secret = getattr(body, name)
            raw = secret.get_secret_value().strip() if secret else ""
            try:
                ciphertext = encrypt_secret(raw) if raw else None
            except RuntimeError as exc:
                raise _fail(
                    503,
                    "SECRET_STORAGE_UNAVAILABLE",
                    "尚未配置有效的运行密钥加密密钥，不能保存凭据",
                ) from exc
            session.add(
                RuntimeConfigSecret(
                    id=uuid4(),
                    snapshot_id=snapshot.id,
                    name=name,
                    ciphertext=ciphertext,
                    cleared=not bool(raw),
                )
            )
        await record_audit_event(
            session,
            action="admin.runtime_config.updated",
            result="SUCCESS",
            actor=context,
            object_ref={
                "version": snapshot.version,
                "config_snapshot_id": str(snapshot.id),
            },
            before_ref={"version": current_version},
            after_ref={
                "version": snapshot.version,
                "content_hash": snapshot.content_hash,
                "secret_fields_updated": secret_updates,
            },
            input_value=submitted,
        )
    await session.commit()
    return {
        "data": {
            "version": snapshot.version,
            "content_hash": snapshot.content_hash,
            "content": snapshot.content,
        }
    }


@router.get("/config/versions")
async def list_runtime_config_versions(
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, object]:
    _admin(context)
    rows = await session.scalars(
        select(ConfigSnapshot).order_by(ConfigSnapshot.version.desc()).limit(limit)
    )
    snapshots = list(rows)
    users = (
        {
            user.id: user.login
            for user in await session.scalars(
                select(User).where(User.id.in_([row.created_by for row in snapshots]))
            )
        }
        if snapshots
        else {}
    )
    return {
        "data": [
            {
                "version": row.version,
                "content": row.content,
                "content_hash": row.content_hash,
                "created_by": (
                    users.get(row.created_by, "") if row.created_by else "环境配置"
                ),
                "created_at": row.created_at.isoformat(),
                "is_current": row.is_current,
            }
            for row in snapshots
        ]
    }


@router.get("/audit")
async def list_admin_audit(
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    task_id: UUID | None = None,
    actor_id: UUID | None = None,
    department_id: UUID | None = None,
    action: str | None = Query(default=None, max_length=128),
    input_hash: str | None = Query(
        default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    ),
    reference: str | None = Query(default=None, min_length=1, max_length=128),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    _admin(context)
    statement = select(AuditEvent).order_by(
        AuditEvent.created_at.desc(), AuditEvent.id.desc()
    )
    if task_id is not None:
        statement = statement.where(AuditEvent.task_id == task_id)
    if actor_id is not None:
        statement = statement.where(AuditEvent.actor_id == actor_id)
    if department_id is not None:
        statement = statement.join(User, AuditEvent.actor_id == User.id).where(
            User.department_id == department_id
        )
    if action is not None:
        statement = statement.where(AuditEvent.action == action)
    if input_hash is not None:
        statement = statement.where(AuditEvent.input_hash == input_hash)
    if reference is not None:
        pattern = f"%{reference}%"
        statement = statement.where(
            AuditEvent.object_ref.cast(Text).ilike(pattern)
            | AuditEvent.before_ref.cast(Text).ilike(pattern)
            | AuditEvent.after_ref.cast(Text).ilike(pattern)
        )
    if created_after is not None:
        statement = statement.where(AuditEvent.created_at >= created_after)
    if created_before is not None:
        statement = statement.where(AuditEvent.created_at <= created_before)
    if created_after is not None and created_after.tzinfo is None:
        raise _fail(422, "INPUT_INVALID", "开始时间必须包含时区")
    if created_before is not None and created_before.tzinfo is None:
        raise _fail(422, "INPUT_INVALID", "结束时间必须包含时区")
    if (
        created_after is not None
        and created_before is not None
        and created_after > created_before
    ):
        raise _fail(422, "INPUT_INVALID", "开始时间不能晚于结束时间")
    if cursor:
        created, event_id = _cursor_decode(cursor)
        statement = statement.where(
            tuple_(AuditEvent.created_at, AuditEvent.id) < tuple_(created, event_id)
        )
    events = list((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(events) > limit
    events = events[:limit]
    actor_ids = {event.actor_id for event in events if event.actor_id}
    actor_details = (
        (
            await session.execute(
                select(User.id, User.login, Department.name)
                .join(Department, Department.id == User.department_id)
                .where(User.id.in_(actor_ids))
            )
        ).all()
        if actor_ids
        else []
    )
    actors = {
        user_id: (login, department) for user_id, login, department in actor_details
    }
    next_cursor = (
        _cursor_encode(events[-1].created_at, events[-1].id)
        if has_more and events
        else None
    )
    return {
        "data": {
            "items": [
                {
                    "id": str(event.id),
                    "task_id": str(event.task_id) if event.task_id else None,
                    "actor_id": str(event.actor_id) if event.actor_id else None,
                    "actor": actors.get(event.actor_id, ("系统", ""))[0]
                    if event.actor_id
                    else "系统",
                    "department": actors.get(event.actor_id, ("", ""))[1]
                    if event.actor_id
                    else "系统",
                    "action": event.action,
                    "result": event.result,
                    "object_ref": event.object_ref,
                    "before_ref": event.before_ref,
                    "after_ref": event.after_ref,
                    "input_hash": event.input_hash,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ],
            "next_cursor": next_cursor,
        }
    }
