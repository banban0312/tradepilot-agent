"""持久运行跨度、串行任务事件、业务审计与受保护的轨迹留存。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.auth.context import AuthContext
from app.models import AuditEvent, TaskEvent, TaskEventCursor, TraceSpan
from app.runtime_config import retention_days

SpanStatus = Literal["OK", "ERROR", "INTERRUPTED"]
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|authorization|api[_-]?key|cookie|credential|(^|[_-])token($|[_-]))"
)
_SECRET_TEXT = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"),
)
_REDACTED = "[已脱敏]"


def _json_safe(value: Any, *, key: str | None = None) -> Any:
    """只保存脱敏 JSON 引用和摘要；不能把凭据或原始附件内容写入轨迹。"""
    if key is not None and _SECRET_KEY.search(key):
        return _REDACTED
    if isinstance(value, str):
        result = value
        for pattern in _SECRET_TEXT:
            if pattern.groups:
                result = pattern.sub(lambda match: f"{match.group(1)}=[已脱敏]", result)
            else:
                result = pattern.sub(_REDACTED, result)
        return result
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, bytes):
        return "[二进制内容省略]"
    raise ValueError(f"不支持写入运行轨迹的值类型：{type(value).__name__}")


def _normalized_json(value: Any) -> Any:
    """确保序列化用于输入哈希的数据与轨迹保存使用同一脱敏规则。"""
    safe = _json_safe(value)
    json.dumps(
        safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return safe


def input_hash(value: Any) -> str:
    encoded = json.dumps(
        _normalized_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def append_task_event(
    session: AsyncSession,
    *,
    task_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    span_id: UUID | None = None,
    emitted_at: datetime | None = None,
) -> TaskEvent:
    """锁定任务游标、分配序号并插入事件；提交/回滚由调用方事务控制。"""
    if not event_type.strip():
        raise ValueError("event_type 不能为空")
    await session.execute(
        pg_insert(TaskEventCursor)
        .values(task_id=task_id, next_seq=1)
        .on_conflict_do_nothing(index_elements=[TaskEventCursor.task_id])
    )
    cursor = await session.scalar(
        select(TaskEventCursor)
        .where(TaskEventCursor.task_id == task_id)
        .with_for_update()
    )
    if cursor is None:
        raise RuntimeError("任务事件游标无法创建")
    seq = cursor.next_seq
    cursor.next_seq = seq + 1
    event = TaskEvent(
        task_id=task_id,
        seq=seq,
        event_type=event_type,
        span_id=span_id,
        payload=_normalized_json(payload),
        emitted_at=emitted_at or datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return event


async def append_task_event_on_connection(
    connection: AsyncConnection,
    *,
    task_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    span_id: UUID | None = None,
    emitted_at: datetime | None = None,
) -> int:
    """在调用方连接事务内追加事件，供租约状态写入与事件原子提交。"""
    if not event_type.strip():
        raise ValueError("event_type 不能为空")
    await connection.execute(
        pg_insert(TaskEventCursor)
        .values(task_id=task_id, next_seq=1)
        .on_conflict_do_nothing(index_elements=[TaskEventCursor.task_id])
    )
    seq = await connection.scalar(
        select(TaskEventCursor.next_seq)
        .where(TaskEventCursor.task_id == task_id)
        .with_for_update()
    )
    if seq is None:
        raise RuntimeError("任务事件游标无法创建")
    await connection.execute(
        update(TaskEventCursor)
        .where(TaskEventCursor.task_id == task_id)
        .values(next_seq=seq + 1)
    )
    await connection.execute(
        insert(TaskEvent).values(
            task_id=task_id,
            seq=seq,
            event_type=event_type,
            span_id=span_id,
            payload=_normalized_json(payload),
            emitted_at=emitted_at or datetime.now(UTC),
        )
    )
    return int(seq)


async def start_span(
    session: AsyncSession,
    *,
    name: str,
    kind: str,
    execution_mode: str,
    task_id: UUID | None = None,
    actor: AuthContext | None = None,
    parent_span_id: UUID | None = None,
    trace_id: UUID | None = None,
    attempt: int = 1,
    protocol: str | None = None,
    input_ref: dict[str, Any] | None = None,
    started_at: datetime | None = None,
) -> TraceSpan:
    """建立 RUNNING span；外部调用前调用方须提交本事务，使起始记录可恢复。"""
    if not name.strip() or not kind.strip() or not execution_mode.strip():
        raise ValueError("span 名称、类型和执行模式不能为空")
    if attempt < 1:
        raise ValueError("attempt 必须大于 0")

    parent = await session.get(TraceSpan, parent_span_id) if parent_span_id else None
    if parent_span_id and parent is None:
        raise ValueError("父 span 不存在")
    if parent is not None:
        if parent.task_id != task_id:
            raise ValueError("父子 span 必须属于同一任务")
        if trace_id is not None and trace_id != parent.trace_id:
            raise ValueError("父子 span 必须属于同一 trace")
        trace_id = parent.trace_id

    span = TraceSpan(
        id=uuid4(),
        trace_id=trace_id or uuid4(),
        task_id=task_id,
        parent_span_id=parent_span_id,
        actor_id=actor.user_id if actor else None,
        kind=kind,
        name=name,
        attempt=attempt,
        execution_mode=execution_mode,
        protocol=protocol,
        start_at=started_at or datetime.now(UTC),
        status="RUNNING",
        input_ref=_normalized_json(input_ref) if input_ref is not None else None,
    )
    session.add(span)
    await session.flush()
    if task_id is not None:
        await append_task_event(
            session,
            task_id=task_id,
            event_type="span.started",
            span_id=span.id,
            payload={
                "trace_id": span.trace_id,
                "span_id": span.id,
                "name": name,
                "kind": kind,
            },
            emitted_at=span.start_at,
        )
    return span


async def finish_span(
    session: AsyncSession,
    *,
    span_id: UUID,
    status: SpanStatus,
    duration_ms: int | None,
    output_ref: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    ended_at: datetime | None = None,
) -> TraceSpan:
    """完成 span 并追加状态事件；duration_ms 应来自 monotonic clock。"""
    if status not in {"OK", "ERROR", "INTERRUPTED"}:
        raise ValueError("结束状态必须是 OK、ERROR 或 INTERRUPTED")
    if duration_ms is not None and duration_ms < 0:
        raise ValueError("duration_ms 不能为负数")
    span = await session.scalar(
        select(TraceSpan).where(TraceSpan.id == span_id).with_for_update()
    )
    if span is None:
        raise ValueError("span 不存在")
    if span.status != "RUNNING":
        raise ValueError("span 已结束，不能覆盖原结果")
    span.status = status
    span.end_at = ended_at or datetime.now(UTC)
    span.duration_ms = duration_ms
    span.output_ref = _normalized_json(output_ref) if output_ref is not None else None
    span.usage = _normalized_json(usage) if usage is not None else None
    span.error = _normalized_json(error) if error is not None else None
    await session.flush()
    if span.task_id is not None:
        await append_task_event(
            session,
            task_id=span.task_id,
            event_type={
                "OK": "span.completed",
                "ERROR": "span.failed",
                "INTERRUPTED": "span.interrupted",
            }[status],
            span_id=span.id,
            payload={
                "span_id": span.id,
                "status": status,
                "duration_ms": duration_ms,
                "usage": span.usage,
                "error": span.error,
            },
            emitted_at=span.end_at,
        )
        if span.kind.casefold() == "tool" and status == "OK":
            await append_task_event(
                session,
                task_id=span.task_id,
                event_type="tool.completed",
                span_id=span.id,
                payload={
                    "span_id": span.id,
                    "name": span.name,
                    "duration_ms": duration_ms,
                    "output_ref": span.output_ref,
                },
                emitted_at=span.end_at,
            )
    return span


async def interrupt_stale_spans(
    session: AsyncSession,
    *,
    stale_before: datetime,
    interrupted_at: datetime | None = None,
) -> int:
    """将超时仍 RUNNING 的记录标为 INTERRUPTED，不编造完成结果或根因。"""
    spans = await session.scalars(
        select(TraceSpan)
        .where(TraceSpan.status == "RUNNING", TraceSpan.start_at <= stale_before)
        .order_by(TraceSpan.start_at, TraceSpan.id)
        .with_for_update(skip_locked=True)
    )
    now = interrupted_at or datetime.now(UTC)
    interrupted = list(spans)
    for span in interrupted:
        span.status = "INTERRUPTED"
        span.end_at = now
        span.error = {"code": "EXECUTION_INTERRUPTED"}
        await session.flush()
        if span.task_id is not None:
            await append_task_event(
                session,
                task_id=span.task_id,
                event_type="span.interrupted",
                span_id=span.id,
                payload={
                    "span_id": span.id,
                    "status": "INTERRUPTED",
                    "error": span.error,
                },
                emitted_at=now,
            )
    return len(interrupted)


async def record_audit_event(
    session: AsyncSession,
    *,
    action: str,
    result: str,
    actor: AuthContext | None,
    task_id: UUID | None = None,
    object_ref: dict[str, Any] | None = None,
    before_ref: dict[str, Any] | None = None,
    after_ref: dict[str, Any] | None = None,
    input_value: Any = None,
    event_id: UUID | None = None,
    created_at: datetime | None = None,
) -> AuditEvent:
    """追加审计记录；应与对应的业务动作在同一数据库事务内提交。"""
    if not action.strip() or not result.strip():
        raise ValueError("审计动作和结果不能为空")
    audit = AuditEvent(
        id=event_id or uuid4(),
        task_id=task_id,
        actor_id=actor.user_id if actor else None,
        action=action,
        object_ref=_normalized_json(object_ref) if object_ref is not None else None,
        before_ref=_normalized_json(before_ref) if before_ref is not None else None,
        after_ref=_normalized_json(after_ref) if after_ref is not None else None,
        result=result,
        input_hash=input_hash(input_value) if input_value is not None else None,
        created_at=created_at or datetime.now(UTC),
    )
    session.add(audit)
    await session.flush()
    return audit


async def list_task_events_after(
    session: AsyncSession, *, task_id: UUID, after_seq: int, limit: int = 100
) -> tuple[TaskEvent, ...]:
    """按持久游标顺序读取事件；API 调用方另行执行任务访问范围校验。"""
    if after_seq < 0 or not 1 <= limit <= 500:
        raise ValueError("事件游标或分页大小无效")
    events = await session.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id, TaskEvent.seq > after_seq)
        .order_by(TaskEvent.seq)
        .limit(limit)
    )
    return tuple(events)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    cutoff: datetime
    protected_task_count: int
    deleted_event_count: int
    deleted_span_count: int


async def purge_expired_trace_data(
    session: AsyncSession,
    *,
    now: datetime,
    active_task_ids: frozenset[UUID],
    task_ids_with_orders: frozenset[UUID],
) -> RetentionResult:
    """清理过期运行轨迹；审计与游标永久保留，活动/已下单任务受保护。"""
    cutoff = now - timedelta(days=await retention_days(session, "trace_retention_days"))
    running_ids = await session.scalars(
        select(TraceSpan.task_id)
        .where(TraceSpan.status == "RUNNING", TraceSpan.task_id.is_not(None))
        .distinct()
    )
    protected = set(active_task_ids) | set(task_ids_with_orders) | set(running_ids)

    event_delete = delete(TaskEvent).where(TaskEvent.emitted_at < cutoff)
    if protected:
        event_delete = event_delete.where(TaskEvent.task_id.not_in(protected))
    deleted_events = await session.execute(event_delete)

    span_delete = delete(TraceSpan).where(
        TraceSpan.status != "RUNNING",
        TraceSpan.end_at.is_not(None),
        TraceSpan.end_at < cutoff,
    )
    if protected:
        span_delete = span_delete.where(
            (TraceSpan.task_id.is_(None)) | TraceSpan.task_id.not_in(protected)
        )
    deleted_spans = await session.execute(span_delete)
    return RetentionResult(
        cutoff=cutoff,
        protected_task_count=len(protected),
        deleted_event_count=max(0, deleted_events.rowcount or 0),
        deleted_span_count=max(0, deleted_spans.rowcount or 0),
    )
