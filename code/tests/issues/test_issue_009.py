"""M2-02 MCP 契约与签名身份上下文验证。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.config import settings
from app.database import async_session_factory
from app.mcp_server.client import MCPToolClient, _error_payload, _validate
from app.mcp_server.context import (
    CONTEXT_HEADER,
    MCPContextError,
    issue_tool_context,
    verify_tool_context,
)
from app.mcp_server.contracts import (
    CheckContractBalanceInput,
    MCPCallError,
    MCPUnavailable,
    QueryCustomerInput,
    QueryCustomerOutput,
    SaveOrderDraftInput,
)
from app.mcp_server.server import TOOL_NAMES, mcp_server
from app.models import (
    AgentTask,
    AuditEvent,
    Department,
    Draft,
    Order,
    TaskEvent,
    TaskEventCursor,
    TaskSession,
    TraceSpan,
    User,
    UserDepartmentScope,
    UserRole,
)
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, func, select

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8001/mcp")


def _test_context(
    *, actor: AuthContext, department_id: UUID, task_id: UUID, epoch: int = 1
):
    lease = SimpleNamespace(task_id=task_id, epoch=epoch)
    return issue_tool_context(
        actor=actor,
        department_id=department_id,
        lease=lease,  # type: ignore[arg-type]
        allowed_tools=frozenset(TOOL_NAMES),
        tool_call_id="model-call-001",
        now=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_tools_publish_strict_input_and_typed_output_schemas() -> None:
    tools = {tool.name: tool for tool in await mcp_server.list_tools()}
    assert set(TOOL_NAMES) <= set(tools)
    for name in TOOL_NAMES:
        tool = tools[name]
        assert tool.input_schema.get("additionalProperties") is False
        assert tool.output_schema is not None
        assert tool.output_schema.get("additionalProperties") is False
    assert tools["query_customer"].input_schema["properties"]["limit"]["maximum"] == 10
    assert [
        clause["required"][0]
        for clause in tools["query_customer"].input_schema["anyOf"]
    ] == ["name", "code"]
    assert all(
        "pattern" in clause["properties"][clause["required"][0]]
        for clause in tools["query_customer"].input_schema["anyOf"]
    )
    assert tools["get_contract"].input_schema["properties"]["order_type"]["enum"] == [
        "SALES",
        "PURCHASE",
    ]
    assert (
        tools["check_contract_balance"].input_schema["properties"]["order_amount"][
            "type"
        ]
        == "string"
    )
    assert "approve" not in tools
    assert "create_order" not in tools


def test_strict_inputs_reject_extra_fields_wrong_types_and_invalid_money() -> None:
    with pytest.raises(ValidationError):
        QueryCustomerInput.model_validate_json('{"user_id":"forged"}', strict=True)
    with pytest.raises(ValidationError):
        QueryCustomerInput.model_validate_json(
            '{"name":"客户甲","limit":"5"}', strict=True
        )
    with pytest.raises(ValidationError):
        QueryCustomerInput.model_validate_json(
            '{"limit":11,"name":"客户甲"}', strict=True
        )
    with pytest.raises(ValidationError):
        CheckContractBalanceInput.model_validate_json(
            json.dumps(
                {
                    "contract_id": str(uuid4()),
                    "draft_id": str(uuid4()),
                    "draft_version": 1,
                    "order_amount": 220000.0,
                }
            ),
            strict=True,
        )
    for amount in (
        "NaN",
        "Infinity",
        "0.00",
        "1.234",
        "1000000000000000000.00",
    ):
        with pytest.raises(ValidationError):
            CheckContractBalanceInput.model_validate_json(
                json.dumps(
                    {
                        "contract_id": str(uuid4()),
                        "draft_id": str(uuid4()),
                        "draft_version": 1,
                        "order_amount": amount,
                    }
                ),
                strict=True,
            )
    with pytest.raises(ValidationError):
        SaveOrderDraftInput.model_validate_json(
            json.dumps(
                {
                    "task_id": str(uuid4()),
                    "expected_version": 0,
                    "candidate_order": {"business_fields": {"approved_by": "forged"}},
                    "evidence_refs": [],
                }
            ),
            strict=True,
        )


def test_context_signature_expiry_tamper_and_client_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_context_signing_key", SecretStr("k" * 64))
    actor_id, dept_id, task_id = uuid4(), uuid4(), uuid4()
    actor = AuthContext(
        user_id=actor_id,
        login="operator-test",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({dept_id}),
    )
    now = datetime.now(UTC)
    lease = SimpleNamespace(task_id=task_id, epoch=3)
    token = issue_tool_context(
        actor=actor,
        department_id=dept_id,
        lease=lease,  # type: ignore[arg-type]
        allowed_tools=frozenset({"query_customer"}),
        tool_call_id="tool-call-x",
        now=now,
    )
    verified = verify_tool_context(token, now=now + timedelta(seconds=1))
    assert verified.actor_id == actor_id
    assert verified.task_id == task_id
    assert verified.execution_epoch == 3
    payload, signature = token.split(".", 1)
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(MCPContextError, match="签名无效"):
        verify_tool_context(f"{tampered_payload}.{signature}", now=now)
    with pytest.raises(MCPContextError, match="已过期"):
        verify_tool_context(token, now=now + timedelta(seconds=46))
    with pytest.raises(ValueError, match="有效期"):
        issue_tool_context(
            actor=actor,
            department_id=dept_id,
            lease=lease,  # type: ignore[arg-type]
            allowed_tools=frozenset({"query_customer"}),
            tool_call_id="tool-call-x",
            now=now,
            ttl=timedelta(seconds=61),
        )
    with pytest.raises(MCPCallError) as bad_input:
        _validate(QueryCustomerInput, {"name": "客户甲", "unexpected": 1})
    assert bad_input.value.payload.code == "TOOL_INPUT_INVALID"
    with pytest.raises(ValidationError):
        QueryCustomerOutput.model_validate_json(
            json.dumps(
                {
                    "data": {"candidates": []},
                    "issues": [],
                    "source_refs": [],
                    "observed_at": "not-a-date",
                    "schema_version": "1.0",
                }
            ),
            strict=True,
        )
    assert (
        _error_payload(
            [
                SimpleNamespace(
                    text='Error: {"code":"ACCESS_DENIED","message":"拒绝","retryable":false,"details":{"reason":"越权"}}'
                )
            ]
        ).code
        == "ACCESS_DENIED"
    )


@pytest.mark.asyncio
async def test_real_streamable_http_context_reauthorization_and_trace() -> None:
    if os.getenv("TRADEPILOT_MCP_INTEGRATION") != "1":
        pytest.skip("真实 MCP/PostgreSQL 集成由 verify_issue_009.ps1 隔离 Compose 启动")

    async with async_session_factory() as session:
        original_counts = {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in (Draft, Order, AuditEvent)
        }

    department_id = uuid4()
    user_id = uuid4()
    task_id = uuid4()
    session_id = uuid4()
    async with async_session_factory() as session:
        session.add(
            Department(
                id=department_id,
                code=f"I9-{department_id.hex[:8]}",
                name="隔离验证部门",
                active=True,
            )
        )
        session.add(
            User(
                id=user_id,
                login=f"i9-{user_id.hex[:12]}",
                password_hash="仅用于隔离集成验证",
                department_id=department_id,
                active=True,
            )
        )
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=user_id, role=BUSINESS_OPERATOR),
                UserDepartmentScope(user_id=user_id, department_id=department_id),
                TaskSession(id=session_id, owner_id=user_id, title="Issue 9 隔离验证"),
            ]
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=user_id,
                department_id=department_id,
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

    actor = AuthContext(
        user_id=user_id,
        login=f"i9-{user_id.hex[:12]}",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({department_id}),
    )
    token = _test_context(actor=actor, department_id=department_id, task_id=task_id)
    client = MCPToolClient(MCP_URL)
    try:
        actual_tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(TOOL_NAMES) <= set(actual_tools)
        assert (await client.health_check()) == {
            "service": "tradepilot-mcp",
            "status": "ok",
        }

        # 原生官方客户端直连服务，证明服务端拒绝 client adapter 以外的伪造字段。
        async with (
            httpx.AsyncClient(headers={CONTEXT_HEADER: token}, timeout=10.0) as http,
            Client(streamable_http_client(MCP_URL, http_client=http)) as native_client,
        ):
            invalid = await native_client.call_tool(
                "query_customer", {"name": "隔离客户", "user_id": str(uuid4())}
            )
            assert invalid.is_error
            assert _error_payload(invalid.content).code == "TOOL_INPUT_INVALID"
            wrong_type = await native_client.call_tool(
                "query_customer", {"name": "隔离客户", "limit": "5"}
            )
            assert wrong_type.is_error
            assert _error_payload(wrong_type.content).code == "TOOL_INPUT_INVALID"
            missing_field = await native_client.call_tool("match_material", {})
            assert missing_field.is_error
            assert _error_payload(missing_field.content).code == "TOOL_INPUT_INVALID"
            mismatched_task = await native_client.call_tool(
                "save_order_draft",
                {
                    "task_id": str(uuid4()),
                    "expected_version": 0,
                    "candidate_order": {},
                    "evidence_refs": [],
                },
            )
            assert mismatched_task.is_error
            assert _error_payload(mismatched_task.content).code == "ACCESS_DENIED"
            forged_header = await native_client.call_tool(
                "query_customer", {"name": "隔离客户", "department_id": str(uuid4())}
            )
            assert forged_header.is_error

        async with (
            httpx.AsyncClient(timeout=10.0) as http,
            Client(streamable_http_client(MCP_URL, http_client=http)) as native_client,
        ):
            denied_without_context = await native_client.call_tool(
                "query_customer", {"name": "隔离客户"}
            )
            assert denied_without_context.is_error
            assert (
                _error_payload(denied_without_context.content).code
                == "MCP_CONTEXT_INVALID"
            )

        async with async_session_factory() as session:
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.state = "WAITING_INPUT"
            await session.commit()
        async with (
            httpx.AsyncClient(headers={CONTEXT_HEADER: token}, timeout=10.0) as http,
            Client(streamable_http_client(MCP_URL, http_client=http)) as native_client,
        ):
            inactive = await native_client.call_tool(
                "query_customer", {"name": "隔离客户"}
            )
            assert inactive.is_error
            assert _error_payload(inactive.content).code == "TASK_NOT_RUNNING"
        async with async_session_factory() as session:
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.state = "RUNNING"
            await session.commit()

        # M2-03 已交付客户查询；本隔离部门没有该客户，必须如实返回空候选。
        no_customer = await client.call_tool(
            "query_customer", {"name": "隔离客户"}, context_token=token
        )
        assert no_customer.data.candidates == ()
        assert [issue.code for issue in no_customer.issues] == ["CUSTOMER_NOT_FOUND"]

        async with async_session_factory() as session:
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.execution_epoch = 2
            await session.commit()
        async with (
            httpx.AsyncClient(headers={CONTEXT_HEADER: token}, timeout=10.0) as http,
            Client(streamable_http_client(MCP_URL, http_client=http)) as native_client,
        ):
            stale = await native_client.call_tool(
                "query_customer", {"name": "隔离客户"}
            )
            assert stale.is_error
            assert _error_payload(stale.content).code == "STALE_EXECUTION_EPOCH"

        async with async_session_factory() as session:
            spans = list(
                (
                    await session.scalars(
                        select(TraceSpan).where(TraceSpan.task_id == task_id)
                    )
                ).all()
            )
            assert len(spans) == 1
            assert spans[0].status == "OK"
            assert spans[0].error is None
            assert spans[0].input_ref["tool_call_id"] == "model-call-001"
            assert spans[0].input_ref["operation_id"]
            assert spans[0].input_ref["mcp_request_id"]
            assert "隔离客户" not in json.dumps(spans[0].input_ref, ensure_ascii=False)
            counts_after = {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model)) or 0
                )
                for model in (Draft, Order, AuditEvent)
            }
            assert counts_after == original_counts

        with pytest.raises(MCPUnavailable):
            await MCPToolClient(
                "http://127.0.0.1:1/mcp", timeout_seconds=0.2
            ).health_check()
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(TaskEvent).where(TaskEvent.task_id == task_id))
            await session.execute(
                delete(TaskEventCursor).where(TaskEventCursor.task_id == task_id)
            )
            await session.execute(delete(TraceSpan).where(TraceSpan.task_id == task_id))
            task_session = await session.get(TaskSession, session_id)
            if task_session is not None:
                task_session.last_task_id = None
                await session.flush()
            await session.execute(delete(AgentTask).where(AgentTask.id == task_id))
            await session.execute(
                delete(TaskSession).where(TaskSession.id == session_id)
            )
            await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
            await session.execute(
                delete(UserDepartmentScope).where(
                    UserDepartmentScope.user_id == user_id
                )
            )
            await session.execute(delete(User).where(User.id == user_id))
            await session.execute(
                delete(Department).where(Department.id == department_id)
            )
            await session.commit()
