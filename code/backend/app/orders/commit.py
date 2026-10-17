"""人工批准后正式提交订单，并在同一事务中复核实时规则与额度。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import BUSINESS_APPROVER, BUSINESS_OPERATOR, AuthContext
from app.config import Settings, settings
from app.drafts.rules import precheck_candidate
from app.drafts.service import load_draft
from app.drafts.types import line_amount
from app.models import (
    AgentTask,
    Contract,
    ContractBalance,
    ContractLine,
    ContractLineBalance,
    Draft,
    Order,
    OrderItem,
    OrderNumberCounter,
    QuotaOccupation,
    TaskApproval,
    TaskCommand,
    User,
    UserDepartmentScope,
    UserRole,
)
from app.observability.service import append_task_event, record_audit_event

ORDER_NAMESPACE = UUID("6876a9ec-bf0e-47c4-8d32-48e4f5b91022")


class OrderCommitRejected(RuntimeError):
    """提交身份或批准绑定已失效；订单与额度不会被写入。"""


@dataclass(frozen=True, slots=True)
class OrderCommitResult:
    state: str
    order_id: UUID | None = None
    order_number: str | None = None
    reason: str | None = None
    code: str | None = None
    replayed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "order_id": str(self.order_id) if self.order_id else None,
            "order_number": self.order_number,
            "reason": self.reason,
            "code": self.code,
            "replayed": self.replayed,
        }


def _current_business_date(config: Settings) -> date:
    return datetime.now(ZoneInfo(config.business_timezone)).date()


def _submit_key(task_id: UUID, approval_id: UUID, version: int, digest: str) -> str:
    value = f"{task_id}:{approval_id}:{version}:{digest}".encode()
    return hashlib.sha256(value).hexdigest()


def _command_binds_approval(command: TaskCommand, approval: TaskApproval) -> bool:
    return (
        command.kind == "REVIEW"
        and command.actor_id == approval.actor_id
        and command.expected_version == approval.version
        and command.target_interrupt_id == approval.interrupt_id
        and command.payload.get("decision") == "APPROVE"
        and command.payload.get("draft_id") == str(approval.draft_id)
        and command.payload.get("content_hash") == approval.content_hash
        and command.payload.get("checkpoint_id") == approval.checkpoint_ref
        and command.payload.get("interrupt_id") == approval.interrupt_id
        and approval.decision == "APPROVE"
    )


def _order_number(
    *, order_type: str, customer_code: str, day: date, sequence: int
) -> str:
    prefix = "XSDD" if order_type == "SALES" else "CGDD"
    code = re.sub(r"[^A-Z0-9]", "", customer_code.upper())[:4] or "CUST"
    return f"{prefix}-{code}-{day:%Y%m%d}-{sequence:03d}"


async def _approver_context(
    session: AsyncSession, *, approval: TaskApproval, task: AgentTask, config: Settings
) -> AuthContext:
    user = await session.get(User, approval.actor_id)
    roles = frozenset(
        await session.scalars(
            select(UserRole.role).where(UserRole.user_id == approval.actor_id)
        )
    )
    departments = frozenset(
        await session.scalars(
            select(UserDepartmentScope.department_id).where(
                UserDepartmentScope.user_id == approval.actor_id
            )
        )
    )
    if (
        user is None
        or not user.active
        or BUSINESS_APPROVER not in roles
        or task.department_id not in departments
        or approval.department_id != task.department_id
        or (
            approval.actor_id == task.owner_id
            and not (config.demo_mode and config.demo_allow_self_approval)
        )
    ):
        raise OrderCommitRejected(
            "审批人已失去当前部门的审批权限", "APPROVAL_FORBIDDEN"
        )
    return AuthContext(
        user_id=user.id,
        login=user.login,
        roles=roles,
        department_ids=departments,
    )


async def _blocked(
    session: AsyncSession,
    *,
    task: AgentTask,
    draft: Draft,
    approval: TaskApproval,
    command: TaskCommand,
    actor: AuthContext,
    code: str,
    reason: str,
) -> OrderCommitResult:
    """实时规则阻断后释放草稿锁，并持久化失效批准和审计结果。"""
    now = datetime.now(UTC)
    approval.invalidated_at = now
    draft.lock_state = "EDITABLE"
    command.result_ref = {
        "state": "BLOCKED_RULE",
        "code": code,
        "reason": reason,
        "approval_id": str(approval.id),
        "version": approval.version,
    }
    await session.flush()
    await record_audit_event(
        session,
        action="order.commit_blocked",
        result="BLOCKED",
        actor=actor,
        task_id=task.id,
        object_ref={"approval_id": approval.id, "draft_id": draft.id},
        after_ref={"code": code, "reason": reason, "state": "BLOCKED_RULE"},
        input_value={
            "version": approval.version,
            "content_hash": approval.content_hash,
        },
        created_at=now,
    )
    await append_task_event(
        session,
        task_id=task.id,
        event_type="task.order_commit_blocked",
        payload={"code": code, "reason": reason, "approval_id": str(approval.id)},
        emitted_at=now,
    )
    return OrderCommitResult("BLOCKED_RULE", reason=reason, code=code)


async def commit_approved_order(
    session: AsyncSession,
    *,
    task_id: UUID,
    command_id: UUID,
    approval_id: UUID,
    config: Settings = settings,
) -> OrderCommitResult:
    """固定 task→draft→approval→contract→balance→lines→counter→event 顺序锁定后提交。

    调用方须在 TaskExecutionLease.write_session() 内执行，任务行和当前 epoch
    由 Worker 租约先行锁定。重复调用优先返回已提交订单，不重复占用额度。
    """
    task = await session.scalar(
        select(AgentTask).where(AgentTask.id == task_id).with_for_update()
    )
    if task is None:
        raise OrderCommitRejected("任务不存在", "TASK_NOT_FOUND")
    draft = await session.scalar(
        select(Draft).where(Draft.task_id == task_id).with_for_update()
    )
    if draft is None:
        raise OrderCommitRejected("任务草稿不存在", "DRAFT_NOT_FOUND")
    approval = await session.scalar(
        select(TaskApproval)
        .where(
            TaskApproval.id == approval_id,
            TaskApproval.task_id == task_id,
        )
        .with_for_update()
    )
    command = await session.scalar(
        select(TaskCommand)
        .where(
            TaskCommand.id == command_id,
            TaskCommand.task_id == task_id,
        )
        .with_for_update()
    )
    if approval is None or command is None:
        raise OrderCommitRejected("批准或审批命令不存在", "APPROVAL_NOT_FOUND")

    # 提交事务已成功但图 checkpoint 尚未保存时，恢复执行返回原订单结果。
    existing = await session.scalar(select(Order).where(Order.task_id == task_id))
    if existing is not None:
        if (
            existing.approval_id != approval.id
            or existing.approved_version != approval.version
            or existing.approved_content_hash != approval.content_hash
            or existing.submit_key
            != _submit_key(
                task.id, approval.id, approval.version, approval.content_hash
            )
            or not _command_binds_approval(command, approval)
        ):
            raise OrderCommitRejected(
                "任务已由其他审批快照提交，拒绝不匹配的幂等回放",
                "ORDER_IDEMPOTENCY_CONFLICT",
            )
        result = OrderCommitResult(
            "COMPLETED", existing.id, existing.number, replayed=True
        )
        command.result_ref = {
            **result.as_dict(),
            "approval_id": str(existing.approval_id),
        }
        return result
    prior_result = command.result_ref or {}
    if prior_result.get("state") == "BLOCKED_RULE":
        return OrderCommitResult(
            "BLOCKED_RULE",
            reason=str(
                prior_result.get("reason") or "实时合同规则已变化，请修改草稿后重新审批"
            ),
            code=str(prior_result.get("code") or "CONTRACT_RULE_CHANGED"),
            replayed=True,
        )

    if (
        not _command_binds_approval(command, approval)
        or approval.invalidated_at is not None
        or approval.draft_id != draft.id
        or approval.version != draft.current_version
        or draft.lock_state not in {"SUBMITTING", "SUBMITTED"}
    ):
        raise OrderCommitRejected(
            "批准已过期、无权提交或草稿版本已变化", "APPROVAL_INVALID"
        )
    owner = await session.get(User, task.owner_id)
    owner_scopes = frozenset(
        await session.scalars(
            select(UserDepartmentScope.department_id).where(
                UserDepartmentScope.user_id == task.owner_id
            )
        )
    )
    owner_roles = frozenset(
        await session.scalars(
            select(UserRole.role).where(UserRole.user_id == task.owner_id)
        )
    )
    if (
        owner is None
        or not owner.active
        or BUSINESS_OPERATOR not in owner_roles
        or task.department_id not in owner_scopes
    ):
        raise OrderCommitRejected(
            "任务所有者已失效或部门范围已撤销", "TASK_OWNER_UNAVAILABLE"
        )
    actor = await _approver_context(
        session, approval=approval, task=task, config=config
    )
    snapshot = await load_draft(session, draft.id, version=approval.version)
    if (
        snapshot is None
        or snapshot.candidate is None
        or not snapshot.is_validated
        or snapshot.content_hash != approval.content_hash
    ):
        raise OrderCommitRejected("批准快照与当前草稿不一致", "APPROVAL_INVALID")

    candidate = snapshot.candidate
    if candidate.order_type != task.task_type:
        return await _blocked(
            session,
            task=task,
            draft=draft,
            approval=approval,
            command=command,
            actor=actor,
            code="TASK_ORDER_TYPE_MISMATCH",
            reason="草稿订单方向与任务方向不一致，请修改后重新审批",
        )
    if candidate.contract_id is None:
        return await _blocked(
            session,
            task=task,
            draft=draft,
            approval=approval,
            command=command,
            actor=actor,
            code="CONTRACT_REQUIRED",
            reason="草稿缺少有效合同，请补充后重新审批",
        )
    contract = await session.scalar(
        select(Contract).where(Contract.id == candidate.contract_id).with_for_update()
    )
    if contract is None:
        return await _blocked(
            session,
            task=task,
            draft=draft,
            approval=approval,
            command=command,
            actor=actor,
            code="CONTRACT_UNAVAILABLE",
            reason="合同已不可用，请修改草稿后重新审批",
        )
    if contract.department_id != task.department_id:
        raise OrderCommitRejected("合同不属于任务的当前部门", "DEPARTMENT_MISMATCH")
    # 合同余额和实际使用到的合同物料行必须先于编号计数器按固定次序锁定。
    balance = await session.scalar(
        select(ContractBalance)
        .where(ContractBalance.contract_id == contract.id)
        .with_for_update()
    )
    lines = list(
        await session.scalars(
            select(ContractLine)
            .where(ContractLine.contract_id == contract.id)
            .order_by(ContractLine.id)
            .with_for_update()
        )
    )
    line_by_key = {(line.material_id, line.unit): line for line in lines}
    requested_line_ids = sorted(
        {
            line_by_key[(item.material_id, item.unit)].id
            for item in candidate.items
            if item.material_id is not None
            and (item.material_id, item.unit) in line_by_key
        }
    )
    locked_line_balances = (
        list(
            await session.scalars(
                select(ContractLineBalance)
                .where(ContractLineBalance.contract_line_id.in_(requested_line_ids))
                .order_by(ContractLineBalance.contract_line_id)
                .with_for_update()
            )
        )
        if requested_line_ids
        else []
    )
    line_balance_by_id = {row.contract_line_id: row for row in locked_line_balances}
    missing_limited_balance = any(
        line_by_key[(item.material_id, item.unit)].max_quantity is not None
        and line_by_key[(item.material_id, item.unit)].id not in line_balance_by_id
        for item in candidate.items
        if item.material_id is not None and (item.material_id, item.unit) in line_by_key
    )
    if balance is None or missing_limited_balance:
        return await _blocked(
            session,
            task=task,
            draft=draft,
            approval=approval,
            command=command,
            actor=actor,
            code="QUOTA_LEDGER_UNAVAILABLE",
            reason="合同额度账缺失，无法安全提交，请联系管理员核对额度台账",
        )

    day = _current_business_date(config)
    precheck = await precheck_candidate(
        session, candidate=candidate, actor=actor, business_date=day
    )
    if precheck.issues or precheck.validated_draft is None:
        issue = precheck.issues[0] if precheck.issues else None
        return await _blocked(
            session,
            task=task,
            draft=draft,
            approval=approval,
            command=command,
            actor=actor,
            code=issue.code if issue else "DRAFT_INVALID",
            reason=issue.message
            if issue
            else "草稿不再满足订单提交规则，请修改后重新审批",
        )
    validated = precheck.validated_draft
    if validated.total_amount > contract.total_amount - balance.occupied_amount:
        return await _blocked(
            session,
            task=task,
            draft=draft,
            approval=approval,
            command=command,
            actor=actor,
            code="CONTRACT_AMOUNT_EXCEEDED",
            reason="合同剩余额度不足，请调整金额后重新审批",
        )

    item_lines: list[ContractLine] = []
    quantities_by_line: dict[UUID, Decimal] = {}
    item_amounts: list[Decimal] = []
    for item in validated.items:
        line = line_by_key.get((item.material_id, item.unit))
        if (
            line is None
            or item.material_id is None
            or item.quantity is None
            or item.unit_price is None
        ):
            return await _blocked(
                session,
                task=task,
                draft=draft,
                approval=approval,
                command=command,
                actor=actor,
                code="CONTRACT_LINE_UNAVAILABLE",
                reason="合同物料行已变化，请调整草稿后重新审批",
            )
        item_lines.append(line)
        quantities_by_line[line.id] = (
            quantities_by_line.get(line.id, Decimal(0)) + item.quantity
        )
        item_amounts.append(line_amount(item.quantity, item.unit_price))
    for line_id, quantity in quantities_by_line.items():
        line = next(line for line in item_lines if line.id == line_id)
        if line.max_quantity is not None:
            current = line_balance_by_id[line_id].occupied_quantity
            if quantity > line.max_quantity - current:
                return await _blocked(
                    session,
                    task=task,
                    draft=draft,
                    approval=approval,
                    command=command,
                    actor=actor,
                    code="QUANTITY_LIMIT_EXCEEDED",
                    reason="合同物料剩余数量不足，请调整后重新审批",
                )

    customer_code = "CUST"
    from app.models import Customer

    customer = await session.get(Customer, contract.customer_id)
    if customer is not None:
        customer_code = customer.code
    await session.execute(
        pg_insert(OrderNumberCounter)
        .values(
            business_date=day,
            type=task.task_type,
            department_id=task.department_id,
            next_value=1,
        )
        .on_conflict_do_nothing(
            index_elements=[
                OrderNumberCounter.business_date,
                OrderNumberCounter.type,
                OrderNumberCounter.department_id,
            ]
        )
    )
    counter = await session.scalar(
        select(OrderNumberCounter)
        .where(
            OrderNumberCounter.business_date == day,
            OrderNumberCounter.type == task.task_type,
            OrderNumberCounter.department_id == task.department_id,
        )
        .with_for_update()
    )
    if counter is None:
        raise RuntimeError("订单编号计数器无法创建")
    sequence = counter.next_value
    counter.next_value = sequence + 1
    order_id = uuid5(ORDER_NAMESPACE, f"{task.id}:{approval.id}")
    submit_key = _submit_key(
        task.id, approval.id, approval.version, approval.content_hash
    )
    number = _order_number(
        order_type=task.task_type,
        customer_code=customer_code,
        day=day,
        sequence=sequence,
    )
    order = Order(
        id=order_id,
        task_id=task.id,
        approval_id=approval.id,
        submit_key=submit_key,
        approved_version=approval.version,
        approved_content_hash=approval.content_hash,
        number=number,
        department_id=task.department_id,
        contract_id=contract.id,
        type=task.task_type,
        currency=validated.currency,
        total_amount=validated.total_amount,
        state="CREATED",
    )
    session.add(order)
    await session.flush()
    for index, (item, line, amount) in enumerate(
        zip(validated.items, item_lines, item_amounts, strict=True), start=1
    ):
        session.add(
            OrderItem(
                order_id=order.id,
                line_no=index,
                contract_line_id=line.id,
                material_id=item.material_id,
                raw_name=item.raw_name,
                specification=item.specification,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=item.unit_price,
                amount=amount.quantize(Decimal("0.01")),
            )
        )
    balance.occupied_amount += validated.total_amount
    balance.occupied_order_count += 1
    balance.version += 1
    for line_id, quantity in quantities_by_line.items():
        line_balance = line_balance_by_id.get(line_id)
        if line_balance is not None:
            line_balance.occupied_quantity += quantity
    occupation = QuotaOccupation(
        id=uuid4(),
        order_id=order.id,
        task_id=task.id,
        approval_id=approval.id,
        contract_id=contract.id,
        submit_key=submit_key,
        amount=validated.total_amount,
        quantities=[
            {"contract_line_id": str(line_id), "quantity": format(quantity, "f")}
            for line_id, quantity in sorted(
                quantities_by_line.items(), key=lambda item: str(item[0])
            )
        ],
    )
    session.add(occupation)
    draft.lock_state = "SUBMITTED"
    task.state = "COMPLETED"
    task.current_step = "completed"
    task.last_error = None
    task.projection_version += 1
    command.result_ref = {
        "state": "COMPLETED",
        "order_id": str(order.id),
        "order_number": order.number,
        "approval_id": str(approval.id),
        "submit_key": submit_key,
    }
    result = OrderCommitResult("COMPLETED", order.id, order.number)
    await session.flush()
    await record_audit_event(
        session,
        action="order.committed",
        result="SUCCESS",
        actor=actor,
        task_id=task.id,
        object_ref={
            "order_id": order.id,
            "order_number": order.number,
            "approval_id": approval.id,
        },
        after_ref={
            "state": "CREATED",
            "amount": validated.total_amount,
            "version": approval.version,
        },
        input_value={"submit_key": submit_key, "content_hash": approval.content_hash},
    )
    # 事件游标锁必须是本事务最后取得的业务锁。
    await append_task_event(
        session,
        task_id=task.id,
        event_type="task.order_committed",
        payload={
            "state": "COMPLETED",
            "order_id": str(order.id),
            "order_number": order.number,
            "projection_version": task.projection_version,
        },
    )
    return result
