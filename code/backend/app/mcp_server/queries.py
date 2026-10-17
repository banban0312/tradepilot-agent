"""只读业务查询；每个查询都以可信任务部门限制结果集。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.master_data.normalization import normalize_material_name
from app.mcp_server.contracts import (
    CheckContractBalanceInput,
    CheckContractBalanceOutput,
    ContractBalanceData,
    ContractData,
    ContractLineData,
    CustomerCandidate,
    CustomerQueryData,
    DecimalValueString,
    GetContractInput,
    GetContractOutput,
    MatchMaterialInput,
    MatchMaterialOutput,
    MaterialCandidate,
    MaterialMatchData,
    QueryCustomerInput,
    QueryCustomerOutput,
    SignedMoneyString,
    ToolEnvelope,
    ToolIssue,
)
from app.models import (
    Contract,
    ContractBalance,
    ContractLine,
    Customer,
    Draft,
    DraftRevision,
    Material,
    MaterialAlias,
    Project,
)


class MCPReadError(RuntimeError):
    """有明确业务语义的查询拒绝；不包含被拒绝资源的内容。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _normalize(value: str) -> str:
    """忽略空格和常见标点，但不推断缺失的规格数字。"""
    return normalize_material_name(value)


def _business_date(now: datetime | None = None) -> date:
    """合同有效期按 Spec 配置的业务时区判断，不用容器 UTC 日期代替。"""
    current = now or datetime.now(UTC)
    return current.astimezone(ZoneInfo(settings.business_timezone)).date()


