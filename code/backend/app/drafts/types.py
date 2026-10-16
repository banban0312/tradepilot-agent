"""草稿输入、可审批类型和金额规则。"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

OrderType = Literal["SALES", "PURCHASE"]
QUANTITY_SCALE = Decimal("0.000001")
MONEY_SCALE = Decimal("0.01")


def _decimal_input(value: Any, *, field_name: str) -> Decimal | None:
    """只接受十进制字符串或 Decimal，避免 JSON 浮点数先丢失精度。"""
    if value is None:
        return None
    if not isinstance(value, (str, Decimal)):
        raise ValueError(f"{field_name} 必须使用十进制字符串")  # noqa: TRY004 - Pydantic v2 requires ValueError here
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 不是有效的十进制数") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} 必须是有限数值")
    if number < 0:
        raise ValueError(f"{field_name} 不能为负数")
    if number >= Decimal(1000000000000):
        raise ValueError(f"{field_name} 超出 numeric(18,6) 范围")
    if number != 0 and number.normalize().as_tuple().exponent < -6:
        raise ValueError(f"{field_name} 最多支持 6 位小数")
    return number


OptionalQuantity = Annotated[
    Decimal | None,
    BeforeValidator(lambda value: _decimal_input(value, field_name="数量")),
]
OptionalPrice = Annotated[
    Decimal | None,
    BeforeValidator(lambda value: _decimal_input(value, field_name="单价")),
]


class FieldSource(StrEnum):
    USER_TEXT = "USER_TEXT"
    FILE_PAGE = "FILE_PAGE"
    FILE_CELL = "FILE_CELL"
    MASTER_DATA = "MASTER_DATA"
    USER_EDIT = "USER_EDIT"
    SYSTEM_COMPUTED = "SYSTEM_COMPUTED"


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateOrderItem(DraftModel):
    material_id: UUID | None = None
    raw_name: str | None = None
    specification: str | None = None
    quantity: OptionalQuantity = None
    unit: str | None = None
    unit_price: OptionalPrice = None


class FieldEvidenceInput(DraftModel):
    """客户端可提供来源和原/确认值，不接受 confirmed_by 等身份字段。"""

    field_path: str = Field(min_length=1, max_length=512)
    source_type: FieldSource
    artifact_id: UUID | None = None
    message_id: UUID | None = None
    locator: dict[str, Any] | None = None
    raw_value: Any = None
    accepted_value: Any = None


class CandidateOrder(DraftModel):
    """可缺字段的提取候选；保存成功不代表具备审批资格。"""

    order_type: OrderType | None = None
    contract_id: UUID | None = None
    customer_id: UUID | None = None
    project_id: UUID | None = None
    currency: Literal["CNY"] = "CNY"
    delivery_date: date | None = None
    address: str | None = None
    contact: str | None = None
    business_fields: dict[str, Any] = Field(default_factory=dict)
    items: tuple[CandidateOrderItem, ...] = ()
    evidence: tuple[FieldEvidenceInput, ...] = ()

    @field_validator("business_fields")
    @classmethod
    def reject_server_owned_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        """扩展业务字段仍不能承载由服务端管理的身份、状态或审批事实。"""
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
                for key, nested in node.items():
                    normalized = str(key).casefold()
                    if normalized in reserved or normalized.startswith(
                        ("approval_", "approved_")
                    ):
                        raise ValueError(f"business_fields 不能包含服务端字段：{key}")
                    inspect(nested)
            elif isinstance(node, (list, tuple)):
                for nested in node:
                    inspect(nested)

        inspect(value)
        return value


class DraftValidationIssue(DraftModel):
    code: str
    field: str
    message: str


class ValidatedDraft(DraftModel):
    """满足本阶段结构和金额规则的业务快照，不代表审批或动态额度校验通过。"""

    order_type: OrderType
    contract_id: UUID
    customer_id: UUID
    project_id: UUID
    currency: Literal["CNY"]
    delivery_date: date
    address: str = Field(min_length=1)
    contact: str = Field(min_length=1)
    business_fields: dict[str, Any]
    items: tuple[CandidateOrderItem, ...] = Field(min_length=1)
    total_amount: Decimal


class DraftValidationError(ValueError):
    """候选订单未满足 ValidatedDraft 的必要条件。"""

    def __init__(self, issues: tuple[DraftValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__("候选订单尚不能转为有效草稿")


def line_amount(quantity: Decimal, unit_price: Decimal) -> Decimal:
    """先以高精度计算行金额，再按人民币分位 ROUND_HALF_UP。"""
    with localcontext() as context:
        context.prec = 40
        return (quantity * unit_price).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def candidate_amounts(
    candidate: CandidateOrder,
) -> tuple[tuple[Decimal | None, ...], Decimal | None]:
    amounts = tuple(
        line_amount(item.quantity, item.unit_price)
        if item.quantity is not None and item.unit_price is not None
        else None
        for item in candidate.items
    )
    if not amounts or any(amount is None for amount in amounts):
        return amounts, None
    complete_amounts = tuple(amount for amount in amounts if amount is not None)
    if any(amount >= Decimal(1000000000000000000) for amount in complete_amounts):
        raise ValueError("草稿行金额超出 numeric(20,2) 范围")
    with localcontext() as context:
        context.prec = max(
            40,
            max(len(amount.as_tuple().digits) for amount in complete_amounts)
            + len(str(len(complete_amounts)))
            + 2,
        )
        total = sum(complete_amounts, Decimal(0))
    if total >= Decimal(1000000000000000000):
        raise ValueError("草稿总金额超出 numeric(20,2) 范围")
    return amounts, total.quantize(MONEY_SCALE)


def validate_candidate(candidate: CandidateOrder) -> ValidatedDraft:
    """集中检查必要业务字段，避免用可缺失的候选对象代表可审批草稿。"""
    issues: list[DraftValidationIssue] = []

    def required(value: Any, path: str, label: str) -> None:
        if value is None or value == "":
            issues.append(
                DraftValidationIssue(
                    code="MISSING_FIELD", field=path, message=f"缺少{label}"
                )
            )

    required(candidate.order_type, "order_type", "订单类型")
    required(candidate.contract_id, "contract_id", "合同")
    required(candidate.customer_id, "customer_id", "客户")
    required(candidate.project_id, "project_id", "项目")
    required(candidate.delivery_date, "delivery_date", "交期")
    required(candidate.address, "address", "收货地址")
    required(candidate.contact, "contact", "联系人")
    if not candidate.items:
        issues.append(
            DraftValidationIssue(
                code="MISSING_ITEMS", field="items", message="至少需要一条订单明细"
            )
        )
    for index, item in enumerate(candidate.items, start=1):
        prefix = f"items[{index - 1}]"
        required(item.material_id, f"{prefix}.material_id", f"第 {index} 行标准物料")
        required(item.raw_name, f"{prefix}.raw_name", f"第 {index} 行物料名称")
        required(item.specification, f"{prefix}.specification", f"第 {index} 行规格")
        required(item.unit, f"{prefix}.unit", f"第 {index} 行单位")
        if item.quantity is None or item.quantity <= 0:
            issues.append(
                DraftValidationIssue(
                    code="INVALID_QUANTITY",
                    field=f"{prefix}.quantity",
                    message="数量必须大于 0",
                )
            )
        if item.unit_price is None or item.unit_price <= 0:
            issues.append(
                DraftValidationIssue(
                    code="INVALID_UNIT_PRICE",
                    field=f"{prefix}.unit_price",
                    message="单价必须大于 0",
                )
            )
    if issues:
        raise DraftValidationError(tuple(issues))

    amounts, total = candidate_amounts(candidate)
    assert total is not None and all(amount is not None for amount in amounts)
    if total <= 0:
        raise DraftValidationError(
            (
                DraftValidationIssue(
                    code="INVALID_TOTAL",
                    field="total_amount",
                    message="总金额必须大于 0",
                ),
            )
        )
    try:
        return ValidatedDraft(
            order_type=candidate.order_type,
            contract_id=candidate.contract_id,
            customer_id=candidate.customer_id,
            project_id=candidate.project_id,
            currency=candidate.currency,
            delivery_date=candidate.delivery_date,
            address=candidate.address,
            contact=candidate.contact,
            business_fields=candidate.business_fields,
            items=candidate.items,
            total_amount=total,
        )
    except ValidationError as exc:  # 防止类型契约调整后漏报字段校验问题
        raise DraftValidationError(
            tuple(
                DraftValidationIssue(
                    code="INVALID_FIELD",
                    field=".".join(map(str, error["loc"])),
                    message="字段值无效",
                )
                for error in exc.errors()
            )
        ) from exc
