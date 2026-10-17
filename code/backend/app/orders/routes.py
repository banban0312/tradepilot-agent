"""授权业务用户只读查询正式订单及批准、来源和审计快照。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import AuthContext, require_business_access
from app.auth.dependencies import get_current_identity, request_id
from app.database import get_db_session
from app.models import (
    AgentTask,
    AuditEvent,
    Contract,
    ContractBalance,
    Customer,
    Department,
    DraftRevision,
    FieldEvidence,
    Material,
    Order,
    OrderItem,
    Project,
    TaskApproval,
    User,
)

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "RESOURCE_NOT_FOUND", "message": "订单不存在"},
    )


def _order_query(context: AuthContext):
    return (
        select(Order, Contract, Project)
        .join(
            Contract,
            (Contract.id == Order.contract_id)
            & (Contract.department_id == Order.department_id),
        )
        .join(
            Project,
            (Project.id == Contract.project_id)
            & (Project.department_id == Order.department_id),
        )
        .where(Order.department_id.in_(context.department_ids))
    )


@router.get("")
async def list_orders(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    order_type: Annotated[
        Literal["SALES", "PURCHASE"] | None, Query(alias="type")
    ] = None,
    contract: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    project: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    state: Annotated[Literal["CREATED"] | None, Query()] = None,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """按可信部门范围和筛选条件列出订单；时间范围以 UTC 创建日期解释。"""
    if not context.roles.intersection({"business_operator", "business_approver"}):
        raise _not_found()
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INPUT_DATE_RANGE_INVALID",
                "message": "开始日期不能晚于结束日期",
            },
        )

    conditions = [Order.department_id.in_(context.department_ids)]
    if order_type is not None:
        conditions.append(Order.type == order_type)
    if contract is not None:
        term = contract.strip()
        if not term:
            raise HTTPException(
                status_code=422,
                detail={"code": "INPUT_INVALID", "message": "合同编号筛选不能为空白"},
            )
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Contract.number.ilike(f"%{escaped}%", escape="\\"))
    if project is not None:
        term = project.strip()
        if not term:
            raise HTTPException(
                status_code=422,
                detail={"code": "INPUT_INVALID", "message": "项目筛选不能为空白"},
            )
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conditions.append(or_(Project.code.ilike(pattern), Project.name.ilike(pattern)))
    if state is not None:
        conditions.append(Order.state == state)
    if from_date is not None:
        conditions.append(
            Order.created_at >= datetime.combine(from_date, time.min, UTC)
        )
    if to_date is not None and to_date < date.max:
        conditions.append(
            Order.created_at
            < datetime.combine(to_date + timedelta(days=1), time.min, UTC)
        )

    total = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .join(
                Contract,
                (Contract.id == Order.contract_id)
                & (Contract.department_id == Order.department_id),
            )
            .join(
                Project,
                (Project.id == Contract.project_id)
                & (Project.department_id == Order.department_id),
            )
            .where(*conditions)
        )
        or 0
    )
    rows = (
        await session.execute(
            _order_query(context)
            .where(*conditions)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "data": {
            "items": [
                {
                    "id": str(order.id),
                    "number": order.number,
                    "type": order.type,
                    "contract_number": contract.number,
                    "project_code": project.code,
                    "project_name": project.name,
                    "state": order.state,
                    "currency": order.currency,
                    "total_amount": format(order.total_amount, ".2f"),
                    "created_at": order.created_at.isoformat(),
                    "task_id": str(order.task_id),
                }
                for order, contract, project in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        },
        "request_id": request_id(request),
    }


@router.get("/{order_id}")
async def get_order(
    order_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    """返回订单不可变主明细、批准版本、额度和任务审计来源。"""
    row = await session.execute(
        select(
            Order,
            Contract,
            Customer,
            Project,
            Department,
            AgentTask,
            User,
            ContractBalance,
        )
        .join(
            Contract,
            (Contract.id == Order.contract_id)
            & (Contract.department_id == Order.department_id),
        )
        .join(
            Customer,
            (Customer.id == Contract.customer_id)
            & (Customer.department_id == Order.department_id),
        )
        .join(
            Project,
            (Project.id == Contract.project_id)
            & (Project.department_id == Order.department_id),
        )
        .join(Department, Department.id == Order.department_id)
        .join(AgentTask, AgentTask.id == Order.task_id)
        .join(User, User.id == AgentTask.owner_id)
        .outerjoin(ContractBalance, ContractBalance.contract_id == Order.contract_id)
        .where(Order.id == order_id, Order.department_id.in_(context.department_ids))
    )
    data = row.one_or_none()
    if data is None:
        raise _not_found()
    order, contract, customer, project, department, task, operator, balance = data
    require_business_access(context, order.department_id)

    approval = await session.get(TaskApproval, order.approval_id)
    if (
        approval is None
        or approval.task_id != order.task_id
        or approval.department_id != order.department_id
        or approval.decision != "APPROVE"
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ORDER_APPROVAL_UNAVAILABLE",
                "message": "订单批准记录暂不可用",
            },
        )
    revision = await session.get(
        DraftRevision, (approval.draft_id, order.approved_version)
    )
    if (
        revision is None
        or revision.version != approval.version
        or approval.version != order.approved_version
        or revision.content_hash != order.approved_content_hash
        or approval.content_hash != order.approved_content_hash
        or revision.total_amount != order.total_amount
        or revision.currency != order.currency
        or revision.contract_id != order.contract_id
        or revision.order_type != order.type
        or revision.customer_id != contract.customer_id
        or revision.project_id != contract.project_id
        or task.department_id != order.department_id
        or task.task_type != order.type
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ORDER_SNAPSHOT_UNAVAILABLE",
                "message": "订单批准快照不完整，暂时无法展示",
            },
        )

    item_rows = (
        await session.execute(
            select(OrderItem, Material)
            .join(Material, Material.id == OrderItem.material_id)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.line_no)
        )
    ).all()
    if (
        sum((item.amount for item, _ in item_rows), start=Decimal("0.00"))
        != order.total_amount
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ORDER_ITEMS_UNAVAILABLE",
                "message": "订单明细金额与正式订单总额不一致，暂时无法展示",
            },
        )
    approved_by = await session.get(User, approval.actor_id)
    evidence_rows = (
        await session.execute(
            select(FieldEvidence, User)
            .outerjoin(User, User.id == FieldEvidence.confirmed_by)
            .where(
                FieldEvidence.draft_id == approval.draft_id,
                FieldEvidence.version == order.approved_version,
            )
            .order_by(FieldEvidence.field_path, FieldEvidence.id)
        )
    ).all()
    audit_rows = (
        await session.execute(
            select(AuditEvent, User)
            .outerjoin(User, User.id == AuditEvent.actor_id)
            .where(AuditEvent.task_id == order.task_id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
    ).all()
    remaining = (
        contract.total_amount - balance.occupied_amount if balance is not None else None
    )

    return {
        "data": {
            "id": str(order.id),
            "number": order.number,
            "type": order.type,
            "state": order.state,
            "department": {"id": str(department.id), "name": department.name},
            "contract": {
                "id": str(contract.id),
                "number": contract.number,
                "type": contract.type,
                "customer_name": customer.name,
                "project_id": str(project.id),
                "project_code": project.code,
                "project_name": project.name,
                "currency": contract.currency,
                "total_amount": format(contract.total_amount, ".2f"),
                "occupied_amount": format(balance.occupied_amount, ".2f")
                if balance
                else None,
                "remaining_amount": format(remaining, ".2f")
                if remaining is not None
                else None,
                "balance_available": balance is not None,
            },
            "operator": {"login": operator.login, "department_name": department.name},
            "created_at": order.created_at.isoformat(),
            "delivery_date": revision.delivery_date.isoformat()
            if revision.delivery_date
            else None,
            "address": revision.address,
            "contact": revision.contact,
            "business_fields": revision.business_fields,
            "currency": order.currency,
            "total_amount": format(order.total_amount, ".2f"),
            "items": [
                {
                    "line_no": item.line_no,
                    "material_code": material.code,
                    "material_name": material.name,
                    "raw_name": item.raw_name,
                    "specification": item.specification,
                    "quantity": format(item.quantity, "f"),
                    "unit": item.unit,
                    "unit_price": format(item.unit_price, "f"),
                    "amount": format(item.amount, ".2f"),
                }
                for item, material in item_rows
            ],
            "approval": {
                "id": str(approval.id),
                "decision": approval.decision,
                "version": approval.version,
                "content_hash": approval.content_hash,
                "approved_by": approved_by.login if approved_by else "未知用户",
                "created_at": approval.created_at.isoformat(),
                "invalidated_at": approval.invalidated_at.isoformat()
                if approval.invalidated_at
                else None,
                "snapshot_amount": format(revision.total_amount, ".2f")
                if revision.total_amount is not None
                else None,
            },
            "sources": [
                {
                    "field_path": evidence.field_path,
                    "source_type": evidence.source_type,
                    "locator": evidence.locator,
                    "confirmed_by": confirmer.login if confirmer else None,
                }
                for evidence, confirmer in evidence_rows
            ],
            "audit": [
                {
                    "action": event.action,
                    "result": event.result,
                    "actor": actor.login if actor else "系统",
                    "created_at": event.created_at.isoformat(),
                    "input_hash": event.input_hash,
                }
                for event, actor in audit_rows
            ],
            "task": {
                "id": str(task.id),
                "state": task.state,
                "trace_url": f"/tasks/{task.id}",
            },
        },
        "request_id": request_id(request),
    }
