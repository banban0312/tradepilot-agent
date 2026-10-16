"""M2-03 客户、物料、合同和余额 MCP 查询集成验证。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.config import settings
from app.database import async_session_factory
from app.mcp_server.client import MCPToolClient
from app.mcp_server.context import issue_tool_context
from app.mcp_server.contracts import MCPCallError
from app.mcp_server.queries import _business_date
from app.models import (
    AgentTask,
    AuditEvent,
    Contract,
    ContractBalance,
    ContractLineBalance,
    Customer,
    Draft,
    DraftRevision,
    Order,
    TaskEvent,
    TaskEventCursor,
    TaskSession,
    TraceSpan,
    User,
)

NORTH = UUID("00000000-0000-4000-8000-000000000001")
NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
SALES_CONTRACT = UUID("00000000-0000-4000-8000-000000000050")
PURCHASE_CONTRACT = UUID("00000000-0000-4000-8000-000000000051")
INACTIVE_CONTRACT = UUID("00000000-0000-4000-8000-000000000052")
SOUTH_CONTRACT_NUMBER = "TP-DEMO-SALES-S1"
DEMO_CUSTOMER = UUID("00000000-0000-4000-8000-000000000010")
DEMO_MATERIAL_12 = UUID("00000000-0000-4000-8000-000000000030")
DEMO_MATERIAL_16 = UUID("00000000-0000-4000-8000-000000000031")
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8001/mcp")


def test_contract_date_uses_configured_hong_kong_business_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "business_timezone", "Asia/Hong_Kong")
    assert _business_date(datetime(2026, 10, 16, 16, 30, tzinfo=UTC)) == date(
        2026, 10, 17
    )


def _tool_context(task_id: UUID) -> str:
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
        allowed_tools=frozenset(
            {
                "query_customer",
                "match_material",
                "get_contract",
                "check_contract_balance",
            }
        ),
        tool_call_id="issue-010-validation",
        now=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_real_mcp_queries_are_authorized_ambiguous_and_read_only() -> None:
    if os.getenv("TRADEPILOT_MCP_INTEGRATION") != "1":
        pytest.skip("真实 MCP/PostgreSQL 集成由 verify_issue_010.ps1 隔离 Compose 启动")

    task_id = uuid4()
    session_id = uuid4()
    draft_id = uuid4()
    async with async_session_factory() as session:
        assert await session.get(User, NORTH_USER) is not None
        session.add(
            TaskSession(id=session_id, owner_id=NORTH_USER, title="Issue 10 隔离验证")
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
        session.add(
            Draft(
                id=draft_id,
                current_version=1,
                lock_state="EDITABLE",
            )
        )
        session.add(
            DraftRevision(
                draft_id=draft_id,
                version=1,
                contract_id=SALES_CONTRACT,
                order_type="SALES",
                customer_id=DEMO_CUSTOMER,
                project_id=UUID("00000000-0000-4000-8000-000000000020"),
                total_amount=Decimal("220000.00"),
                content_hash="a" * 64,
                validation={"valid": False, "issues": []},
                changed_by=NORTH_USER,
            )
        )
        await session.commit()

    business_models = (
        Customer,
        Contract,
        ContractBalance,
        ContractLineBalance,
        Draft,
        DraftRevision,
        Order,
        AuditEvent,
    )
    async with async_session_factory() as session:
        before = {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in business_models
        }

    client = MCPToolClient(MCP_URL)
    token = _tool_context(task_id)
    try:
        customer = await client.call_tool(
            "query_customer", {"name": "华北示例客户"}, context_token=token
        )
        assert len(customer.data.candidates) == 1
        assert customer.data.candidates[0].id == DEMO_CUSTOMER
        assert customer.data.candidates[0].reason == "名称匹配"

        hidden_customer = await client.call_tool(
            "query_customer", {"name": "华南示例客户"}, context_token=token
        )
        assert hidden_customer.data.candidates == ()
        assert [item.code for item in hidden_customer.issues] == ["CUSTOMER_NOT_FOUND"]

        ambiguous = await client.call_tool(
            "match_material",
            {"raw_name": "HRB400E", "specification": "HRB400E"},
            context_token=token,
        )
        assert {item.material_id for item in ambiguous.data.candidates} == {
            DEMO_MATERIAL_12,
            DEMO_MATERIAL_16,
        }
        assert all(item.requires_confirmation for item in ambiguous.data.candidates)
        assert [item.code for item in ambiguous.issues] == [
            "MATERIAL_CONFIRMATION_REQUIRED"
        ]

        specific = await client.call_tool(
            "match_material",
            {"raw_name": "HRB400E", "specification": "12mm", "unit": "吨"},
            context_token=token,
        )
        assert [item.material_id for item in specific.data.candidates] == [
            DEMO_MATERIAL_12
        ]
        assert specific.data.candidates[0].specification == "HRB400 12mm"
        assert not specific.data.candidates[0].requires_confirmation
        assert specific.issues == ()

        contract = await client.call_tool(
            "get_contract",
            {"contract_number": "TP-DEMO-SALES-N1", "order_type": "SALES"},
            context_token=token,
        )
        assert contract.data.id == SALES_CONTRACT
        assert contract.data.customer_name == "华北示例客户"
        assert contract.data.project_name == "华北示例工程"
        assert contract.data.lines[0].unit_price == "3500.000000"
        assert contract.issues == ()

        wrong_direction = await client.call_tool(
            "get_contract",
            {"contract_number": "TP-DEMO-PURCHASE-N1", "order_type": "PURCHASE"},
            context_token=token,
        )
        assert wrong_direction.data.id == PURCHASE_CONTRACT
        assert [item.code for item in wrong_direction.issues] == [
            "CONTRACT_TYPE_MISMATCH"
        ]

        inactive = await client.call_tool(
            "get_contract",
            {"contract_number": "TP-DEMO-INACTIVE-N1", "order_type": "SALES"},
            context_token=token,
        )
        assert inactive.data.status == "INACTIVE"
        assert [item.code for item in inactive.issues] == ["CONTRACT_INACTIVE"]

        with pytest.raises(MCPCallError) as cross_department:
            await client.call_tool(
                "get_contract",
                {"contract_number": SOUTH_CONTRACT_NUMBER, "order_type": "SALES"},
                context_token=token,
            )
        assert cross_department.value.payload.code == "CONTRACT_NOT_FOUND"

        balance = await client.call_tool(
            "check_contract_balance",
            {
                "contract_id": str(SALES_CONTRACT),
                "draft_id": str(draft_id),
                "draft_version": 1,
                "order_amount": "220000.00",
            },
            context_token=token,
        )
        assert balance.data.total == "150000.00"
        assert balance.data.occupied == "0.00"
        assert balance.data.remaining == "150000.00"
        assert balance.data.order_amount == "220000.00"
        assert not balance.data.allowed
        assert "CONTRACT_AMOUNT_EXCEEDED" in balance.data.reasons

        with pytest.raises(MCPCallError) as self_reported_amount:
            await client.call_tool(
                "check_contract_balance",
                {
                    "contract_id": str(SALES_CONTRACT),
                    "draft_id": str(draft_id),
                    "draft_version": 1,
                    "order_amount": "149999.99",
                },
                context_token=token,
            )
        assert self_reported_amount.value.payload.code == "DRAFT_AMOUNT_MISMATCH"

        async with async_session_factory() as session:
            after = {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model)) or 0
                )
                for model in business_models
            }
            assert after == before
            balance_row = await session.get(ContractBalance, SALES_CONTRACT)
            assert balance_row is not None
            assert balance_row.occupied_amount == Decimal("0.00")
            assert balance_row.occupied_order_count == 0
            spans = list(
                (
                    await session.scalars(
                        select(TraceSpan).where(TraceSpan.task_id == task_id)
                    )
                ).all()
            )
            assert len(spans) == 8
            assert all(span.status == "OK" for span in spans)
            for span in spans:
                assert span.input_ref is not None and span.input_ref.get("input_hash")
                assert span.output_ref is not None and span.output_ref.get(
                    "output_hash"
                )
                assert "華北示例客户" not in str(span.output_ref)
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(TaskEvent).where(TaskEvent.task_id == task_id))
            await session.execute(
                delete(TaskEventCursor).where(TaskEventCursor.task_id == task_id)
            )
            await session.execute(delete(TraceSpan).where(TraceSpan.task_id == task_id))
            await session.execute(
                delete(DraftRevision).where(DraftRevision.draft_id == draft_id)
            )
            await session.execute(delete(Draft).where(Draft.id == draft_id))
            task_session = await session.get(TaskSession, session_id)
            if task_session is not None:
                task_session.last_task_id = None
                await session.flush()
            await session.execute(delete(AgentTask).where(AgentTask.id == task_id))
            await session.execute(
                delete(TaskSession).where(TaskSession.id == session_id)
            )
