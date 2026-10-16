"""M2-04 真实 MCP 草稿保存、版本冲突与逻辑操作幂等验证。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.mcp_server.client import MCPToolClient
from app.mcp_server.context import issue_tool_context
from app.mcp_server.contracts import MCPCallError
from app.models import (
    AgentTask,
    AuditEvent,
    Draft,
    DraftRevision,
    MCPToolEffect,
    Order,
    TaskEvent,
    TaskSession,
    TraceSpan,
    User,
)
from sqlalchemy import func, select

NORTH = UUID("00000000-0000-4000-8000-000000000001")
NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8001/mcp")


def _token(task_id: UUID, operation_id: UUID, *, call_id: str) -> str:
    actor = AuthContext(
        user_id=NORTH_USER,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({NORTH}),
    )
    return issue_tool_context(
        actor=actor,
        department_id=NORTH,
        lease=SimpleNamespace(task_id=task_id, epoch=1),  # type: ignore[arg-type]
        allowed_tools=frozenset({"save_order_draft"}),
        tool_call_id=call_id,
        operation_id=operation_id,
        now=datetime.now(UTC),
    )


def _candidate(address: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "items": [
            {
                "raw_name": "HRB400E 螺纹钢",
                "specification": "20mm",
                "quantity": "180",
                "unit": "吨",
                "unit_price": "3500.000000",
            }
        ]
    }
    if address is not None:
        value["address"] = address
    return value


def _arguments(
    task_id: UUID, version: int, candidate: dict[str, object]
) -> dict[str, object]:
    return {
        "task_id": str(task_id),
        "expected_version": version,
        "candidate_order": candidate,
        "evidence_refs": [],
    }


async def _call(
    client: MCPToolClient,
    task_id: UUID,
    op_id: UUID,
    version: int,
    candidate: dict[str, object],
    *,
    call_id: str,
):
    return await client.call_tool(
        "save_order_draft",
        _arguments(task_id, version, candidate),
        context_token=_token(task_id, op_id, call_id=call_id),
    )


@pytest.mark.asyncio
async def test_real_mcp_draft_save_versions_and_idempotency() -> None:
    if os.getenv("TRADEPILOT_MCP_INTEGRATION") != "1":
        pytest.skip("真实 MCP/PostgreSQL 集成由 verify_issue_011.ps1 隔离 Compose 启动")

    task_id, session_id = uuid4(), uuid4()
    async with async_session_factory() as session:
        assert await session.get(User, NORTH_USER) is not None
        session.add(
            TaskSession(id=session_id, owner_id=NORTH_USER, title="Issue 11 隔离验证")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=NORTH_USER,
                department_id=NORTH,
                task_type="SALES",
                state="RUNNING",
                execution_epoch=1,
            )
        )
        await session.flush()
        task_session = await session.get(TaskSession, session_id)
        assert task_session is not None
        task_session.last_task_id = task_id
        await session.commit()

    client = MCPToolClient(MCP_URL)
    first_op = uuid4()
    wrong_task_args = _arguments(uuid4(), 0, _candidate())
    try:
        first = await _call(
            client, task_id, first_op, 0, _candidate(), call_id="save-first"
        )
        assert first.data.version == 1
        assert first.data.total_amount == "630000.00"
        assert not first.data.is_validated
        assert first.data.issues
        assert first.data.content_hash

        # A retried graph node may have a new MCP call ID; operation ID and arguments
        # remain the durable identity and must reproduce the complete original result.
        replay = await _call(
            client, task_id, first_op, 0, _candidate(), call_id="save-replay"
        )
        assert replay == first

        with pytest.raises(MCPCallError) as changed_operation:
            await _call(
                client,
                task_id,
                first_op,
                0,
                _candidate("不同工地"),
                call_id="save-replay-conflict",
            )
        assert changed_operation.value.payload.code == "IDEMPOTENCY_CONFLICT"

        with pytest.raises(MCPCallError) as mismatched_task:
            await client.call_tool(
                "save_order_draft",
                wrong_task_args,
                context_token=_token(task_id, uuid4(), call_id="forged-task"),
            )
        assert mismatched_task.value.payload.code == "ACCESS_DENIED"

        # A new logical action with identical business content does not create a
        # second revision, while a real edit creates v2 and recalculates the hash.
        same_content = await _call(
            client, task_id, uuid4(), 1, _candidate(), call_id="save-same-content"
        )
        assert same_content.data.version == 1
        assert same_content.data.content_hash == first.data.content_hash
        updated = await _call(
            client,
            task_id,
            uuid4(),
            1,
            _candidate("一号工地"),
            call_id="save-edit",
        )
        assert updated.data.version == 2
        assert updated.data.content_hash != first.data.content_hash
        assert not updated.data.is_validated

        with pytest.raises(MCPCallError) as stale_version:
            await _call(
                client,
                task_id,
                uuid4(),
                1,
                _candidate("旧版本覆盖"),
                call_id="save-stale-version",
            )
        assert stale_version.value.payload.code == "DRAFT_VERSION_CONFLICT"

        # Same expected version under concurrent distinct operations is serialized
        # by the task row lock: exactly one edit wins and the other sees a conflict.
        concurrent = await asyncio.gather(
            _call(
                client,
                task_id,
                uuid4(),
                2,
                _candidate("并发地址甲"),
                call_id="save-race-a",
            ),
            _call(
                client,
                task_id,
                uuid4(),
                2,
                _candidate("并发地址乙"),
                call_id="save-race-b",
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, Exception) for item in concurrent) == 1
        conflict = next(item for item in concurrent if isinstance(item, MCPCallError))
        assert conflict.payload.code == "DRAFT_VERSION_CONFLICT"

        async with async_session_factory() as session:
            drafts = list(
                (
                    await session.scalars(select(Draft).where(Draft.task_id == task_id))
                ).all()
            )
            assert len(drafts) == 1
            draft = drafts[0]
            revisions = int(
                await session.scalar(
                    select(func.count())
                    .select_from(DraftRevision)
                    .where(DraftRevision.draft_id == draft.id)
                )
                or 0
            )
            assert revisions == 3
            effects = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MCPToolEffect)
                    .where(MCPToolEffect.task_id == task_id)
                )
                or 0
            )
            assert effects == 4
            audits = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.task_id == task_id,
                        AuditEvent.action == "MCP草稿保存",
                    )
                )
                or 0
            )
            assert audits == 4
            saved_events = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskEvent)
                    .where(
                        TaskEvent.task_id == task_id,
                        TaskEvent.event_type == "draft.saved",
                    )
                )
                or 0
            )
            assert saved_events == 4
            assert await session.scalar(select(func.count()).select_from(Order)) == 0
            spans = list(
                (
                    await session.scalars(
                        select(TraceSpan).where(TraceSpan.task_id == task_id)
                    )
                ).all()
            )
            assert len(spans) == 5  # successful saves/replay plus the winning race
            assert all(span.status == "OK" for span in spans)
            assert all(
                span.input_ref and span.input_ref.get("operation_id") for span in spans
            )
            assert all(
                "HRB400E" not in json.dumps(span.input_ref, ensure_ascii=False)
                for span in spans
            )

        # Invalid epoch must leave the existing draft and effect ledger untouched.
        async with async_session_factory() as session:
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.execution_epoch = 2
            await session.commit()
        with pytest.raises(MCPCallError) as stale_epoch:
            await _call(
                client,
                task_id,
                uuid4(),
                3,
                _candidate("陈旧租约"),
                call_id="save-stale-epoch",
            )
        assert stale_epoch.value.payload.code == "STALE_EXECUTION_EPOCH"
        async with async_session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(DraftRevision)
                    .where(DraftRevision.draft_id == draft.id)
                )
                == 3
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(MCPToolEffect)
                    .where(MCPToolEffect.task_id == task_id)
                )
                == 4
            )
    finally:
        # 审计行只能追加。此数据仅创建在隔离 Compose 数据库中，由验证脚本销毁卷。
        pass
