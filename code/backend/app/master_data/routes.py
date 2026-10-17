"""管理员合同台账、标准物料与业务别名维护。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import SYSTEM_ADMIN, AuthContext
from app.auth.dependencies import get_current_identity, request_id, require_csrf
from app.database import get_db_session
from app.master_data.normalization import normalize_material_name
from app.mcp_server.contracts import MatchMaterialInput
from app.mcp_server.queries import match_materials
from app.models import (
    Contract,
    ContractBalance,
    ContractLine,
    ContractLineBalance,
    Customer,
    Department,
    Material,
    MaterialAlias,
    Order,
    Project,
)
from app.observability.service import record_audit_event

router = APIRouter(prefix="/api/v1/admin", tags=["admin-master-data"])
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_LongText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]


class ContractLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: UUID | None = None
    material_id: UUID
    unit: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)
    ]
    unit_price: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    max_quantity: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )

    @field_validator("unit_price", "max_quantity")
    @classmethod
    def require_finite_numbers(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("数值必须为有限十进制数")
        return value


class ContractCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_id: UUID
    number: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    type: Literal["SALES", "PURCHASE"]
    customer_id: UUID
    project_id: UUID
    currency: Literal["CNY"] = "CNY"
    total_amount: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"
    valid_from: date
    valid_to: date
    lines: list[ContractLineInput] = Field(default_factory=list, max_length=200)

    @field_validator("total_amount")
    @classmethod
    def require_finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("合同总额必须为有限金额")
        return value


class ContractUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    number: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
        ]
        | None
    ) = None
    type: Literal["SALES", "PURCHASE"] | None = None
    customer_id: UUID | None = None
    project_id: UUID | None = None
    currency: Literal["CNY"] | None = None
    total_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=20, decimal_places=2
    )
    status: Literal["ACTIVE", "INACTIVE"] | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    lines: list[ContractLineInput] | None = Field(default=None, max_length=200)

    @field_validator("total_amount")
    @classmethod
    def require_finite_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("合同总额必须为有限金额")
        return value


class MaterialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    name: _LongText
    specification: _LongText
    unit: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)
    ]
    active: bool = True
    aliases: list[_LongText] = Field(default_factory=list, max_length=100)


class MaterialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    code: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
        ]
        | None
    ) = None
    name: _LongText | None = None
    specification: _LongText | None = None
    unit: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)
        ]
        | None
    ) = None
    active: bool | None = None
    aliases: list[_LongText] | None = Field(default=None, max_length=100)


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _require_admin(context: AuthContext) -> None:
    if SYSTEM_ADMIN not in context.roles:
        raise _fail(403, "ACCESS_DENIED", "当前账号没有主数据管理权限")


def _check_department(context: AuthContext, department_id: UUID) -> None:
    if department_id not in context.department_ids:
        raise _fail(404, "RESOURCE_NOT_FOUND", "资源不存在或当前账号无权访问")


async def _validate_contract_links(
    session: AsyncSession,
    *,
    department_id: UUID,
    customer_id: UUID,
    project_id: UUID,
) -> tuple[Customer, Project]:
    customer = await session.scalar(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.department_id == department_id,
            Customer.active.is_(True),
        )
    )
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.department_id == department_id,
        )
    )
    if customer is None or project is None:
        raise _fail(
            422, "INPUT_REFERENCE_INVALID", "客户或项目不属于所选部门，或客户已停用"
        )
    return customer, project


async def _validate_lines(
    session: AsyncSession, lines: list[ContractLineInput]
) -> None:
    seen: set[tuple[UUID, str]] = set()
    for line in lines:
        material = await session.get(Material, line.material_id)
        if material is None or not material.active:
            raise _fail(422, "INPUT_REFERENCE_INVALID", "合同明细物料不存在或已停用")
        if line.unit != material.unit:
            raise _fail(422, "INPUT_UNIT_INVALID", "合同明细单位必须与标准物料单位一致")
        key = (line.material_id, line.unit)
        if key in seen:
            raise _fail(
                422, "CONTRACT_LINE_DUPLICATE", "同一合同不能重复添加相同物料和单位"
            )
        seen.add(key)


def _line_data(line: ContractLine, material: Material) -> dict[str, object]:
    return {
        "id": str(line.id),
        "material_id": str(material.id),
        "material_code": material.code,
        "material_name": material.name,
        "specification": material.specification,
        "unit": line.unit,
        "unit_price": str(line.unit_price) if line.unit_price is not None else None,
        "max_quantity": str(line.max_quantity)
        if line.max_quantity is not None
        else None,
    }


async def _contract_lines(
    session: AsyncSession, contract_id: UUID
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(ContractLine, Material)
            .join(Material, Material.id == ContractLine.material_id)
            .where(ContractLine.contract_id == contract_id)
            .order_by(Material.code, ContractLine.unit)
        )
    ).all()
    return [_line_data(line, material) for line, material in rows]


def _balance_data(
    contract: Contract, balance: ContractBalance | None
) -> dict[str, object]:
    if balance is None:
        raise _fail(
            503,
            "CONTRACT_BALANCE_UNAVAILABLE",
            "合同余额账本缺失，暂不能安全读取合同统计",
        )
    occupied = balance.occupied_amount
    order_count = balance.occupied_order_count
    return {
        "occupied_amount": str(occupied),
        "order_count": order_count,
        "remaining_amount": str(contract.total_amount - occupied),
        "balance_version": balance.version if balance is not None else 0,
    }


def _contract_fields(contract: Contract) -> dict[str, object]:
    return {
        "id": str(contract.id),
        "department_id": str(contract.department_id),
        "number": contract.number,
        "type": contract.type,
        "customer_id": str(contract.customer_id),
        "project_id": str(contract.project_id),
        "currency": contract.currency,
        "total_amount": str(contract.total_amount),
        "status": contract.status,
        "valid_from": contract.valid_from.isoformat(),
        "valid_to": contract.valid_to.isoformat(),
        "version": contract.version,
    }


async def _audit(
    session: AsyncSession,
    *,
    actor: AuthContext,
    action: str,
    entity: str,
    entity_id: UUID,
    after: dict[str, object],
    before: dict[str, object] | None,
    input_value: object,
) -> None:
    await record_audit_event(
        session,
        actor=actor,
        action=action,
        result="OK",
        object_ref={"type": entity, "id": entity_id},
        before_ref=before,
        after_ref=after,
        input_value=input_value,
    )


@router.get("/departments")
async def list_departments(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _require_admin(context)
    rows = await session.scalars(
        select(Department)
        .where(Department.id.in_(context.department_ids), Department.active.is_(True))
        .order_by(Department.name)
    )
    return {
        "data": [
            {"id": str(row.id), "code": row.code, "name": row.name} for row in rows
        ],
        "request_id": request_id(request),
    }


@router.get("/lookups")
async def list_contract_lookups(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    department_id: UUID,
) -> dict[str, object]:
    _require_admin(context)
    _check_department(context, department_id)
    customers = await session.scalars(
        select(Customer)
        .where(Customer.department_id == department_id, Customer.active.is_(True))
        .order_by(Customer.name, Customer.code)
    )
    projects = await session.scalars(
        select(Project)
        .where(Project.department_id == department_id)
        .order_by(Project.name, Project.code)
    )
    materials = await session.scalars(
        select(Material).where(Material.active.is_(True)).order_by(Material.code)
    )
    return {
        "data": {
            "customers": [
                {"id": str(item.id), "code": item.code, "name": item.name}
                for item in customers
            ],
            "projects": [
                {"id": str(item.id), "code": item.code, "name": item.name}
                for item in projects
            ],
            "materials": [
                {
                    "id": str(item.id),
                    "code": item.code,
                    "name": item.name,
                    "specification": item.specification,
                    "unit": item.unit,
                }
                for item in materials
            ],
        },
        "request_id": request_id(request),
    }


@router.get("/contracts")
async def list_contracts(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    q: Annotated[str | None, Query(max_length=128)] = None,
    department_id: UUID | None = None,
    type: Literal["SALES", "PURCHASE"] | None = None,
    status: Literal["ACTIVE", "INACTIVE"] | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10000),
) -> dict[str, object]:
    _require_admin(context)
    departments = set(context.department_ids)
    if department_id is not None:
        _check_department(context, department_id)
        departments = {department_id}
    filters = [Contract.department_id.in_(departments)]
    if type is not None:
        filters.append(Contract.type == type)
    if status is not None:
        filters.append(Contract.status == status)
    if q and q.strip():
        escaped = q.strip().replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        filters.append(Contract.number.ilike(pattern, escape="\\"))
    total = int(
        await session.scalar(select(func.count()).select_from(Contract).where(*filters))
        or 0
    )
    rows = (
        await session.execute(
            select(Contract, Customer, Project, Department, ContractBalance)
            .join(Customer, Customer.id == Contract.customer_id)
            .join(Project, Project.id == Contract.project_id)
            .join(Department, Department.id == Contract.department_id)
            .outerjoin(ContractBalance, ContractBalance.contract_id == Contract.id)
            .where(*filters)
            .order_by(Contract.updated_at.desc(), Contract.number)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "data": {
            "items": [
                {
                    **_contract_fields(contract),
                    "customer_name": customer.name,
                    "project_name": project.name,
                    "department_name": department.name,
                    **_balance_data(contract, balance),
                }
                for contract, customer, project, department, balance in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        },
        "request_id": request_id(request),
    }


@router.get("/contracts/{contract_id}")
async def get_contract(
    contract_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _require_admin(context)
    row = await session.execute(
        select(Contract, Customer, Project, Department, ContractBalance)
        .join(Customer, Customer.id == Contract.customer_id)
        .join(Project, Project.id == Contract.project_id)
        .join(Department, Department.id == Contract.department_id)
        .outerjoin(ContractBalance, ContractBalance.contract_id == Contract.id)
        .where(
            Contract.id == contract_id,
            Contract.department_id.in_(context.department_ids),
        )
    )
    result = row.one_or_none()
    if result is None:
        raise _fail(404, "RESOURCE_NOT_FOUND", "合同不存在")
    contract, customer, project, department, balance = result
    return {
        "data": {
            **_contract_fields(contract),
            "customer_name": customer.name,
            "project_name": project.name,
            "department_name": department.name,
            **_balance_data(contract, balance),
            "lines": await _contract_lines(session, contract.id),
            "created_at": contract.created_at.isoformat(),
            "updated_at": contract.updated_at.isoformat(),
        },
        "request_id": request_id(request),
    }


@router.post("/contracts", status_code=201)
async def create_contract(
    body: ContractCreate,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _require_admin(context)
    _check_department(context, body.department_id)
    if body.valid_to < body.valid_from:
        raise _fail(422, "INPUT_DATE_RANGE_INVALID", "合同结束日期不能早于开始日期")
    await _validate_contract_links(
        session,
        department_id=body.department_id,
        customer_id=body.customer_id,
        project_id=body.project_id,
    )
    await _validate_lines(session, body.lines)
    duplicate = await session.scalar(
        select(Contract.id).where(
            Contract.department_id == body.department_id,
            func.lower(Contract.number) == body.number.casefold(),
        )
    )
    if duplicate is not None:
        raise _fail(409, "CONTRACT_NUMBER_CONFLICT", "该部门已存在相同合同编号")
    contract = Contract(
        id=uuid4(),
        department_id=body.department_id,
        number=body.number,
        type=body.type,
        customer_id=body.customer_id,
        project_id=body.project_id,
        currency=body.currency,
        total_amount=body.total_amount,
        status=body.status,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        version=1,
    )
    session.add(contract)
    session.add(
        ContractBalance(
            contract_id=contract.id,
            occupied_amount=Decimal("0.00"),
            occupied_order_count=0,
            version=0,
        )
    )
    for line_input in body.lines:
        line = ContractLine(
            id=uuid4(),
            contract_id=contract.id,
            material_id=line_input.material_id,
            unit=line_input.unit,
            unit_price=line_input.unit_price,
            max_quantity=line_input.max_quantity,
        )
        session.add(line)
        session.add(
            ContractLineBalance(contract_line_id=line.id, occupied_quantity=Decimal(0))
        )
    try:
        await _audit(
            session,
            actor=context,
            action="admin.contract.created",
            entity="contract",
            entity_id=contract.id,
            before=None,
            after={
                **_contract_fields(contract),
                "lines": await _contract_lines(session, contract.id),
            },
            input_value=body.model_dump(mode="json"),
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _fail(
            409, "CONTRACT_CONFLICT", "合同编号或关联主数据已变化，请刷新后重试"
        ) from exc
    return {"data": _contract_fields(contract), "request_id": request_id(request)}


@router.patch("/contracts/{contract_id}")
async def update_contract(
    contract_id: UUID,
    body: ContractUpdate,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _require_admin(context)
    contract = await session.scalar(
        select(Contract)
        .where(
            Contract.id == contract_id,
            Contract.department_id.in_(context.department_ids),
        )
        .with_for_update()
    )
    if contract is None:
        raise _fail(404, "RESOURCE_NOT_FOUND", "合同不存在")
    if body.expected_version != contract.version:
        raise _fail(409, "VERSION_CONFLICT", "合同已由其他管理员修改，请刷新后重试")
    updates = body.model_dump(exclude_unset=True, exclude={"expected_version", "lines"})
    if any(value is None for value in updates.values()):
        raise _fail(422, "INPUT_INVALID", "合同字段不能设为空")
    next_from = updates.get("valid_from", contract.valid_from)
    next_to = updates.get("valid_to", contract.valid_to)
    if next_to < next_from:
        raise _fail(422, "INPUT_DATE_RANGE_INVALID", "合同结束日期不能早于开始日期")
    if "number" in updates:
        duplicate_number = await session.scalar(
            select(Contract.id).where(
                Contract.department_id == contract.department_id,
                Contract.id != contract.id,
                func.lower(Contract.number) == updates["number"].casefold(),
            )
        )
        if duplicate_number is not None:
            raise _fail(409, "CONTRACT_NUMBER_CONFLICT", "该部门已存在相同合同编号")
    if "customer_id" in updates or "project_id" in updates:
        await _validate_contract_links(
            session,
            department_id=contract.department_id,
            customer_id=updates.get("customer_id", contract.customer_id),
            project_id=updates.get("project_id", contract.project_id),
        )
    balance = await session.scalar(
        select(ContractBalance)
        .where(ContractBalance.contract_id == contract.id)
        .with_for_update()
    )
    if balance is None:
        raise _fail(
            503,
            "CONTRACT_BALANCE_UNAVAILABLE",
            "合同余额账本缺失，暂不能安全修改合同",
        )
    if "total_amount" in updates:
        booked_orders = await session.scalar(
            select(func.coalesce(func.sum(Order.total_amount), 0)).where(
                Order.contract_id == contract.id
            )
        )
        occupied_amount = balance.occupied_amount
        if updates["total_amount"] < max(occupied_amount, Decimal(booked_orders or 0)):
            raise _fail(
                409, "CONTRACT_TOTAL_BELOW_OCCUPIED", "合同总额不能低于已占用金额"
            )
    before = {
        **_contract_fields(contract),
        "lines": await _contract_lines(session, contract.id),
    }
    changed = any(getattr(contract, key) != value for key, value in updates.items())
    line_inputs = body.lines
    if "lines" in body.model_fields_set and line_inputs is None:
        raise _fail(422, "INPUT_INVALID", "合同明细列表不能设为空值")
    if line_inputs is not None:
        await _validate_lines(session, line_inputs)
        for line_input in line_inputs:
            if line_input.line_id is None:
                exists = await session.scalar(
                    select(ContractLine.id).where(
                        ContractLine.contract_id == contract.id,
                        ContractLine.material_id == line_input.material_id,
                        ContractLine.unit == line_input.unit,
                    )
                )
                if exists is not None:
                    raise _fail(
                        409, "CONTRACT_LINE_DUPLICATE", "该合同已存在相同物料和单位"
                    )
                line = ContractLine(
                    id=uuid4(),
                    contract_id=contract.id,
                    material_id=line_input.material_id,
                    unit=line_input.unit,
                    unit_price=line_input.unit_price,
                    max_quantity=line_input.max_quantity,
                )
                session.add(line)
                session.add(
                    ContractLineBalance(
                        contract_line_id=line.id, occupied_quantity=Decimal(0)
                    )
                )
                changed = True
                continue
            line = await session.scalar(
                select(ContractLine)
                .where(
                    ContractLine.id == line_input.line_id,
                    ContractLine.contract_id == contract.id,
                )
                .with_for_update()
            )
            if line is None:
                raise _fail(404, "RESOURCE_NOT_FOUND", "合同明细不存在")
            if (
                line.material_id != line_input.material_id
                or line.unit != line_input.unit
            ):
                raise _fail(
                    409,
                    "CONTRACT_LINE_IDENTITY_IMMUTABLE",
                    "已有合同明细的物料与单位不可在此更换",
                )
            line_balance = await session.scalar(
                select(ContractLineBalance)
                .where(ContractLineBalance.contract_line_id == line.id)
                .with_for_update()
            )
            if line_balance is None and (
                line.max_quantity is not None or line_input.max_quantity is not None
            ):
                raise _fail(
                    503,
                    "CONTRACT_LINE_BALANCE_UNAVAILABLE",
                    "已配置数量上限的合同明细缺少履约账本，暂不能安全修改",
                )
            if (
                line_input.max_quantity is not None
                and line_balance is not None
                and line_input.max_quantity < line_balance.occupied_quantity
            ):
                raise _fail(
                    409,
                    "CONTRACT_LINE_BELOW_OCCUPIED",
                    "物料数量上限不能低于已履约数量",
                )
            if (
                line.unit_price != line_input.unit_price
                or line.max_quantity != line_input.max_quantity
            ):
                line.unit_price = line_input.unit_price
                line.max_quantity = line_input.max_quantity
                changed = True
    if changed:
        for key, value in updates.items():
            setattr(contract, key, value)
        contract.version += 1
        try:
            await _audit(
                session,
                actor=context,
                action="admin.contract.updated",
                entity="contract",
                entity_id=contract.id,
                before=before,
                after={
                    **_contract_fields(contract),
                    "lines": await _contract_lines(session, contract.id),
                },
                input_value=body.model_dump(mode="json", exclude_unset=True),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _fail(
                409, "CONTRACT_CONFLICT", "合同主数据已变化，请刷新后重试"
            ) from exc
    else:
        await session.commit()
    return {"data": _contract_fields(contract), "request_id": request_id(request)}


async def _material_aliases(session: AsyncSession, material_id: UUID) -> list[str]:
    values = await session.scalars(
        select(MaterialAlias.alias)
        .where(MaterialAlias.material_id == material_id)
        .order_by(MaterialAlias.normalized_alias, MaterialAlias.alias)
    )
    return list(values.all())


def _material_fields(material: Material, aliases: list[str]) -> dict[str, object]:
    return {
        "id": str(material.id),
        "code": material.code,
        "name": material.name,
        "specification": material.specification,
        "unit": material.unit,
        "active": material.active,
        "version": material.version,
        "aliases": aliases,
        "created_at": material.created_at.isoformat(),
        "updated_at": material.updated_at.isoformat(),
    }


async def _normalized_aliases(aliases: list[str]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in aliases:
        text = value.strip()
        normalized = normalize_material_name(text)
        if not text or not normalized:
            raise _fail(422, "MATERIAL_ALIAS_INVALID", "物料别名不能为空")
        if normalized in seen:
            raise _fail(
                422,
                "MATERIAL_ALIAS_DUPLICATE",
                "同一物料不能重复设置归一化后相同的别名",
            )
        seen.add(normalized)
        rows.append((text, normalized))
    return rows


async def _check_material_code(
    session: AsyncSession, code: str, *, exclude_id: UUID | None = None
) -> None:
    statement = select(Material.id).where(func.lower(Material.code) == code.casefold())
    if exclude_id is not None:
        statement = statement.where(Material.id != exclude_id)
    if await session.scalar(statement) is not None:
        raise _fail(409, "MATERIAL_CODE_CONFLICT", "标准物料编码已存在")


@router.get("/materials")
async def list_materials(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    q: Annotated[str | None, Query(max_length=128)] = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10000),
) -> dict[str, object]:
    _require_admin(context)
    filters = []
    if active is not None:
        filters.append(Material.active.is_(active))
    if q and q.strip():
        escaped = q.strip().replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        matches = select(MaterialAlias.material_id).where(
            MaterialAlias.alias.ilike(pattern, escape="\\")
        )
        filters.append(
            (Material.code.ilike(pattern, escape="\\"))
            | (Material.name.ilike(pattern, escape="\\"))
            | (Material.specification.ilike(pattern, escape="\\"))
            | Material.id.in_(matches)
        )
    total = int(
        await session.scalar(select(func.count()).select_from(Material).where(*filters))
        or 0
    )
    materials = list(
        (
            await session.scalars(
                select(Material)
                .where(*filters)
                .order_by(Material.code)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    ids = [item.id for item in materials]
    alias_rows = (
        await session.execute(
            select(MaterialAlias.material_id, MaterialAlias.alias)
            .where(MaterialAlias.material_id.in_(ids))
            .order_by(MaterialAlias.normalized_alias, MaterialAlias.alias)
        )
        if ids
        else []
    )
    aliases_by_id: dict[UUID, list[str]] = {}
    for material_id, alias in alias_rows:
        aliases_by_id.setdefault(material_id, []).append(alias)
    return {
        "data": {
            "items": [
                _material_fields(item, aliases_by_id.get(item.id, []))
                for item in materials
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        },
        "request_id": request_id(request),
    }


@router.get("/materials/match-preview")
async def preview_material_match(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    raw_name: Annotated[str, Query(min_length=1, max_length=512)],
    specification: Annotated[str | None, Query(max_length=256)] = None,
    unit: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, object]:
    _require_admin(context)
    result = await match_materials(
        session,
        MatchMaterialInput(raw_name=raw_name, specification=specification, unit=unit),
    )
    return {
        "data": result.data.model_dump(mode="json"),
        "request_id": request_id(request),
    }


@router.post("/materials", status_code=201)
async def create_material(
    body: MaterialCreate,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _require_admin(context)
    await _check_material_code(session, body.code)
    aliases = await _normalized_aliases(body.aliases)
    material = Material(
        id=uuid4(),
        code=body.code,
        name=body.name,
        specification=body.specification,
        unit=body.unit,
        active=body.active,
        version=1,
    )
    session.add(material)
    for alias, normalized in aliases:
        session.add(
            MaterialAlias(
                id=uuid4(),
                material_id=material.id,
                alias=alias,
                normalized_alias=normalized,
            )
        )
    before = None
    after = {
        "code": material.code,
        "name": material.name,
        "specification": material.specification,
        "unit": material.unit,
        "active": material.active,
        "version": material.version,
        "aliases": [a for a, _ in aliases],
    }
    try:
        await _audit(
            session,
            actor=context,
            action="admin.material.created",
            entity="material",
            entity_id=material.id,
            before=before,
            after=after,
            input_value=body.model_dump(mode="json"),
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _fail(
            409, "MATERIAL_CONFLICT", "标准物料编码或别名已变化，请刷新后重试"
        ) from exc
    # PostgreSQL 生成的时间戳由 refresh 显式载入，避免序列化时触发隐式异步 IO。
    await session.refresh(material)
    saved_aliases = await _material_aliases(session, material.id)
    return {
        "data": _material_fields(material, saved_aliases),
        "request_id": request_id(request),
    }


@router.patch("/materials/{material_id}")
async def update_material(
    material_id: UUID,
    body: MaterialUpdate,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    _require_admin(context)
    material = await session.scalar(
        select(Material).where(Material.id == material_id).with_for_update()
    )
    if material is None:
        raise _fail(404, "RESOURCE_NOT_FOUND", "标准物料不存在")
    if material.version != body.expected_version:
        raise _fail(409, "VERSION_CONFLICT", "标准物料已由其他管理员修改，请刷新后重试")
    updates = body.model_dump(
        exclude_unset=True, exclude={"expected_version", "aliases"}
    )
    if any(value is None for value in updates.values()):
        raise _fail(422, "INPUT_INVALID", "标准物料字段不能设为空")
    if "code" in updates:
        await _check_material_code(session, updates["code"], exclude_id=material.id)
    before_aliases = await _material_aliases(session, material.id)
    before = {
        "code": material.code,
        "name": material.name,
        "specification": material.specification,
        "unit": material.unit,
        "active": material.active,
        "version": material.version,
        "aliases": before_aliases,
    }
    changed = any(getattr(material, key) != value for key, value in updates.items())
    if "aliases" in body.model_fields_set:
        if body.aliases is None:
            raise _fail(
                422, "INPUT_INVALID", "物料别名列表不能设为空值；请传空数组清空别名"
            )
        after_aliases = body.aliases or []
        normalized_aliases = await _normalized_aliases(after_aliases)
        if before_aliases != sorted(
            after_aliases, key=lambda value: (normalize_material_name(value), value)
        ):
            existing = list(
                (
                    await session.scalars(
                        select(MaterialAlias).where(
                            MaterialAlias.material_id == material.id
                        )
                    )
                ).all()
            )
            for row in existing:
                await session.delete(row)
            # 先释放事务内的唯一键，再插入替换后的别名集合。
            await session.flush()
            for alias, normalized in normalized_aliases:
                session.add(
                    MaterialAlias(
                        id=uuid4(),
                        material_id=material.id,
                        alias=alias,
                        normalized_alias=normalized,
                    )
                )
            changed = True
    for key, value in updates.items():
        setattr(material, key, value)
    if changed:
        material.version += 1
        after = {
            "code": material.code,
            "name": material.name,
            "specification": material.specification,
            "unit": material.unit,
            "active": material.active,
            "version": material.version,
            "aliases": (
                sorted(
                    body.aliases,
                    key=lambda value: (normalize_material_name(value), value),
                )
                if body.aliases is not None
                else before_aliases
            ),
        }
        try:
            await _audit(
                session,
                actor=context,
                action="admin.material.updated",
                entity="material",
                entity_id=material.id,
                before=before,
                after=after,
                input_value=body.model_dump(mode="json", exclude_unset=True),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _fail(
                409, "MATERIAL_CONFLICT", "标准物料或别名已变化，请刷新后重试"
            ) from exc
    else:
        await session.commit()
    await session.refresh(material)
    aliases = await _material_aliases(session, material.id)
    return {
        "data": _material_fields(material, aliases),
        "request_id": request_id(request),
    }
