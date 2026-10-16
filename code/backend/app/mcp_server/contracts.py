"""MCP 工具的严格输入、输出和业务错误契约。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MoneyString = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=4,
        max_length=21,
        pattern=r"^(0|[1-9][0-9]{0,17})\.[0-9]{2}$",
    ),
]
PositiveMoneyString = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=4,
        max_length=21,
        pattern=r"^(0\.(?:0[1-9]|[1-9][0-9])|[1-9][0-9]{0,17}\.[0-9]{2})$",
    ),
]
DecimalValueString = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=24,
        pattern=r"^(0|[1-9][0-9]{0,17})(\.[0-9]{1,6})?$",
    ),
]
SignedMoneyString = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=4,
        max_length=22,
        pattern=r"^-?(0|[1-9][0-9]{0,17})\.[0-9]{2}$",
    ),
]
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
MAX_TOOL_ARGUMENT_BYTES = 262_144


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class QueryCustomerInput(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    code: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def require_search_key(self) -> QueryCustomerInput:
        if not (self.name and self.name.strip()) and not (
            self.code and self.code.strip()
        ):
            raise ValueError("name 和 code 至少提供一项")
        return self


class MatchMaterialInput(StrictModel):
    raw_name: ShortText
    specification: str | None = Field(default=None, max_length=256)
    unit: str | None = Field(default=None, max_length=32)

    @field_validator("raw_name")
    @classmethod
    def require_nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_name 不能为空")
        return value


class GetContractInput(StrictModel):
    contract_number: ShortText
    order_type: Literal["SALES", "PURCHASE"]

    @field_validator("contract_number")
    @classmethod
    def trim_contract_number(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("contract_number 不能为空")
        return value


class CheckContractBalanceInput(StrictModel):
    contract_id: UUID
    draft_id: UUID
    draft_version: int = Field(ge=1)
    order_amount: PositiveMoneyString

    @field_validator("order_amount")
    @classmethod
    def finite_amount(cls, value: str) -> str:
        number = Decimal(value)
        if not number.is_finite() or number <= 0:
            raise ValueError("order_amount 必须为正的有限金额")
        return value


class EvidenceInput(StrictModel):
    field_path: ShortText
    source_type: Literal[
        "USER_TEXT",
        "FILE_PAGE",
        "FILE_CELL",
        "MASTER_DATA",
        "USER_EDIT",
        "SYSTEM_COMPUTED",
    ]
    artifact_id: UUID | None = None
    message_id: UUID | None = None
    locator: dict[str, Any] | None = None
    raw_value: Any = None
    accepted_value: Any = None


class CandidateOrderItemInput(StrictModel):
    material_id: UUID | None = None
    raw_name: str | None = Field(default=None, max_length=512)
    specification: str | None = Field(default=None, max_length=256)
    quantity: str | None = Field(default=None, max_length=24)
    unit: str | None = Field(default=None, max_length=32)
    unit_price: str | None = Field(default=None, max_length=24)

    @field_validator("quantity", "unit_price")
    @classmethod
    def decimal_values_are_strings(cls, value: str | None) -> str | None:
        if value is not None:
            number = Decimal(value)
            if not number.is_finite() or number < 0 or number >= Decimal(1000000000000):
                raise ValueError("数量和单价必须为有限的非负十进制字符串")
            if number != 0 and number.normalize().as_tuple().exponent < -6:
                raise ValueError("数量和单价最多支持 6 位小数")
        return value


class CandidateOrderInput(StrictModel):
    order_type: Literal["SALES", "PURCHASE"] | None = None
    contract_id: UUID | None = None
    customer_id: UUID | None = None
    project_id: UUID | None = None
    currency: Literal["CNY"] = "CNY"
    delivery_date: date | None = None
    address: str | None = Field(default=None, max_length=512)
    contact: str | None = Field(default=None, max_length=128)
    business_fields: dict[str, Any] = Field(default_factory=dict)
    items: tuple[CandidateOrderItemInput, ...] = Field(default=(), max_length=500)
    evidence: tuple[EvidenceInput, ...] = Field(default=(), max_length=1000)

    @field_validator("business_fields")
    @classmethod
    def reject_server_identity(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "actor_id",
            "owner_id",
            "user_id",
            "department_id",
            "created_by",
            "changed_by",
            "confirmed_by",
            "approver_id",
            "approved_by",
            "approval_id",
            "order_id",
            "task_id",
            "draft_id",
            "current_version",
            "lock_state",
            "order_state",
        }

        def inspect(node: Any) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    normalized = str(key).casefold()
                    if normalized in reserved or normalized.startswith(
                        ("approval_", "approved_")
                    ):
                        raise ValueError("business_fields 不能包含服务端身份或审批字段")
                    inspect(child)
            elif isinstance(node, (list, tuple)):
                for child in node:
                    inspect(child)

        inspect(value)
        return value


class SaveOrderDraftInput(StrictModel):
    task_id: UUID
    expected_version: int = Field(ge=0)
    candidate_order: CandidateOrderInput
    evidence_refs: tuple[EvidenceInput, ...] = Field(max_length=500)


class ToolIssue(StrictModel):
    code: str
    field: str | None = None
    message: str


class ToolEnvelope[T: BaseModel](BaseModel):
    """统一的成功输出；data 由每个工具的响应 Schema 约束。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    data: T
    issues: tuple[ToolIssue, ...] = ()
    source_refs: tuple[str, ...] = ()
    observed_at: datetime
    schema_version: str = "1.0"


