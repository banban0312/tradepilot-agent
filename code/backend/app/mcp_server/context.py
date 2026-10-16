"""短期签名的内部 MCP 身份上下文；从不接受模型参数中的身份声明。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.auth.context import (
    BUSINESS_APPROVER,
    BUSINESS_OPERATOR,
    AuthContext,
    require_department_scope,
)
from app.config import settings
from app.tasks.service import TaskExecutionLease

CONTEXT_HEADER = "X-TradePilot-Context"
ISSUER = "tradepilot-worker"
AUDIENCE = "tradepilot-mcp"
MAX_CONTEXT_TTL_SECONDS = 60
MCP_TOOL_ALLOWLIST = frozenset(
    {
        "query_customer",
        "match_material",
        "get_contract",
        "check_contract_balance",
        "save_order_draft",
    }
)


class ToolContextClaims(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    issuer: str = ISSUER
    audience: str = AUDIENCE
    actor_id: UUID
    department_id: UUID
    task_id: UUID
    execution_epoch: int = Field(ge=1)
    allowed_tools: frozenset[str] = Field(min_length=1, max_length=5)
    operation_id: UUID
    trace_id: UUID
    tool_call_id: str = Field(min_length=1, max_length=128)
    issued_at: datetime
    expires_at: datetime


class MCPContextError(ValueError):
    """签名上下文无效，附有可安全返回的错误码。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _key() -> bytes:
    configured = settings.mcp_context_signing_key
    value = configured.get_secret_value().encode("utf-8") if configured else b""
    if len(value) < 32:
        raise MCPContextError(
            "MCP_CONTEXT_UNAVAILABLE", "MCP 内部签名密钥未配置或长度不足"
        )
    return value


def _canonical(claims: ToolContextClaims) -> bytes:
    return json.dumps(
        claims.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def issue_tool_context(
    *,
    actor: AuthContext,
    department_id: UUID,
    lease: TaskExecutionLease,
    allowed_tools: frozenset[str],
    tool_call_id: str,
    operation_id: UUID | None = None,
    trace_id: UUID | None = None,
    now: datetime | None = None,
    ttl: timedelta = timedelta(seconds=45),
) -> str:
    """仅从已验证会话和实际租约签发上下文，有效期最多 60 秒。"""
    if ttl <= timedelta(0) or ttl > timedelta(seconds=MAX_CONTEXT_TTL_SECONDS):
        raise ValueError("MCP 上下文有效期必须在 1 至 60 秒之间")
    require_department_scope(actor, department_id)
    if not actor.roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise ValueError("当前账号不具备业务操作角色")
    if not allowed_tools or not allowed_tools <= MCP_TOOL_ALLOWLIST:
        raise ValueError("MCP 上下文只能授权已注册的业务工具")
    if lease.task_id is None or lease.epoch < 1:
        raise ValueError("MCP 上下文必须绑定有效任务租约")
    issued = now or datetime.now(UTC)
    claims = ToolContextClaims(
        actor_id=actor.user_id,
        department_id=department_id,
        task_id=lease.task_id,
        execution_epoch=lease.epoch,
        allowed_tools=allowed_tools,
        operation_id=operation_id or uuid4(),
        trace_id=trace_id or uuid4(),
        tool_call_id=tool_call_id,
        issued_at=issued,
        expires_at=issued + ttl,
    )
    payload = _encode(_canonical(claims))
    signature = _encode(hmac.digest(_key(), payload.encode("ascii"), "sha256"))
    return f"{payload}.{signature}"


def verify_tool_context(
    token: str, *, now: datetime | None = None
) -> ToolContextClaims:
    if not token or len(token) > 8192 or token.count(".") != 1:
        raise MCPContextError("MCP_CONTEXT_INVALID", "MCP 调用上下文无效")
    payload, signature = token.split(".", 1)
    try:
        expected = hmac.digest(_key(), payload.encode("ascii"), "sha256")
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, supplied):
            raise MCPContextError("MCP_CONTEXT_INVALID", "MCP 调用上下文签名无效")
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = ToolContextClaims.model_validate_json(raw, strict=True)
    except MCPContextError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise MCPContextError("MCP_CONTEXT_INVALID", "MCP 调用上下文格式无效") from exc
    current = now or datetime.now(UTC)
    if claims.issuer != ISSUER or claims.audience != AUDIENCE:
        raise MCPContextError("MCP_CONTEXT_INVALID", "MCP 调用上下文目标无效")
    if claims.issued_at > current + timedelta(seconds=5):
        raise MCPContextError("MCP_CONTEXT_INVALID", "MCP 调用上下文签发时间无效")
    if claims.expires_at <= current:
        raise MCPContextError("MCP_CONTEXT_EXPIRED", "MCP 调用上下文已过期")
    if claims.expires_at - claims.issued_at > timedelta(
        seconds=MAX_CONTEXT_TTL_SECONDS
    ):
        raise MCPContextError("MCP_CONTEXT_INVALID", "MCP 调用上下文有效期超限")
    return claims


def token_fingerprint(token: str) -> str:
    """仅供测试/诊断使用的不可逆指纹；禁止记录原始 token。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
