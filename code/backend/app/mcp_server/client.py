"""官方 MCP Streamable HTTP 客户端适配器及错误/Schema 归一化。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from app.mcp_server.context import CONTEXT_HEADER
from app.mcp_server.contracts import (
    CheckContractBalanceInput,
    CheckContractBalanceOutput,
    GetContractInput,
    GetContractOutput,
    MatchMaterialInput,
    MatchMaterialOutput,
    MCPCallError,
    MCPErrorPayload,
    MCPUnavailable,
    QueryCustomerInput,
    QueryCustomerOutput,
    SaveOrderDraftInput,
    SaveOrderDraftOutput,
    StrictModel,
)

INPUT_SCHEMAS: dict[str, type[StrictModel]] = {
    "query_customer": QueryCustomerInput,
    "match_material": MatchMaterialInput,
    "get_contract": GetContractInput,
    "check_contract_balance": CheckContractBalanceInput,
    "save_order_draft": SaveOrderDraftInput,
}
OUTPUT_SCHEMAS: dict[str, type[StrictModel]] = {
    "query_customer": QueryCustomerOutput,
    "match_material": MatchMaterialOutput,
    "get_contract": GetContractOutput,
    "check_contract_balance": CheckContractBalanceOutput,
    "save_order_draft": SaveOrderDraftOutput,
}


def _validate(model: type[StrictModel], value: object) -> StrictModel:
    try:
        return model.model_validate_json(
            json.dumps(value, ensure_ascii=False, allow_nan=False), strict=True
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise MCPCallError(
            MCPErrorPayload(
                code="TOOL_INPUT_INVALID",
                message="工具输入不符合严格 Schema",
                retryable=False,
            )
        ) from exc


def _error_payload(contents: Sequence[Any]) -> MCPErrorPayload:
    for content in contents:
        text = getattr(content, "text", None)
        if not isinstance(text, str):
            continue
        start = text.find("{")
        while start >= 0:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[start:])
                return MCPErrorPayload.model_validate(value, strict=True)
            except (json.JSONDecodeError, ValidationError):
                start = text.find("{", start + 1)
    return MCPErrorPayload(
        code="MCP_TOOL_ERROR",
        message="MCP 服务拒绝了本次工具调用",
        retryable=False,
    )


class MCPToolClient:
    """每次真实调用通过官方 SDK 建立带短期身份头的 Streamable HTTP 会话。"""

    def __init__(self, url: str, *, timeout_seconds: float = 10.0):
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def list_tools(self, *, context_token: str | None = None) -> tuple[Any, ...]:
        result = await self._request("list", context_token=context_token)
        return tuple(result)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context_token: str,
    ) -> StrictModel:
        input_schema = INPUT_SCHEMAS.get(name)
        output_schema = OUTPUT_SCHEMAS.get(name)
        if input_schema is None or output_schema is None:
            raise MCPCallError(
                MCPErrorPayload(
                    code="MCP_TOOL_NOT_ALLOWED",
                    message="工具不在应用白名单中",
                    retryable=False,
                )
            )
        validated = _validate(input_schema, arguments)
        encoded = json.loads(validated.model_dump_json(exclude_none=True))
        result = await self._request(
            "call",
            context_token=context_token,
            name=name,
            arguments=encoded,
        )
        if result.is_error:
            raise MCPCallError(_error_payload(result.content))
        structured = result.structured_content
        if structured is None:
            raise MCPCallError(
                MCPErrorPayload(
                    code="TOOL_OUTPUT_INVALID",
                    message="MCP 工具没有返回结构化输出",
                    retryable=False,
                )
            )
        try:
            return output_schema.model_validate_json(
                json.dumps(structured, ensure_ascii=False, allow_nan=False), strict=True
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise MCPCallError(
                MCPErrorPayload(
                    code="TOOL_OUTPUT_INVALID",
                    message="MCP 工具输出不符合声明的 Schema",
                    retryable=False,
                )
            ) from exc

    async def health_check(self) -> dict[str, str]:
        result = await self._request("health")
        if result.is_error or result.structured_content != {
            "service": "tradepilot-mcp",
            "status": "ok",
        }:
            raise MCPCallError(
                MCPErrorPayload(
                    code="MCP_HEALTH_INVALID",
                    message="MCP 健康探针返回不符合约定",
                    retryable=True,
                )
            )
        return result.structured_content

    async def _request(
        self,
        action: str,
        *,
        context_token: str | None = None,
        name: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        headers = {CONTEXT_HEADER: context_token} if context_token else {}
        try:
            async with (
                httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(self.timeout_seconds),
                ) as http,
                Client(streamable_http_client(self.url, http_client=http)) as client,
            ):
                if action == "list":
                    return (await client.list_tools()).tools
                if action == "health":
                    return await client.call_tool("health_check", {})
                assert name is not None and arguments is not None
                return await client.call_tool(name, arguments)
        except (MCPCallError, MCPUnavailable):
            raise
        except Exception as exc:
            raise MCPUnavailable("MCP 服务连接失败或传输中断") from exc