def _like_fragment(value: str) -> str:
    """让客户输入中的 LIKE 元字符按普通字符搜索。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _decimal(value: Decimal | None) -> DecimalValueString | None:
    if value is None:
        return None
    return format(value, "f")


def _money(value: Decimal) -> SignedMoneyString:
    return format(value.quantize(Decimal("0.01")), ".2f")


def _envelope(data, *, issues: tuple[ToolIssue, ...] = (), refs: tuple[str, ...] = ()):
    return ToolEnvelope(
        data=data,
        issues=issues,
        source_refs=refs,
        observed_at=datetime.now(UTC),
        schema_version="1.0",
    )


async def query_customers(
    session: AsyncSession, value: QueryCustomerInput, *, department_id: UUID
) -> QueryCustomerOutput:
    conditions = [Customer.department_id == department_id, Customer.active.is_(True)]
    if value.code:
        code = value.code.strip()
        conditions.append(Customer.code.ilike(f"%{_like_fragment(code)}%", escape="\\"))
    if value.name:
        name = value.name.strip()
        conditions.append(Customer.name.ilike(f"%{_like_fragment(name)}%", escape="\\"))
    rows = list(
        (
            await session.scalars(
                select(Customer)
                .where(*conditions[:2], or_(*conditions[2:]))
                .order_by(Customer.code)
                .limit(value.limit)
            )
        ).all()
    )
    candidates = tuple(
        CustomerCandidate(
            id=row.id,
            code=row.code,
            name=row.name,
            reason=(
                "名称与编码同时匹配"
                if value.name
                and value.code
                and value.name.strip().casefold() in row.name.casefold()
                and value.code.strip().casefold() in row.code.casefold()
                else "编码匹配"
                if value.code and value.code.strip().casefold() in row.code.casefold()
                else "名称匹配"
            ),
        )
        for row in rows
    )
    issues = (
        (
            ToolIssue(
                code="CUSTOMER_NOT_FOUND", message="当前部门未找到符合条件的有效客户"
            ),
        )
        if not candidates
        else ()
    )
    return _envelope(
        CustomerQueryData(candidates=candidates),
        issues=issues,
        refs=tuple(f"customer:{row.id}" for row in rows),
    )


async def match_materials(
    session: AsyncSession, value: MatchMaterialInput
) -> MatchMaterialOutput:
    raw = _normalize(value.raw_name)
    specification = _normalize(value.specification or "")
    unit = _normalize(value.unit or "")
    materials = list(
        (
            await session.scalars(
                select(Material)
                .where(Material.active.is_(True))
                .order_by(Material.code)
            )
        ).all()
    )
    aliases = list((await session.scalars(select(MaterialAlias))).all())
    aliases_by_material: dict[UUID, list[str]] = {}
    for alias in aliases:
        aliases_by_material.setdefault(alias.material_id, []).append(alias.alias)

    candidates: list[tuple[Material, str]] = []
    grade_terms = set(
        re.findall(
            r"[a-z]{1,8}\d+",
            f"{value.raw_name} {value.specification or ''}",
            re.IGNORECASE,
        )
    )
    for material in materials:
        normalized_name = _normalize(material.name)
        normalized_code = _normalize(material.code)
        normalized_spec = _normalize(material.specification)
        normalized_aliases = [
            _normalize(alias) for alias in aliases_by_material.get(material.id, [])
        ]
        name_hit = (
            raw == normalized_name
            or raw == normalized_code
            or raw in normalized_aliases
            or (len(raw) >= 2 and normalized_name in raw)
            or any(len(alias) >= 2 and alias in raw for alias in normalized_aliases)
            or any(term.casefold() in normalized_spec for term in grade_terms)
        )
        if not name_hit:
            continue
        grade_hit = any(term.casefold() in normalized_spec for term in grade_terms)
        grade_specification = any(
            specification.startswith(term.casefold()) for term in grade_terms
        )
        if (
            specification
            and specification not in normalized_spec
            and not (grade_specification and grade_hit)
        ):
            continue
        if unit and unit != _normalize(material.unit):
            continue
        if raw == normalized_code:
            reason = "标准编码精确匹配"
        elif raw == normalized_name or raw in normalized_aliases:
            reason = "标准名称或维护别名精确匹配"
        elif grade_hit:
            reason = "规格型号前缀匹配"
        else:
            reason = "名称包含匹配"
        if specification:
            reason += "；规格匹配"
        if unit:
            reason += "；单位匹配"
        candidates.append((material, reason))

    confirmation_required = len(candidates) != 1 or not specification
    data = MaterialMatchData(
        candidates=tuple(
            MaterialCandidate(
                material_id=material.id,
                code=material.code,
                name=material.name,
                specification=material.specification,
                unit=material.unit,
                reason=reason,
                requires_confirmation=confirmation_required,
            )
            for material, reason in candidates[:10]
        )
    )
    issues = (
        (
            ToolIssue(
                code="MATERIAL_NOT_FOUND",
                message="没有符合名称、规格和单位条件的有效物料",
            ),
        )
        if not candidates
        else (
            (
                ToolIssue(
                    code="MATERIAL_CONFIRMATION_REQUIRED",
                    message="存在多个物料候选，或输入未提供规格；需要人工确认",
                ),
            )
            if confirmation_required
            else ()
        )
    )
    return _envelope(
        data,
        issues=issues,
        refs=tuple(f"material:{material.id}" for material, _ in candidates[:10]),
    )


async def get_contract_details(
    session: AsyncSession,
    value: GetContractInput,
    *,
    department_id: UUID,
    task_type: str,
    business_date: date | None = None,
) -> GetContractOutput:
    contract = await session.scalar(
        select(Contract).where(
            Contract.number == value.contract_number,
            Contract.department_id == department_id,
        )
    )
    if contract is None:
        raise MCPReadError("CONTRACT_NOT_FOUND", "当前部门未找到该合同")

    customer = await session.get(Customer, contract.customer_id)
    project = await session.get(Project, contract.project_id)
    line_rows = list(
        (
            await session.execute(
                select(ContractLine, Material)
                .join(Material, Material.id == ContractLine.material_id)
                .where(ContractLine.contract_id == contract.id)
                .order_by(ContractLine.id)
            )
        ).all()
    )
    lines = tuple(
        ContractLineData(
            material_id=material.id,
            material_code=material.code,
            material_name=material.name,
            specification=material.specification,
            unit=line.unit,
            unit_price=_decimal(line.unit_price),
            max_quantity=_decimal(line.max_quantity),
        )
        for line, material in line_rows
    )
    data = ContractData(
        id=contract.id,
        contract_number=contract.number,
        order_type=contract.type,
        customer_id=contract.customer_id,
        customer_name=customer.name if customer else "",
        project_id=contract.project_id,
        project_name=project.name if project else "",
        delivery_address=project.delivery_address if project else None,
        currency=contract.currency,
        total=f"{contract.total_amount:.2f}",
        status=contract.status,
        valid_from=contract.valid_from,
        valid_to=contract.valid_to,
        lines=lines,
    )
    today = business_date or _business_date()
    issues: list[ToolIssue] = []
    if contract.type != value.order_type or contract.type != task_type:
        issues.append(
            ToolIssue(
                code="CONTRACT_TYPE_MISMATCH", message="合同方向与任务或请求方向不一致"
            )
        )
    if contract.status != "ACTIVE":
        issues.append(
            ToolIssue(code="CONTRACT_INACTIVE", message="合同已停用，不能用于新订单")
        )
    if not contract.valid_from <= today <= contract.valid_to:
        issues.append(
            ToolIssue(code="CONTRACT_OUT_OF_TERM", message="合同当前不在有效期内")
        )
    return _envelope(
        data,
        issues=tuple(issues),
        refs=(
            f"contract:{contract.id}",
            f"customer:{contract.customer_id}",
            f"project:{contract.project_id}",
        ),
    )


async def check_balance(
    session: AsyncSession,
    value: CheckContractBalanceInput,
    *,
    department_id: UUID,
    task_type: str,
    actor_id: UUID,
) -> CheckContractBalanceOutput:
    contract = await session.scalar(
        select(Contract).where(
            Contract.id == value.contract_id,
            Contract.department_id == department_id,
        )
    )
    if contract is None:
        raise MCPReadError("CONTRACT_NOT_FOUND", "合同不存在或当前身份无权访问")

    draft = await session.get(Draft, value.draft_id)
    revision = await session.get(DraftRevision, (value.draft_id, value.draft_version))
    if (
        draft is None
        or revision is None
        or draft.current_version != value.draft_version
        or revision.changed_by != actor_id
    ):
        raise MCPReadError("DRAFT_NOT_FOUND_OR_STALE", "草稿不存在或版本已过期")
    if revision.contract_id != contract.id:
        raise MCPReadError(
            "DRAFT_CONTRACT_MISMATCH", "草稿合同与本次额度查询合同不一致"
        )
    if revision.total_amount is None:
        raise MCPReadError(
            "DRAFT_AMOUNT_UNAVAILABLE", "草稿金额未完成计算，不能执行额度核验"
        )
    requested = Decimal(value.order_amount)
    if requested != revision.total_amount:
        raise MCPReadError(
            "DRAFT_AMOUNT_MISMATCH", "自报订单金额与当前草稿版本金额不一致"
        )

    balance = await session.get(ContractBalance, contract.id)
    if balance is None:
        raise MCPReadError(
            "CONTRACT_BALANCE_UNAVAILABLE", "合同余额账缺失，无法安全核验"
        )
    remaining = contract.total_amount - balance.occupied_amount
    reasons: list[str] = []
    today = _business_date()
    if contract.type != task_type:
        reasons.append("CONTRACT_TYPE_MISMATCH")
    if contract.status != "ACTIVE":
        reasons.append("CONTRACT_INACTIVE")
    if not contract.valid_from <= today <= contract.valid_to:
        reasons.append("CONTRACT_OUT_OF_TERM")
    if remaining < 0:
        reasons.append("CONTRACT_BALANCE_INCONSISTENT")
    if requested > remaining:
        reasons.append("CONTRACT_AMOUNT_EXCEEDED")
    data = ContractBalanceData(
        total=f"{contract.total_amount:.2f}",
        occupied=f"{balance.occupied_amount:.2f}",
        remaining=_money(remaining),
        order_amount=f"{requested:.2f}",
        allowed=not reasons,
        checked_at=datetime.now(UTC),
        reasons=tuple(reasons),
    )
    return _envelope(
        data,
        refs=(f"contract:{contract.id}", f"draft:{draft.id}:v{value.draft_version}"),
    )
