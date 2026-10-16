"""合同、主数据、确定性字段规则及只读额度预检。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import AuthContext, require_business_access
from app.models import (
    Contract,
    ContractBalance,
    ContractLine,
    ContractLineBalance,
    Customer,
    Material,
    MaterialAlias,
    Project,
)

from .types import (
    CandidateOrder,
    CandidateOrderItem,
    DraftValidationIssue,
    FieldEvidenceInput,
    FieldSource,
    ValidatedDraft,
    candidate_amounts,
    line_amount,
    validate_candidate,
)


@dataclass(frozen=True, slots=True)
class MaterialCandidate:
    material_id: UUID
    code: str
    name: str
    specification: str
    unit: str


@dataclass(frozen=True, slots=True)
class ContractLinePreview:
    line_no: int
    contract_line_id: UUID | None
    material_id: UUID | None
    unit: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    remaining_quantity: Decimal | None
    quantity_limited: bool


@dataclass(frozen=True, slots=True)
class RulePrecheckResult:
    contract_id: UUID
    department_id: UUID
    prechecked_at: datetime
    business_date: date
    contract_amount: Decimal
    occupied_amount: Decimal | None
    remaining_amount: Decimal | None
    occupied_order_count: int | None
    order_count_limited: bool
    requested_amount: Decimal | None
    excess_amount: Decimal
    candidate: CandidateOrder
    validated_draft: ValidatedDraft | None
    line_previews: tuple[ContractLinePreview, ...]
    material_candidates: tuple[tuple[MaterialCandidate, ...], ...]
    issues: tuple[DraftValidationIssue, ...]

    @property
    def can_proceed_to_approval(self) -> bool:
        return self.validated_draft is not None and not self.issues


def _issue(code: str, field: str, message: str) -> DraftValidationIssue:
    return DraftValidationIssue(code=code, field=field, message=message)


def _normalize(value: str) -> str:
    return re.sub(r"[\s\-_/·（）()]+", "", value).casefold()


async def _suggest_materials(
    session: AsyncSession, item: CandidateOrderItem
) -> tuple[MaterialCandidate, ...]:
    """只返回匹配线索，不自动把模糊规格选成某个物料。"""
    materials = await session.scalars(
        select(Material).where(Material.active.is_(True)).order_by(Material.code)
    )
    rows = list(materials)
    if not rows:
        return ()

    raw_name = _normalize(item.raw_name or "")
    specification = _normalize(item.specification or "")
    aliases = await session.scalars(select(MaterialAlias))
    alias_ids = {
        alias.material_id
        for alias in aliases
        if raw_name and _normalize(alias.alias) == raw_name
    }
    exact = [
        material
        for material in rows
        if material.id in alias_ids
        or (raw_name and _normalize(material.name) == raw_name)
        or (specification and _normalize(material.specification) == specification)
    ]
    if not exact and raw_name:
        exact = [material for material in rows if _normalize(material.name) in raw_name]
    if not exact:
        grade_terms = set(
            re.findall(
                r"[a-z]{1,8}\d+",
                f"{item.raw_name or ''} {item.specification or ''}",
                re.IGNORECASE,
            )
        )
        if grade_terms:
            exact = [
                material
                for material in rows
                if any(
                    term.casefold() in _normalize(material.specification)
                    for term in grade_terms
                )
            ]
    return tuple(
        MaterialCandidate(
            material_id=material.id,
            code=material.code,
            name=material.name,
            specification=material.specification,
            unit=material.unit,
        )
        for material in exact[:10]
    )


async def precheck_candidate(
    session: AsyncSession,
    *,
    candidate: CandidateOrder,
    actor: AuthContext,
    business_date: date,
) -> RulePrecheckResult:
    """基于当前 PostgreSQL 主数据做只读预检；不锁定或写入任何额度。"""
    if candidate.contract_id is None:
        raise ValueError("合同 ID 缺失，无法执行合同规则预检")

    contract = await session.get(Contract, candidate.contract_id)
    if contract is None:
        # 不存在与无权访问使用相同的外部语义，避免泄露跨部门合同。
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "资源不存在"},
        )
    require_business_access(actor, contract.department_id)

    issues: list[DraftValidationIssue] = []
    if contract.type != candidate.order_type:
        issues.append(
            _issue("CONTRACT_TYPE_MISMATCH", "order_type", "合同方向与订单方向不一致")
        )
    if contract.status != "ACTIVE":
        issues.append(
            _issue("CONTRACT_INACTIVE", "contract_id", "合同已停用，不能用于新订单")
        )
    if not (contract.valid_from <= business_date <= contract.valid_to):
        issues.append(
            _issue(
                "CONTRACT_OUT_OF_TERM",
                "contract_id",
                "合同在当前业务日期不处于有效期内",
            )
        )
    if candidate.delivery_date is not None:
        if candidate.delivery_date < business_date:
            issues.append(
                _issue("DELIVERY_DATE_IN_PAST", "delivery_date", "交期早于当前业务日期")
            )
        if not (contract.valid_from <= candidate.delivery_date <= contract.valid_to):
            issues.append(
                _issue(
                    "DELIVERY_OUTSIDE_CONTRACT_TERM",
                    "delivery_date",
                    "交期不在合同有效期内",
                )
            )
    if candidate.currency != contract.currency:
        issues.append(
            _issue("CURRENCY_MISMATCH", "currency", "草稿币种与合同币种不一致")
        )

    customer = (
        await session.get(Customer, candidate.customer_id)
        if candidate.customer_id
        else None
    )
    if candidate.customer_id != contract.customer_id:
        issues.append(
            _issue(
                "CONTRACT_CUSTOMER_MISMATCH", "customer_id", "客户与合同签约客户不一致"
            )
        )
    if (
        customer is None
        or not customer.active
        or customer.department_id != contract.department_id
    ):
        issues.append(
            _issue(
                "CUSTOMER_UNAVAILABLE",
                "customer_id",
                "客户不存在、已停用或不属于合同部门",
            )
        )

    project = (
        await session.get(Project, candidate.project_id)
        if candidate.project_id
        else None
    )
    if candidate.project_id != contract.project_id:
        issues.append(
            _issue(
                "CONTRACT_PROJECT_MISMATCH", "project_id", "项目与合同关联项目不一致"
            )
        )
    if project is None or project.department_id != contract.department_id:
        issues.append(
            _issue("PROJECT_UNAVAILABLE", "project_id", "项目不存在或不属于合同部门")
        )

    effective_items: list[CandidateOrderItem] = []
    effective_evidence = list(candidate.evidence)
    line_previews: list[ContractLinePreview] = []
    suggestions: list[tuple[MaterialCandidate, ...]] = []
    line_quantity: dict[UUID, Decimal] = {}
    item_lines: dict[int, ContractLine] = {}

    for index, item in enumerate(candidate.items):
        path = f"items[{index}]"
        material = (
            await session.get(Material, item.material_id) if item.material_id else None
        )
        suggestion = (
            await _suggest_materials(session, item) if item.material_id is None else ()
        )
        suggestions.append(suggestion)
        if item.material_id is None:
            issues.append(
                _issue(
                    "MATERIAL_CONFIRMATION_REQUIRED",
                    f"{path}.material_id",
                    "请从标准物料候选中确认物料；未确认规格前不会自动补全直径或型号",
                )
            )
        elif material is None or not material.active:
            issues.append(
                _issue(
                    "MATERIAL_UNAVAILABLE", f"{path}.material_id", "物料不存在或已停用"
                )
            )
        elif item.unit != material.unit:
            issues.append(
                _issue(
                    "MATERIAL_UNIT_MISMATCH",
                    f"{path}.unit",
                    "订单单位与标准物料单位不兼容",
                )
            )
        elif item.specification and _normalize(item.specification) != _normalize(
            material.specification
        ):
            issues.append(
                _issue(
                    "MATERIAL_SPECIFICATION_MISMATCH",
                    f"{path}.specification",
                    "物料规格与所选标准物料不一致，请确认规格",
                )
            )
        elif item.raw_name and _normalize(item.raw_name) not in {
            _normalize(material.name),
            *(
                _normalize(alias.alias)
                for alias in await session.scalars(
                    select(MaterialAlias).where(
                        MaterialAlias.material_id == material.id
                    )
                )
            ),
        }:
            issues.append(
                _issue(
                    "MATERIAL_NAME_MISMATCH",
                    f"{path}.raw_name",
                    "物料名称与所选标准物料不匹配",
                )
            )

        contract_line = None
        if material is not None and material.active:
            contract_line = await session.scalar(
                select(ContractLine).where(
                    ContractLine.contract_id == contract.id,
                    ContractLine.material_id == material.id,
                    ContractLine.unit == item.unit,
                )
            )
            if contract_line is None:
                other_unit = await session.scalar(
                    select(ContractLine).where(
                        ContractLine.contract_id == contract.id,
                        ContractLine.material_id == material.id,
                    )
                )
                code = (
                    "CONTRACT_LINE_UNIT_MISMATCH"
                    if other_unit
                    else "MATERIAL_NOT_IN_CONTRACT"
                )
                message = (
                    "合同物料行单位不兼容" if other_unit else "所选物料未配置在该合同中"
                )
                issues.append(_issue(code, f"{path}.material_id", message))
            else:
                item_lines[index] = contract_line
                if item.quantity is not None:
                    line_quantity[contract_line.id] = (
                        line_quantity.get(contract_line.id, Decimal(0)) + item.quantity
                    )

        price = item.unit_price
        if contract_line is not None:
            if price is None:
                price = contract_line.unit_price
                if price is not None:
                    effective_evidence.append(
                        FieldEvidenceInput(
                            field_path=f"{path}.unit_price",
                            source_type=FieldSource.MASTER_DATA,
                            locator={"contract_line_id": str(contract_line.id)},
                            accepted_value=format(price, "f"),
                        )
                    )
            elif (
                contract_line.unit_price is not None
                and price != contract_line.unit_price
            ):
                issues.append(
                    _issue(
                        "CONTRACT_PRICE_MISMATCH",
                        f"{path}.unit_price",
                        "含税单价与合同约定价格不一致",
                    )
                )
            if price is None or price <= 0:
                issues.append(
                    _issue(
                        "UNIT_PRICE_REQUIRED",
                        f"{path}.unit_price",
                        "合同未配置单价，请补充有效含税单价",
                    )
                )

        effective_items.append(item.model_copy(update={"unit_price": price}))

    for index, item in enumerate(candidate.items):
        path = f"items[{index}]"
        line = item_lines.get(index)
        amount = None
        if item.quantity is not None and effective_items[index].unit_price is not None:
            amount = line_amount(item.quantity, effective_items[index].unit_price)
        remaining_quantity = None
        if line is not None and line.max_quantity is not None:
            balance = await session.get(ContractLineBalance, line.id)
            if balance is None:
                issues.append(
                    _issue(
                        "QUANTITY_BALANCE_UNAVAILABLE",
                        f"{path}.quantity",
                        "合同物料额度账缺失，无法安全预检",
                    )
                )
            else:
                remaining_quantity = line.max_quantity - balance.occupied_quantity
                if line_quantity.get(line.id, Decimal(0)) > remaining_quantity:
                    issues.append(
                        _issue(
                            "QUANTITY_LIMIT_EXCEEDED",
                            f"items[{index}].quantity",
                            "本次数量超过该合同物料剩余数量额度",
                        )
                    )
        line_previews.append(
            ContractLinePreview(
                line_no=index + 1,
                contract_line_id=line.id if line else None,
                material_id=item.material_id,
                unit=item.unit,
                quantity=item.quantity,
                unit_price=effective_items[index].unit_price,
                amount=amount,
                remaining_quantity=remaining_quantity,
                quantity_limited=bool(
                    line is not None and line.max_quantity is not None
                ),
            )
        )

    effective_candidate = candidate.model_copy(
        update={"items": tuple(effective_items), "evidence": tuple(effective_evidence)}
    )
    validated = None
    requested_amount = None
    excess_amount = Decimal("0.00")
    try:
        _, requested_amount = candidate_amounts(effective_candidate)
    except ValueError:
        issues.append(
            _issue("AMOUNT_OUT_OF_RANGE", "total_amount", "订单金额超出可支持范围")
        )
    try:
        validated = validate_candidate(effective_candidate)
    except ValueError as error:
        if hasattr(error, "issues"):
            issues.extend(error.issues)  # type: ignore[attr-defined]
        elif "numeric(20,2)" in str(error):
            if "AMOUNT_OUT_OF_RANGE" not in {issue.code for issue in issues}:
                issues.append(
                    _issue(
                        "AMOUNT_OUT_OF_RANGE", "total_amount", "订单金额超出可支持范围"
                    )
                )
        else:
            raise

    balance = await session.get(ContractBalance, contract.id)
    if balance is None:
        occupied_amount = None
        remaining_amount = None
        issues.append(
            _issue(
                "CONTRACT_BALANCE_UNAVAILABLE",
                "contract_id",
                "合同余额账缺失，无法安全预检",
            )
        )
    else:
        occupied_amount = balance.occupied_amount
        remaining_amount = contract.total_amount - occupied_amount
        if remaining_amount < 0:
            issues.append(
                _issue(
                    "CONTRACT_BALANCE_INCONSISTENT",
                    "contract_id",
                    "合同已占用金额超过合同总额，请核对额度账",
                )
            )
    if (
        requested_amount is not None
        and remaining_amount is not None
        and requested_amount > remaining_amount
    ):
        excess_amount = requested_amount - remaining_amount
        issues.append(
            _issue(
                "CONTRACT_AMOUNT_EXCEEDED",
                "total_amount",
                f"超出合同剩余额度 {excess_amount:.2f} 元",
            )
        )

    if validated is not None and issues:
        validated = None
    return RulePrecheckResult(
        contract_id=contract.id,
        department_id=contract.department_id,
        prechecked_at=datetime.now(UTC),
        business_date=business_date,
        contract_amount=contract.total_amount,
        occupied_amount=occupied_amount,
        remaining_amount=remaining_amount,
        occupied_order_count=balance.occupied_order_count if balance else None,
        order_count_limited=False,
        requested_amount=requested_amount,
        excess_amount=excess_amount,
        candidate=effective_candidate,
        validated_draft=validated,
        line_previews=tuple(line_previews),
        material_candidates=tuple(suggestions),
        issues=tuple(issues),
    )
