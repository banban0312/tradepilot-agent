"""严格 MCP 服务；主数据查询实际读取授权范围内的业务记录。"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Tool as MCPTool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import (
    BUSINESS_APPROVER,
    BUSINESS_OPERATOR,
    AuthContext,
    require_business_access,
)
from app.config import settings
from app.database import async_session_factory
from app.drafts.service import DraftConflictError, save_candidate
from app.drafts.types import CandidateOrder, FieldEvidenceInput
from app.mcp_server.context import (
    CONTEXT_HEADER,
    MCP_TOOL_ALLOWLIST,
    MCPContextError,
    ToolContextClaims,
    verify_tool_context,
)
from app.mcp_server.contracts import (
    CheckContractBalanceInput,
    CheckContractBalanceOutput,
    GetContractInput,
    GetContractOutput,
    MatchMaterialInput,
    MatchMaterialOutput,
    MCPErrorPayload,
    QueryCustomerInput,
    QueryCustomerOutput,
    SavedDraftData,
    SaveOrderDraftInput,
    SaveOrderDraftOutput,
    StrictModel,
    ToolIssue,
    validate_raw_input,
)
from app.mcp_server.queries import (
    MCPReadError,
    check_balance,
    get_contract_details,
    match_materials,
    query_customers,
)
from app.models import (
    AgentTask,
    Department,
    Draft,
    MCPToolEffect,
    User,
    UserDepartmentScope,
    UserRole,
)
from app.observability.service import (
    append_task_event,
    finish_span,
    input_hash,
    record_audit_event,
    start_span,
)

TOOL_NAMES = MCP_TOOL_ALLOWLIST
INPUT_MODELS = {
    "query_customer": QueryCustomerInput,
    "match_material": MatchMaterialInput,
    "get_contract": GetContractInput,
    "check_contract_balance": CheckContractBalanceInput,
    "save_order_draft": SaveOrderDraftInput,
}


class StrictSchemaMCPServer(MCPServer):
    """通过公开 list_tools 扩展点发布业务模型生成的精确输入契约。"""

    async def list_tools(self) -> list[MCPTool]:
        published = await super().list_tools()
        return [
            tool.model_copy(update={"input_schema": _input_schema(tool.name)})
            if tool.name in INPUT_MODELS
            else tool
            for tool in published
        ]


def _input_schema(tool_name: str) -> dict[str, Any]:
    schema = INPUT_MODELS[tool_name].model_json_schema()
    if tool_name == "query_customer":
        schema["anyOf"] = [
            {
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": r".*\S.*",
                    }
                },
            },
            {
                "required": ["code"],
                "properties": {
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": r".*\S.*",
                    }
                },
            },
        ]
    return schema


mcp_server = StrictSchemaMCPServer("TradePilot MCP")


def _error(code: str, message: str, *, retryable: bool = False) -> ToolError:
    payload = MCPErrorPayload(code=code, message=message, retryable=retryable)
    return ToolError(payload.model_dump_json())


def _raw_arguments(ctx: Context[Any, Any]) -> object:
    params = ctx.request_context.params
    if not isinstance(params, Mapping):
        raise _error("TOOL_INPUT_INVALID", "MCP 工具参数无效")
    arguments = params.get("arguments")
    if not isinstance(arguments, Mapping):
        raise _error("TOOL_INPUT_INVALID", "MCP 工具参数必须为 JSON 对象")
    return dict(arguments)


def _verified_context(ctx: Context[Any, Any], tool_name: str) -> ToolContextClaims:
    headers = ctx.headers or {}
    token = next(
        (
            value
            for key, value in headers.items()
            if key.casefold() == CONTEXT_HEADER.casefold()
        ),
        "",
    )
    try:
        claims = verify_tool_context(token)
    except MCPContextError as exc:
        raise _error(exc.code, str(exc)) from exc
    if tool_name not in claims.allowed_tools:
        raise _error("MCP_TOOL_NOT_ALLOWED", "当前任务上下文未授权此工具")
    return claims


async def _reauthorize(
    session: AsyncSession,
    claims: ToolContextClaims,
    tool_name: str,
    *,
    lock_task: bool = False,
) -> AuthContext:
    user = await session.get(User, claims.actor_id)
    department = await session.get(Department, claims.department_id)
    task = (
        await session.scalar(
            select(AgentTask).where(AgentTask.id == claims.task_id).with_for_update()
        )
        if lock_task
        else await session.get(AgentTask, claims.task_id)
    )
    if user is None or not user.active or department is None or not department.active:
        raise _error("ACCESS_DENIED", "当前用户或部门不可用")
    if (
        task is None
        or task.owner_id != claims.actor_id
        or task.department_id != claims.department_id
    ):
        raise _error("ACCESS_DENIED", "任务不属于当前身份或部门")
    if task.state != "RUNNING":
        raise _error("TASK_NOT_RUNNING", "任务当前没有可用的执行租约")
    if task.execution_epoch != claims.execution_epoch:
        raise _error("STALE_EXECUTION_EPOCH", "任务执行上下文已失效，请重新执行")
    roles = frozenset(
        (
            await session.scalars(
                select(UserRole.role).where(UserRole.user_id == user.id)
            )
        ).all()
    )
    scopes = frozenset(
        (
            await session.scalars(
                select(UserDepartmentScope.department_id)
                .join(Department, Department.id == UserDepartmentScope.department_id)
                .where(
                    UserDepartmentScope.user_id == user.id,
                    Department.active.is_(True),
                )
            )
        ).all()
    )
    actor = AuthContext(
        user_id=user.id, login=user.login, roles=roles, department_ids=scopes
    )
    try:
        require_business_access(actor, claims.department_id)
    except HTTPException as exc:
        raise _error("ACCESS_DENIED", "当前身份没有此任务的数据访问权限") from exc
    if not roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise _error("ACCESS_DENIED", "当前身份没有业务操作角色")
    if tool_name == "save_order_draft" and BUSINESS_OPERATOR not in roles:
        raise _error("ACCESS_DENIED", "当前身份没有草稿经办权限")
    return actor


async def _execute_read(
    model: type[StrictModel],
    output_model: type[StrictModel],
    tool_name: str,
    ctx: Context[Any, Any],
    query,
) -> StrictModel:
    """验证原始 MCP 参数和可信身份后执行只读查询，并记录摘要轨迹。"""
    started_at = datetime.now(UTC)
    started_ns = time.monotonic_ns()
    try:
        value = validate_raw_input(model, _raw_arguments(ctx))
    except Exception as exc:
        if isinstance(exc, ToolError):
            raise
        raise _error("TOOL_INPUT_INVALID", "工具输入不符合严格 Schema") from exc

    claims = _verified_context(ctx, tool_name)
    async with async_session_factory() as session:
        try:
            actor = await _reauthorize(session, claims, tool_name)
            task = await session.get(AgentTask, claims.task_id)
            if task is None:
                raise _error("ACCESS_DENIED", "任务不属于当前身份或部门")
            if tool_name == "query_customer":
                result = await query(session, value, department_id=claims.department_id)
            elif tool_name == "match_material":
                result = await query(session, value)
            elif tool_name == "get_contract":
                result = await query(
                    session,
                    value,
                    department_id=claims.department_id,
                    task_type=task.task_type,
                )
            elif tool_name == "check_contract_balance":
                result = await query(
                    session,
                    value,
                    department_id=claims.department_id,
                    task_type=task.task_type,
                    actor_id=actor.user_id,
                )
            else:
                result = await query(
                    session,
                    value,
                    department_id=claims.department_id,
                    task_type=task.task_type,
                )
            result = output_model.model_validate_json(
                json.dumps(
                    result.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                strict=True,
            )
        except (ToolError, MCPReadError) as exc:
            if isinstance(exc, ToolError):
                raise
            raise _error(exc.code, str(exc)) from exc
        except Exception as exc:
            raise _error(
                "MCP_QUERY_UNAVAILABLE",
                "无法读取当前业务主数据",
                retryable=True,
            ) from exc

    arguments = value.model_dump(mode="json")
    result_data = result.model_dump(mode="json")
    span_id = None
    try:
        async with async_session_factory() as session:
            span = await start_span(
                session,
                name=tool_name,
                kind="tool",
                execution_mode="live",
                task_id=claims.task_id,
                actor=actor,
                trace_id=claims.trace_id,
                protocol="MCP/Streamable HTTP",
                started_at=started_at,
                input_ref={
                    "input_hash": input_hash(arguments),
                    "operation_id": claims.operation_id,
                    "tool_call_id": claims.tool_call_id,
                    "mcp_request_id": ctx.request_id,
                },
            )
            span_id = span.id
            await session.commit()
        async with async_session_factory() as session:
            await finish_span(
                session,
                span_id=span_id,
                status="OK",
                duration_ms=max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
                output_ref={
                    "output_hash": input_hash(result_data),
                    "source_refs": list(result_data.get("source_refs", ())),
                },
            )
            await session.commit()
    except Exception as exc:
        raise _error(
            "MCP_TRACE_UNAVAILABLE", "查询结果轨迹未能安全保存", retryable=True
        ) from exc
    return result


async def query_customer(
    name: Any = None,
    code: Any = None,
    limit: Any = None,
    ctx: Context[Any, Any] = None,
) -> QueryCustomerOutput:
    """按当前任务部门查询客户候选；本工具仅支持只读查询。"""
    return await _execute_read(
        QueryCustomerInput,
        QueryCustomerOutput,
        "query_customer",
        ctx,
        query_customers,
    )  # type: ignore[return-value]


async def match_material(
    raw_name: Any = None,
    specification: Any = None,
    unit: Any = None,
    ctx: Context[Any, Any] = None,
) -> MatchMaterialOutput:
    """按名称、规格和单位匹配标准物料；候选确认由业务流程处理。"""
    return await _execute_read(
        MatchMaterialInput,
        MatchMaterialOutput,
        "match_material",
        ctx,
        match_materials,
    )  # type: ignore[return-value]


async def get_contract(
    contract_number: Any = None,
    order_type: Any = None,
    ctx: Context[Any, Any] = None,
) -> GetContractOutput:
    """查询当前任务部门内合同信息；不创建订单或占用额度。"""
    return await _execute_read(
        GetContractInput,
        GetContractOutput,
        "get_contract",
        ctx,
        get_contract_details,
    )  # type: ignore[return-value]


async def check_contract_balance(
    contract_id: Any = None,
    draft_id: Any = None,
    draft_version: Any = None,
    order_amount: Any = None,
    ctx: Context[Any, Any] = None,
) -> CheckContractBalanceOutput:
    """核验合同额度的只读契约；提交时仍需服务端重新核验。"""
    return await _execute_read(
        CheckContractBalanceInput,
        CheckContractBalanceOutput,
        "check_contract_balance",
        ctx,
        check_balance,
    )  # type: ignore[return-value]


async def _execute_save_draft(ctx: Context[Any, Any]) -> SaveOrderDraftOutput:
    """在一次事务内保存版本、效果账本、审计及成功轨迹。"""
    started_at = datetime.now(UTC)
    started_ns = time.monotonic_ns()
    try:
        value = validate_raw_input(SaveOrderDraftInput, _raw_arguments(ctx))
    except Exception as exc:
        if isinstance(exc, ToolError):
            raise
        raise _error("TOOL_INPUT_INVALID", "工具输入不符合严格 Schema") from exc
    assert isinstance(value, SaveOrderDraftInput)

    claims = _verified_context(ctx, "save_order_draft")
    if value.task_id != claims.task_id:
        raise _error("ACCESS_DENIED", "请求任务与可信任务上下文不一致")

    arguments = value.model_dump(mode="json", exclude_none=True)
    args_hash = input_hash(arguments)
    # advisory transaction lock also serializes an accidentally reused operation ID
    # across different tasks; a stored result can never be returned across task scope.
    op_key = claims.operation_id.bytes
    lock_a = int.from_bytes(op_key[:4], "big", signed=True)
    lock_b = int.from_bytes(op_key[4:8], "big", signed=True)

    try:
        async with async_session_factory() as session, session.begin():
            actor = await _reauthorize(
                session, claims, "save_order_draft", lock_task=True
            )
            await session.execute(select(func.pg_advisory_xact_lock(lock_a, lock_b)))
            effect = await session.get(MCPToolEffect, claims.operation_id)
            if effect is not None:
                if (
                    effect.task_id != claims.task_id
                    or effect.tool_name != "save_order_draft"
                    or effect.args_hash != args_hash
                ):
                    raise _error(
                        "IDEMPOTENCY_CONFLICT",
                        "同一逻辑操作 ID 已用于不同任务、工具或参数",
                    )
                output = SaveOrderDraftOutput.model_validate_json(
                    json.dumps(effect.result_ref, ensure_ascii=False, allow_nan=False),
                    strict=True,
                )
            else:
                draft = await session.scalar(
                    select(Draft)
                    .where(Draft.task_id == claims.task_id)
                    .with_for_update()
                )
                if draft is None:
                    draft = Draft(id=uuid4(), task_id=claims.task_id)
                    session.add(draft)
                    await session.flush()

                try:
                    candidate = CandidateOrder.model_validate(
                        value.candidate_order.model_dump(mode="python")
                    )
                    evidence_refs = tuple(
                        FieldEvidenceInput.model_validate(
                            item.model_dump(mode="python")
                        )
                        for item in value.evidence_refs
                    )
                    candidate = candidate.model_copy(
                        update={"evidence": (*candidate.evidence, *evidence_refs)}
                    )
                    saved = await save_candidate(
                        session,
                        draft_id=draft.id,
                        expected_version=value.expected_version,
                        candidate=candidate,
                        actor=actor,
                    )
                except DraftConflictError as exc:
                    raise _error(exc.code, str(exc)) from exc
                except ValueError as exc:
                    raise _error(
                        "TOOL_INPUT_INVALID", "草稿字段不符合业务类型约束"
                    ) from exc

                data = SavedDraftData(
                    draft_id=saved.draft_id,
                    version=saved.version,
                    content_hash=saved.content_hash,
                    is_validated=saved.is_validated,
                    total_amount=(
                        format(saved.total_amount, ".2f")
                        if saved.total_amount is not None
                        else None
                    ),
                    issues=tuple(
                        ToolIssue(
                            code=issue.code,
                            field=issue.field,
                            message=issue.message,
                        )
                        for issue in saved.issues
                    ),
                )
                output = SaveOrderDraftOutput(data=data, observed_at=datetime.now(UTC))
                session.add(
                    MCPToolEffect(
                        operation_id=claims.operation_id,
                        task_id=claims.task_id,
                        tool_name="save_order_draft",
                        args_hash=args_hash,
                        result_ref=output.model_dump(mode="json"),
                    )
                )
                await record_audit_event(
                    session,
                    action="MCP草稿保存",
                    result="SUCCEEDED",
                    actor=actor,
                    task_id=claims.task_id,
                    object_ref={"draft_id": saved.draft_id},
                    after_ref={
                        "version": saved.version,
                        "content_hash": saved.content_hash,
                        "is_validated": saved.is_validated,
                        "operation_id": claims.operation_id,
                    },
                    input_value=arguments,
                )

            span = await start_span(
                session,
                name="save_order_draft",
                kind="tool",
                execution_mode="live",
                task_id=claims.task_id,
                actor=actor,
                trace_id=claims.trace_id,
                protocol="MCP/Streamable HTTP",
                started_at=started_at,
                input_ref={
                    "input_hash": args_hash,
                    "operation_id": claims.operation_id,
                    "tool_call_id": claims.tool_call_id,
                    "mcp_request_id": ctx.request_id,
                },
            )
            await finish_span(
                session,
                span_id=span.id,
                status="OK",
                duration_ms=max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
                output_ref={
                    "output_hash": input_hash(output.model_dump(mode="json")),
                    "draft_id": output.data.draft_id,
                    "version": output.data.version,
                    "operation_id": claims.operation_id,
                },
            )
            if effect is None:
                await append_task_event(
                    session,
                    task_id=claims.task_id,
                    event_type="draft.saved",
                    span_id=span.id,
                    payload={
                        "draft_id": output.data.draft_id,
                        "version": output.data.version,
                        "content_hash": output.data.content_hash,
                        "is_validated": output.data.is_validated,
                    },
                )
            return output
    except ToolError:
        raise
    except Exception as exc:
        raise _error(
            "MCP_DRAFT_SAVE_UNAVAILABLE",
            "草稿保存事务失败，结果未确认",
            retryable=True,
        ) from exc


async def save_order_draft(
    task_id: Any = None,
    expected_version: Any = None,
    candidate_order: Any = None,
    evidence_refs: Any = None,
    ctx: Context[Any, Any] = None,
) -> SaveOrderDraftOutput:
    """保存当前可信任务的草稿候选；不能批准或创建正式订单。"""
    return await _execute_save_draft(ctx)


@mcp_server.tool()
def health_check() -> dict[str, str]:
    """报告 MCP 传输服务状态；此工具不查询或修改业务数据。"""
    return {"service": "tradepilot-mcp", "status": "ok"}


# MCP SDK 参数模型适配 Python 函数签名；StrictSchemaMCPServer 通过公开的
# list_tools 扩展点发布精确契约，服务端调用时还会重新校验原始参数。
TOOL_HANDLERS = {
    "query_customer": query_customer,
    "match_material": match_material,
    "get_contract": get_contract,
    "check_contract_balance": check_contract_balance,
    "save_order_draft": save_order_draft,
}
for _handler in TOOL_HANDLERS.values():
    mcp_server.add_tool(_handler, structured_output=True)


def run_server() -> None:
    from mcp.server.transport_security import TransportSecuritySettings

    mcp_server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                f"localhost:{settings.mcp_port}",
                f"127.0.0.1:{settings.mcp_port}",
                f"mcp:{settings.mcp_port}",
            ],
            allowed_origins=[],
        ),
    )
