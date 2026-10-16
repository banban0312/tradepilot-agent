"""验证任务轨迹、顺序事件、敏感值脱敏、追加审计及留存清理。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.config import Settings, settings
from app.database import async_session_factory
from app.models import (
    AgentTask,
    AuditEvent,
    TaskEvent,
    TaskEventCursor,
    TaskSession,
    TraceSpan,
)
from app.observability.service import (
    append_task_event,
    finish_span,
    input_hash,
    interrupt_stale_spans,
    list_task_events_after,
    purge_expired_trace_data,
    record_audit_event,
    start_span,
)
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

ACTOR = AuthContext(
    user_id=UUID("00000000-0000-4000-8000-000000000100"),
    login="demo-operator-north",
    roles=frozenset({BUSINESS_OPERATOR}),
    department_ids=frozenset({UUID("00000000-0000-4000-8000-000000000001")}),
)


async def _create_task_rows(session, task_ids: list[UUID]) -> None:
    """M2 起轨迹引用真实任务；回归测试仍使用隔离的任务 UUID。"""
    for task_id in task_ids:
        nav = TaskSession(id=uuid4(), owner_id=ACTOR.user_id, title="轨迹回归")
        session.add(nav)
        await session.flush()
        session.add(AgentTask(
            id=task_id, session_id=nav.id, owner_id=ACTOR.user_id,
            department_id=next(iter(ACTOR.department_ids)), task_type="SALES",
        ))
        await session.flush()
        nav.last_task_id = task_id


@pytest.mark.asyncio
async def test_span_hierarchy_redaction_usage_and_audit_append_only() -> None:
    task_id = uuid4()
    started = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        await _create_task_rows(session, [task_id])
        parent = await start_span(
            session,
            task_id=task_id,
            actor=ACTOR,
            name="模型调用",
            kind="MODEL",
            execution_mode="AGENT",
            input_ref={"prompt": "Bearer abc.def", "api_key": "should-not-persist"},
            started_at=started,
        )
        child = await start_span(
            session,
            task_id=task_id,
            actor=ACTOR,
            parent_span_id=parent.id,
            name="工具调用",
            kind="TOOL",
            execution_mode="AGENT",
            input_ref={"password": "top-secret", "query": "password=hunter2"},
            started_at=started,
        )
        await finish_span(
            session,
            span_id=child.id,
            status="OK",
            duration_ms=17,
            output_ref={"result": "完成", "token": "sk-abcdefgh12345678"},
            usage={"input_tokens": 13, "output_tokens": 5},
            ended_at=started + timedelta(milliseconds=17),
        )
        await finish_span(
            session,
            span_id=parent.id,
            status="ERROR",
            duration_ms=21,
            error={"code": "UPSTREAM_FAILED", "detail": "secret=do-not-store"},
            ended_at=started + timedelta(milliseconds=21),
        )
        audit = await record_audit_event(
            session,
            actor=ACTOR,
            action="草稿版本修改",
            result="SUCCEEDED",
            task_id=task_id,
            object_ref={"draft_id": str(uuid4()), "version": 3},
            before_ref={"version": 2},
            after_ref={"version": 3},
            input_value={"authorization": "Bearer abc.def", "quantity": 8},
        )
        audit_id = audit.id
        parent_id, child_id = parent.id, child.id

    async with async_session_factory() as session:
        spans = list(
            await session.scalars(
                select(TraceSpan).where(TraceSpan.id.in_([parent_id, child_id]))
            )
        )
        by_id = {span.id: span for span in spans}
        assert by_id[child_id].parent_span_id == parent_id
        assert by_id[parent_id].status == "ERROR"
        assert by_id[child_id].status == "OK"
        assert by_id[child_id].duration_ms == 17
        assert by_id[child_id].usage == {"input_tokens": 13, "output_tokens": 5}
        assert by_id[parent_id].input_ref["api_key"] == "[已脱敏]"
        assert "abc.def" not in str(by_id[parent_id].input_ref)
        assert "hunter2" not in str(by_id[child_id].input_ref)
        assert "abcdefgh12345678" not in str(by_id[child_id].output_ref)
        audit = await session.get(AuditEvent, audit_id)
        assert audit is not None and audit.actor_id == ACTOR.user_id
        assert audit.action == "草稿版本修改" and audit.result == "SUCCEEDED"
        assert audit.input_hash == input_hash(
            {"authorization": "Bearer abc.def", "quantity": 8}
        )
        assert len(audit.input_hash) == 64
        with pytest.raises(DBAPIError):
            await session.execute(
                update(AuditEvent)
                .where(AuditEvent.id == audit_id)
                .values(action="被篡改")
            )
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(delete(AuditEvent).where(AuditEvent.id == audit_id))
        await session.rollback()
        assert await session.get(AuditEvent, audit_id) is not None


@pytest.mark.asyncio
async def test_event_sequence_is_serialized_and_rollback_leaves_no_gap() -> None:
    task_id = uuid4()
    async with async_session_factory() as session, session.begin():
        await _create_task_rows(session, [task_id])

    async def write_event(index: int) -> int:
        async with async_session_factory() as session, session.begin():
            event = await append_task_event(
                session,
                task_id=task_id,
                event_type="验证事件",
                payload={"index": index},
            )
            return event.seq

    sequences = await asyncio.gather(*(write_event(i) for i in range(12)))
    assert sorted(sequences) == list(range(1, 13))

    async with async_session_factory() as session:
        with pytest.raises(RuntimeError, match="模拟调用方回滚"):
            async with session.begin():
                rolled_back = await append_task_event(
                    session,
                    task_id=task_id,
                    event_type="不会持久化",
                    payload={"ok": True},
                )
                assert rolled_back.seq == 13
                raise RuntimeError("模拟调用方回滚")

    async with async_session_factory() as session, session.begin():
        committed = await append_task_event(
            session,
            task_id=task_id,
            event_type="验证事件",
            payload={"index": 13},
        )
        assert committed.seq == 13

    async with async_session_factory() as session:
        events = await list_task_events_after(session, task_id=task_id, after_seq=0)
        assert [event.seq for event in events] == list(range(1, 14))
        assert await list_task_events_after(session, task_id=task_id, after_seq=12) == (
            events[-1],
        )


@pytest.mark.asyncio
async def test_stale_running_span_is_interrupted_without_invented_cause() -> None:
    started = datetime.now(UTC) - timedelta(hours=2)
    task_id = uuid4()
    async with async_session_factory() as session, session.begin():
        await _create_task_rows(session, [task_id])
        span = await start_span(
            session,
            task_id=task_id,
            name="中断恢复检查",
            kind="WORKFLOW",
            execution_mode="AGENT",
            started_at=started,
        )
        span_id = span.id

    async with async_session_factory() as session, session.begin():
        count = await interrupt_stale_spans(
            session,
            stale_before=datetime.now(UTC) - timedelta(minutes=30),
        )
        assert count >= 1

    async with async_session_factory() as session:
        span = await session.get(TraceSpan, span_id)
        assert span is not None and span.status == "INTERRUPTED"
        assert span.error == {"code": "EXECUTION_INTERRUPTED"}
        assert span.output_ref is None and span.duration_ms is None

    direct_task_id = uuid4()
    async with async_session_factory() as session, session.begin():
        await _create_task_rows(session, [direct_task_id])
        span = await start_span(
            session,
            task_id=direct_task_id,
            name="显式中断",
            kind="WORKFLOW",
            execution_mode="AGENT",
        )
        await finish_span(
            session,
            span_id=span.id,
            status="INTERRUPTED",
            duration_ms=None,
            error={"code": "EXECUTION_INTERRUPTED"},
        )

    async with async_session_factory() as session:
        events = await list_task_events_after(
            session, task_id=direct_task_id, after_seq=0
        )
        assert [event.event_type for event in events] == [
            "span.started",
            "span.interrupted",
        ]


@pytest.mark.asyncio
async def test_retention_defaults_to_30_days_and_protects_live_ordered_and_audit_data() -> (
    None
):
    assert settings.trace_retention_days == 30
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=settings.trace_retention_days)
    expired_at = cutoff - timedelta(days=1)
    eligible_task, active_task, ordered_task, running_task = (uuid4() for _ in range(4))
    audit_id = uuid4()

    async with async_session_factory() as session, session.begin():
        await _create_task_rows(
            session, [eligible_task, active_task, ordered_task, running_task]
        )
        for task_id in (eligible_task, active_task, ordered_task, running_task):
            await append_task_event(
                session,
                task_id=task_id,
                event_type="历史事件",
                payload={"sample": True},
                emitted_at=expired_at,
            )
        for task_id in (eligible_task, active_task, ordered_task):
            span = await start_span(
                session,
                task_id=task_id,
                name="已结束轨迹",
                kind="RULE",
                execution_mode="DIRECT",
                started_at=expired_at,
            )
            await finish_span(
                session,
                span_id=span.id,
                status="OK",
                duration_ms=1,
                ended_at=expired_at,
            )
        await start_span(
            session,
            task_id=running_task,
            name="仍在运行",
            kind="WORKFLOW",
            execution_mode="AGENT",
            started_at=expired_at,
        )
        await record_audit_event(
            session,
            action="历史审计",
            result="SUCCEEDED",
            actor=ACTOR,
            task_id=eligible_task,
            input_value={"sample": True},
            event_id=audit_id,
            created_at=expired_at,
        )

    async with async_session_factory() as session, session.begin():
        result = await purge_expired_trace_data(
            session,
            now=now,
            active_task_ids=frozenset({active_task}),
            task_ids_with_orders=frozenset({ordered_task}),
        )
        assert result.deleted_event_count >= 1
        assert result.deleted_span_count >= 1
        assert result.protected_task_count == 3

    async with async_session_factory() as session:
        events = list(
            await session.scalars(
                select(TaskEvent).where(
                    TaskEvent.task_id.in_(
                        [eligible_task, active_task, ordered_task, running_task]
                    )
                )
            )
        )
        assert {event.task_id for event in events} == {
            active_task,
            ordered_task,
            running_task,
        }
        cursor = await session.get(TaskEventCursor, eligible_task)
        assert cursor is not None
        assert await session.get(AuditEvent, audit_id) is not None


@pytest.mark.asyncio
async def test_sensitive_raw_bytes_are_omitted_and_invalid_values_rejected() -> None:
    assert input_hash({"secret": "alpha"}) == input_hash(
        {"secret": "beta"}
    )  # 哈希按同一脱敏后的结构计算，不承诺对低熵秘密不可枚举。
    with pytest.raises(ValueError, match="不支持写入"):
        input_hash({"attachment": object()})
    configured = Settings(
        database_url="postgresql://example.invalid/db", trace_retention_days=45
    )
    assert configured.trace_retention_days == 45
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://example.invalid/db", trace_retention_days=0)
    assert settings.trace_retention_days >= 1
