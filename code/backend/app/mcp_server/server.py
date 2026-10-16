"""严格 MCP 服务；业务处理器在对应数据 issue 完成前明确拒绝执行。"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Tool as MCPTool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import (
    BUSINESS_APPROVER,
    BUSINESS_OPERATOR,
    AuthContext,
    require_business_access,
)
from app.config import settings
from app.database import async_session_factory
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
    SaveOrderDraftInput,
    SaveOrderDraftOutput,
    StrictModel,
    ToolNotImplemented,
    validate_raw_input,
)
from app.models import AgentTask, Department, User, UserDepartmentScope, UserRole
from app.observability.service import finish_span, input_hash, start_span

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
    session: AsyncSession, claims: ToolContextClaims, tool_name: str
) -> AuthContext:
    user = await session.get(User, claims.actor_id)
    department = await session.get(Department, claims.department_id)
    task = await session.get(AgentTask, claims.task_id)
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


async def _trace_unimplemented(
    session_factory: Any,
    *,
    actor: AuthContext,
    claims: ToolContextClaims,
    tool_name: str,
    request_id: str,
    arguments: dict[str, Any],
) -> None:
    """持久记录真实 MCP 输入摘要和明确的未实现结果，不保留原始敏感内容。"""
    started_at = time.monotonic_ns()
    async with session_factory() as session:
        span = await start_span(
            session,
            name=tool_name,
            kind="tool",
            execution_mode="live",
            task_id=claims.task_id,
            actor=actor,
            trace_id=claims.trace_id,
            protocol="MCP/Streamable HTTP",
            input_ref={
                "input_hash": input_hash(arguments),
                "operation_id": claims.operation_id,
                "tool_call_id": claims.tool_call_id,
                "mcp_request_id": request_id,
            },
        )
        span_id = span.id
        await session.commit()
    async with session_factory() as session:
        await finish_span(
            session,
            span_id=span_id,
            status="ERROR",
            duration_ms=max(0, (time.monotonic_ns() - started_at) // 1_000_000),
            output_ref={"code": ToolNotImplemented.code},
            error={"code": ToolNotImplemented.code},
        )
        await session.commit()


async def _not_implemented(
    model: type[StrictModel],
    tool_name: str,
    ctx: Context[Any, Any],
) -> None:
    try:
        raw = _raw_arguments(ctx)
        value = validate_raw_input(model, raw)
    except Exception as exc:
        if isinstance(exc, ToolError):
            raise
        raise _error("TOOL_INPUT_INVALID", "工具输入不符合严格 Schema") from exc
    assert isinstance(value, StrictModel)

    claims = _verified_context(ctx, tool_name)
    if isinstance(value, SaveOrderDraftInput) and value.task_id != claims.task_id:
        raise _error("ACCESS_DENIED", "请求任务与可信任务上下文不一致")
    async with async_session_factory() as session:
        try:
            actor = await _reauthorize(session, claims, tool_name)
        except ToolError:
            raise
        except Exception as exc:
            raise _error(
                "MCP_AUTHORIZATION_UNAVAILABLE",
                "无法重新验证 MCP 调用权限",
                retryable=True,
            ) from exc

    arguments = value.model_dump(mode="json")
    await _trace_unimplemented(
        async_session_factory,
        actor=actor,
        claims=claims,
        tool_name=tool_name,
        request_id=ctx.request_id,
        arguments=arguments,
    )
    raise _error("TOOL_NOT_IMPLEMENTED", "工具契约已注册，业务能力将在后续 issue 交付")


async def query_customer(
    name: Any = None,
    code: Any = None,
    limit: Any = None,
    ctx: Context[Any, Any] = None,
) -> QueryCustomerOutput:
    """按当前任务部门查询客户候选；本工具仅支持只读查询。"""
    await _not_implemented(QueryCustomerInput, "query_customer", ctx)
    raise AssertionError("不可达")


async def match_material(
    raw_name: Any = None,
    specification: Any = None,
    unit: Any = None,
    ctx: Context[Any, Any] = None,
) -> MatchMaterialOutput:
    """按名称、规格和单位匹配标准物料；候选确认由业务流程处理。"""
    await _not_implemented(MatchMaterialInput, "match_material", ctx)
    raise AssertionError("不可达")


async def get_contract(
    contract_number: Any = None,
    order_type: Any = None,
    ctx: Context[Any, Any] = None,
) -> GetContractOutput:
    """查询当前任务部门内合同信息；不创建订单或占用额度。"""
    await _not_implemented(GetContractInput, "get_contract", ctx)
    raise AssertionError("不可达")


async def check_contract_balance(
    contract_id: Any = None,
    draft_id: Any = None,
    draft_version: Any = None,
    order_amount: Any = None,
    ctx: Context[Any, Any] = None,
) -> CheckContractBalanceOutput:
    """核验合同额度的只读契约；提交时仍需服务端重新核验。"""
    await _not_implemented(CheckContractBalanceInput, "check_contract_balance", ctx)
    raise AssertionError("不可达")


async def save_order_draft(
    task_id: Any = None,
    expected_version: Any = None,
    candidate_order: Any = None,
    evidence_refs: Any = None,
    ctx: Context[Any, Any] = None,
) -> SaveOrderDraftOutput:
    """保存当前可信任务的草稿候选；不能批准或创建正式订单。"""
    await _not_implemented(SaveOrderDraftInput, "save_order_draft", ctx)
    raise AssertionError("不可达")


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