class CustomerCandidate(StrictModel):
    id: UUID
    code: str
    name: str
    reason: str


class CustomerQueryData(StrictModel):
    candidates: tuple[CustomerCandidate, ...]


class MaterialCandidate(StrictModel):
    material_id: UUID
    code: str
    name: str
    specification: str | None
    unit: str | None
    reason: str
    requires_confirmation: bool


class MaterialMatchData(StrictModel):
    candidates: tuple[MaterialCandidate, ...]


class ContractLineData(StrictModel):
    material_id: UUID
    material_code: str
    material_name: str
    specification: str
    unit: str
    unit_price: DecimalValueString | None
    max_quantity: DecimalValueString | None


class ContractData(StrictModel):
    id: UUID
    contract_number: str
    order_type: Literal["SALES", "PURCHASE"]
    customer_id: UUID
    customer_name: str
    project_id: UUID
    project_name: str
    delivery_address: str | None
    currency: str
    total: MoneyString
    status: Literal["ACTIVE", "INACTIVE"]
    valid_from: date
    valid_to: date
    lines: tuple[ContractLineData, ...]


class ContractBalanceData(StrictModel):
    total: MoneyString
    occupied: MoneyString
    remaining: SignedMoneyString
    order_amount: MoneyString
    allowed: bool
    checked_at: datetime
    reasons: tuple[str, ...]


class SavedDraftData(StrictModel):
    draft_id: UUID
    version: int = Field(ge=1)
    content_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    issues: tuple[ToolIssue, ...] = ()


QueryCustomerOutput = ToolEnvelope[CustomerQueryData]
MatchMaterialOutput = ToolEnvelope[MaterialMatchData]
GetContractOutput = ToolEnvelope[ContractData]
CheckContractBalanceOutput = ToolEnvelope[ContractBalanceData]
SaveOrderDraftOutput = ToolEnvelope[SavedDraftData]


class MCPErrorPayload(StrictModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class MCPCallError(RuntimeError):
    def __init__(self, payload: MCPErrorPayload):
        self.payload = payload
        super().__init__(payload.message)


class MCPUnavailable(RuntimeError):
    """MCP 传输不可用；调用方不得将其转换成业务成功。"""

    code = "MCP_UNAVAILABLE"


class ToolNotImplemented(RuntimeError):
    """契约已注册，但对应业务 issue 尚未交付。"""

    code = "TOOL_NOT_IMPLEMENTED"


def validate_raw_input(model: type[StrictModel], raw: object) -> StrictModel:
    """从 MCP 原始 JSON 参数再次校验，避免 SDK 参数层忽略额外字段。"""
    import json

    encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_TOOL_ARGUMENT_BYTES:
        raise ValueError("MCP 工具输入超过大小限制")
    return model.model_validate_json(encoded, strict=True)


def normalize_output(model: type[StrictModel], raw: object) -> StrictModel:
    import json

    return model.model_validate_json(
        json.dumps(raw, ensure_ascii=False, allow_nan=False), strict=True
    )
