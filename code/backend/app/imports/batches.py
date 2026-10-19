"""Excel 批次命令：父批次与每个有效订单组的独立持久任务。"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import BUSINESS_OPERATOR, AuthContext, require_business_access
from app.drafts.types import CandidateOrder, candidate_amounts
from app.models import (
    AgentTask,
    AuditEvent,
    Contract,
    ContractBalance,
    ImportBatch,
    ImportBatchItem,
    InputArtifact,
    Order,
    Project,
    TaskArtifact,
    TaskCommand,
    TaskJob,
    TaskSession,
    User,
)
from app.observability.service import append_task_event
from app.tasks.service import IdempotencyConflict, create_task, enqueue_command


class BatchIdempotencyConflict(ValueError):
    """相同批次键被用于另一附件或请求内容。"""


class BatchItemVersionConflict(ValueError):
    """批次分组已被另一编辑请求更新。"""


def _digest_request(artifact: InputArtifact) -> str:
    return hashlib.sha256(f"{artifact.id}:{artifact.sha256}".encode()).hexdigest()


def _group_input(group: dict[str, Any], contract_number: str, project_name: str) -> str:
    candidate = group["candidate"]
    order_type = "销售" if candidate["order_type"] == "SALES" else "采购"
    lines = [
        "请为以下 Excel 订单组建立草稿。只采用给定字段；不猜测、不补造缺失信息，并通过现有业务工具查询和核验主数据。",
        f"订单组：{group['group_id']}；来源工作表：{group['sheet']}；来源行：{','.join(map(str, group['rows']))}。",
        f"订单类型：{order_type}；合同编号：{contract_number}；项目：{project_name}。",
        f"收货地址：{candidate['address']}；联系人：{candidate['contact']}；交期：{candidate['delivery_date']}。",
        "明细：",
    ]
    for index, item in enumerate(candidate["items"], start=1):
        evidence = next(
            (
                row
                for row in candidate["evidence"]
                if row.get("field_path") == f"items[{index - 1}].raw_name"
            ),
            None,
        )
        locator = evidence.get("locator", {}) if evidence else {}
        source = f"{locator.get('sheet', group['sheet'])}!{locator.get('cell', '?')}"
        lines.append(
            f"{index}. 来源 {source}；物料名称 {item['raw_name']}；规格 {item['specification']}；"
            f"数量 {item['quantity']} {item['unit']}；含税单价 {item['unit_price']}。"
        )
    return "\n".join(lines)


async def create_batch(
    session: AsyncSession,
    *,
    context: AuthContext,
    artifact: InputArtifact,
    groups: list[dict[str, Any]],
    idempotency_key: str,
) -> tuple[ImportBatch, bool]:
    """单事务创建父批次、每组记录、有效子任务及队列作业。"""
    require_business_access(context, artifact.department_id)
    if BUSINESS_OPERATOR not in context.roles:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCESS_DENIED", "message": "当前账号无权创建批次"},
        )
    user = await session.get(User, context.user_id)
    if user is None or not user.active or user.department_id != artifact.department_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "附件不存在"},
        )

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"import-batch:{context.user_id}:{idempotency_key}"},
    )
    batch_id = uuid5(context.user_id, f"tradepilot/import-batch/{idempotency_key}")
    existing = await session.scalar(
        select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update()
    )
    request_hash = _digest_request(artifact)
    if existing is not None:
        if (
            existing.owner_id != context.user_id
            or existing.request_hash != request_hash
            or existing.artifact_id != artifact.id
        ):
            raise BatchIdempotencyConflict("幂等键已用于其他导入文件")
        return existing, True

    batch = ImportBatch(
        id=batch_id,
        owner_id=context.user_id,
        department_id=artifact.department_id,
        artifact_id=artifact.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        state="QUEUED",
        stop_requested=False,
    )
    session.add(batch)
    await session.flush()

    task_session = TaskSession(
        id=uuid4(),
        owner_id=context.user_id,
        title=f"Excel 导入批次 {str(batch_id)[:8]}",
    )
    session.add(task_session)
    await session.flush()

    ready_groups = [
        group for group in groups if group.get("status") == "READY_FOR_REVIEW"
    ]
    candidate_refs: dict[tuple[str, str], tuple[dict[str, Any], UUID, UUID]] = {}
    contract_ids: set[UUID] = set()
    project_ids: set[UUID] = set()
    for group in ready_groups:
        group_key = (str(group["sheet"]), str(group["group_id"]))
        candidate = group.get("candidate")
        if not isinstance(candidate, dict):
            raise TypeError("有效订单组缺少已校验的候选数据")
        try:
            contract_id = UUID(candidate["contract_id"])
            project_id = UUID(candidate["project_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("有效订单组的合同或项目关联无效") from exc
        candidate_refs[group_key] = (candidate, contract_id, project_id)
        contract_ids.add(contract_id)
        project_ids.add(project_id)

    contracts = {
        contract.id: contract
        for contract in (
            await session.scalars(
                select(Contract).where(
                    Contract.id.in_(contract_ids),
                    Contract.department_id == artifact.department_id,
                )
            )
        ).all()
    }
    projects = {
        project.id: project
        for project in (
            await session.scalars(select(Project).where(Project.id.in_(project_ids)))
        ).all()
    }

    for group in groups:
        group_id = str(group["group_id"])
        sheet_name = str(group["sheet"])
        source_rows = [int(row) for row in group.get("rows", [])]
        preview_status = str(group["status"])
        item = ImportBatchItem(
            id=uuid4(),
            batch_id=batch.id,
            group_id=group_id,
            sheet_name=sheet_name,
            source_rows=source_rows,
            preview_status=preview_status,
            issues=group.get("issues", []),
            candidate_json=group.get("candidate", {}),
        )
        session.add(item)
        if preview_status != "READY_FOR_REVIEW":
            continue

        candidate, contract_id, project_id = candidate_refs[(sheet_name, group_id)]
        contract = contracts.get(contract_id)
        project = projects.get(project_id)
        if (
            contract is None
            or project is None
            or project.department_id != artifact.department_id
            or contract.project_id != project.id
            or contract.status != "ACTIVE"
            or contract.type != candidate.get("order_type")
            or not (
                contract.valid_from
                <= datetime.fromisoformat(candidate["delivery_date"]).date()
                <= contract.valid_to
            )
        ):
            raise ValueError("有效订单组的合同或项目在创建期间发生变化，请重新预览")

        stable_group = hashlib.sha256(f"{sheet_name}\0{group_id}".encode()).hexdigest()
        child_task = await create_task(
            session,
            owner_id=context.user_id,
            department_id=artifact.department_id,
            task_type=str(candidate["order_type"]),
            title=f"{group_id[:80]}（批量导入）",
            task_id=uuid5(batch.id, f"tradepilot/import-child/{stable_group}"),
            session_id=task_session.id,
        )
        item.task_id = child_task.id
        session.add(TaskArtifact(task_id=child_task.id, artifact_id=artifact.id))
        group_text = _group_input(group, contract.number, project.name)
        request_id = f"batch:{batch.id.hex}:{stable_group[:24]}"
        command = await enqueue_command(
            session,
            task_id=child_task.id,
            actor_id=context.user_id,
            kind="START",
            payload={
                "text": group_text,
                "input_type": "EXCEL_GROUP",
                "order_type": candidate["order_type"],
                "import_batch_id": str(batch.id),
                "source_rows": source_rows,
            },
            request_id=request_id,
        )
        if command.replayed:
            raise IdempotencyConflict("新批次意外关联到已有队列命令")
        await append_task_event(
            session,
            task_id=child_task.id,
            event_type="task.status_changed",
            payload={
                "state": "QUEUED",
                "projection_version": child_task.projection_version,
            },
        )

    session.add(
        AuditEvent(
            id=uuid4(),
            task_id=None,
            actor_id=context.user_id,
            action="import.batch_created",
            object_ref={"batch_id": str(batch.id), "artifact_id": str(artifact.id)},
            after_ref={
                "group_count": len(groups),
                "task_count": sum(
                    group["status"] == "READY_FOR_REVIEW" for group in groups
                ),
            },
            result="SUCCESS",
            input_hash=artifact.sha256,
        )
    )
    await session.flush()
    return batch, False


def _candidate_field(candidate: CandidateOrder, field: str) -> object:
    match = re.fullmatch(
        r"items\[(\d+)]\.(raw_name|specification|quantity|unit|unit_price)", field
    )
    if match is not None:
        index = int(match.group(1))
        return (
            getattr(candidate.items[index], match.group(2))
            if index < len(candidate.items)
            else None
        )
    return getattr(candidate, field, None)


async def correct_batch_item(
    session: AsyncSession,
    *,
    batch_id: UUID,
    sheet_name: str,
    group_id: str,
    expected_version: int,
    candidate_data: dict[str, Any],
    context: AuthContext,
) -> dict[str, object]:
    """锁定一个异常组、保存编辑版本并重新检查后再创建独立子任务。"""
    if BUSINESS_OPERATOR not in context.roles:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCESS_DENIED", "message": "当前账号无权修改批次"},
        )
    batch = await session.scalar(
        select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update()
    )
    if batch is None or batch.owner_id != context.user_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "批次不存在"},
        )
    require_business_access(context, batch.department_id)
    if batch.stop_requested:
        raise HTTPException(
            status_code=409,
            detail={"code": "BATCH_STOPPED", "message": "批次已停止，不能修改订单组"},
        )
    item = await session.scalar(
        select(ImportBatchItem)
        .where(
            ImportBatchItem.batch_id == batch.id,
            ImportBatchItem.sheet_name == sheet_name,
            ImportBatchItem.group_id == group_id,
        )
        .with_for_update()
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "订单组不存在"},
        )
    if item.task_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_ALREADY_CREATED",
                "message": "该订单组已建立任务，请在任务草稿中修改",
            },
        )
    if item.edit_version != expected_version:
        raise BatchItemVersionConflict("订单组版本已变化，请刷新后重试")
    try:
        original = CandidateOrder.model_validate(item.candidate_json)
        candidate = CandidateOrder.model_validate(candidate_data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INPUT_INVALID", "message": "订单组修改内容无效"},
        ) from exc
    immutable = (
        "order_type",
        "contract_id",
        "customer_id",
        "project_id",
        "currency",
        "business_fields",
        "evidence",
    )
    if any(getattr(original, name) != getattr(candidate, name) for name in immutable):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PATCH_FIELD_FORBIDDEN",
                "message": "只能修改订单日期、地址、联系人和 Excel 物料行字段",
            },
        )
    if len(original.items) != len(candidate.items) or any(
        left.material_id != right.material_id
        for left, right in zip(original.items, candidate.items, strict=True)
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PATCH_FIELD_FORBIDDEN",
                "message": "不能修改物料关联或增删 Excel 明细行",
            },
        )

    issues = []
    resolvable = {
        "MISSING_FIELD",
        "INVALID_NUMBER",
        "INVALID_QUANTITY",
        "INVALID_UNIT_PRICE",
        "INVALID_CELL_VALUE",
        "AMOUNT_OUT_OF_RANGE",
    }
    for issue in item.issues:
        # 交期可在表格中修改；日期范围错误必须按本次候选数据重新计算。
        if issue.get("code") == "CONTRACT_DATE_CONFLICT":
            continue
        field = str(issue.get("field", ""))
        value = _candidate_field(candidate, field)
        resolved = value is not None and value != ""
        if field.endswith((".quantity", ".unit_price")):
            try:
                resolved = value is not None and value > 0  # type: ignore[operator]
            except TypeError:
                resolved = False
        if issue.get("code") in resolvable and resolved:
            continue
        issues.append(issue)

    def add_issue(code: str, field: str, message: str) -> None:
        if not any(
            issue.get("code") == code and issue.get("field") == field
            for issue in issues
        ):
            row = item.source_rows[0] if item.source_rows else None
            issues.append(
                {
                    "code": code,
                    "field": field,
                    "message": message,
                    "locator": {
                        "sheet": sheet_name,
                        "row": row,
                        "column": None,
                        "cell": None,
                    },
                }
            )

    for field, label in (
        ("order_type", "订单类型"),
        ("contract_id", "合同"),
        ("customer_id", "客户"),
        ("project_id", "项目"),
        ("delivery_date", "交期"),
        ("address", "收货地址"),
        ("contact", "联系人"),
    ):
        value = _candidate_field(candidate, field)
        if value is None or value == "":
            add_issue("MISSING_FIELD", field, f"缺少{label}")
    if not candidate.items:
        add_issue("MISSING_ITEMS", "items", "至少需要一条订单明细")
    for index, line in enumerate(candidate.items):
        for field, label in (
            ("raw_name", "物料名称"),
            ("specification", "规格"),
            ("unit", "单位"),
        ):
            if not getattr(line, field):
                add_issue(
                    "MISSING_FIELD",
                    f"items[{index}].{field}",
                    f"缺少第 {index + 1} 行{label}",
                )
        if line.quantity is None or line.quantity <= 0:
            add_issue("INVALID_QUANTITY", f"items[{index}].quantity", "数量必须大于 0")
        if line.unit_price is None or line.unit_price <= 0:
            add_issue(
                "INVALID_UNIT_PRICE", f"items[{index}].unit_price", "含税单价必须大于 0"
            )

    contract = (
        await session.scalar(
            select(Contract).where(
                Contract.id == candidate.contract_id,
                Contract.department_id == batch.department_id,
            )
        )
        if candidate.contract_id
        else None
    )
    project = (
        await session.get(Project, candidate.project_id)
        if candidate.project_id
        else None
    )
    if contract is None:
        add_issue(
            "CONTRACT_NOT_FOUND",
            "contract_id",
            "合同无法匹配，请修正 Excel 合同编号后重新上传",
        )
    else:
        if contract.status != "ACTIVE":
            add_issue("CONTRACT_INACTIVE", "contract_id", "合同已停用，不能用于新订单")
        if candidate.order_type != contract.type:
            add_issue(
                "CONTRACT_TYPE_CONFLICT", "order_type", "订单类型与合同类型不一致"
            )
        if (
            candidate.delivery_date is not None
            and not contract.valid_from <= candidate.delivery_date <= contract.valid_to
        ):
            add_issue(
                "CONTRACT_DATE_CONFLICT", "delivery_date", "交期不在合同有效日期范围内"
            )
        if (
            project is None
            or project.department_id != contract.department_id
            or project.id != contract.project_id
        ):
            add_issue(
                "PROJECT_CONTRACT_CONFLICT", "project_id", "项目与合同关联项目不一致"
            )
        if candidate.customer_id != contract.customer_id:
            add_issue(
                "CONTRACT_CUSTOMER_CONFLICT", "customer_id", "客户与合同签约客户不一致"
            )
    try:
        _, total = candidate_amounts(candidate)
    except ValueError:
        total = None
        add_issue("AMOUNT_OUT_OF_RANGE", "items", "订单金额超出系统可处理范围")
    over_quota = False
    if contract is not None and total is not None:
        balance = await session.get(ContractBalance, contract.id)
        if balance is None:
            add_issue(
                "QUOTA_UNAVAILABLE",
                "contract_id",
                "合同额度数据暂不可用，当前不能创建子任务",
            )
        else:
            sibling_items = await session.scalars(
                select(ImportBatchItem).where(
                    ImportBatchItem.batch_id == batch.id,
                    ImportBatchItem.id != item.id,
                    ImportBatchItem.task_id.is_(None),
                    ImportBatchItem.preview_status.in_(
                        ("READY_FOR_REVIEW", "OVER_QUOTA_REVIEW")
                    ),
                )
            )
            pending_amount = Decimal(0)
            for sibling in sibling_items:
                try:
                    sibling_candidate = CandidateOrder.model_validate(
                        sibling.candidate_json
                    )
                    if (
                        sibling_candidate.contract_id == contract.id
                        and not sibling.issues
                    ):
                        _, sibling_total = candidate_amounts(sibling_candidate)
                        pending_amount += sibling_total
                except (TypeError, ValueError):
                    continue
            remaining = max(
                Decimal(0),
                contract.total_amount - balance.occupied_amount - pending_amount,
            )
            over_quota = total > remaining

    item.candidate_json = candidate.model_dump(mode="json")
    item.issues = issues
    item.preview_status = (
        "NEEDS_CORRECTION"
        if issues
        else "OVER_QUOTA_REVIEW"
        if over_quota
        else "READY_FOR_REVIEW"
    )
    item.edit_version += 1
    if item.preview_status == "READY_FOR_REVIEW":
        artifact = await session.get(InputArtifact, batch.artifact_id)
        if (
            artifact is None
            or artifact.owner_id != batch.owner_id
            or contract is None
            or project is None
        ):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RESOURCE_NOT_FOUND",
                    "message": "来源附件或主数据不存在",
                },
            )
        stable_group = hashlib.sha256(f"{sheet_name}\0{group_id}".encode()).hexdigest()
        task_session = TaskSession(
            id=uuid4(), owner_id=batch.owner_id, title=f"Excel 批次 {str(batch.id)[:8]}"
        )
        session.add(task_session)
        await session.flush()
        task = await create_task(
            session,
            owner_id=batch.owner_id,
            department_id=batch.department_id,
            task_type=candidate.order_type or contract.type,
            title=f"{group_id[:80]}（批量导入）",
            task_id=uuid5(batch.id, f"tradepilot/import-child/{stable_group}"),
            session_id=task_session.id,
        )
        item.task_id = task.id
        session.add(TaskArtifact(task_id=task.id, artifact_id=artifact.id))
        group_data = {
            "group_id": group_id,
            "sheet": sheet_name,
            "rows": item.source_rows,
            "candidate": candidate.model_dump(mode="json"),
        }
        command = await enqueue_command(
            session,
            task_id=task.id,
            actor_id=context.user_id,
            kind="START",
            payload={
                "text": _group_input(group_data, contract.number, project.name),
                "input_type": "EXCEL_GROUP",
                "order_type": candidate.order_type,
                "import_batch_id": str(batch.id),
                "source_rows": item.source_rows,
            },
            request_id=f"batch:{batch.id.hex}:{stable_group[:24]}",
        )
        if command.replayed:
            raise BatchIdempotencyConflict("订单组已存在启动命令")
        await append_task_event(
            session,
            task_id=task.id,
            event_type="task.status_changed",
            payload={"state": "QUEUED", "projection_version": task.projection_version},
        )
    session.add(
        AuditEvent(
            id=uuid4(),
            task_id=item.task_id,
            actor_id=context.user_id,
            action="import.batch_item_corrected",
            object_ref={
                "batch_id": str(batch.id),
                "sheet": sheet_name,
                "group_id": group_id,
                "edit_version": item.edit_version,
            },
            after_ref={
                "preview_status": item.preview_status,
                "task_id": str(item.task_id) if item.task_id else None,
            },
            result="SUCCESS",
        )
    )
    await session.flush()
    return await load_batch(session, batch_id=batch.id, context=context)


async def load_batch(
    session: AsyncSession, *, batch_id: UUID, context: AuthContext
) -> dict[str, object]:
    batch = await session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "批次不存在"},
        )
    require_business_access(context, batch.department_id)
    items = list(
        (
            await session.scalars(
                select(ImportBatchItem)
                .where(ImportBatchItem.batch_id == batch.id)
                .order_by(ImportBatchItem.created_at, ImportBatchItem.id)
            )
        ).all()
    )
    task_ids = [item.task_id for item in items if item.task_id is not None]
    tasks = (
        {
            task.id: task
            for task in (
                await session.scalars(
                    select(AgentTask).where(AgentTask.id.in_(task_ids))
                )
            ).all()
        }
        if task_ids
        else {}
    )
    running_tasks = (
        set(
            (
                await session.scalars(
                    select(TaskJob.task_id).where(
                        TaskJob.task_id.in_(task_ids), TaskJob.state == "RUNNING"
                    )
                )
            ).all()
        )
        if task_ids
        else set()
    )
    orders = (
        {
            order.task_id: order
            for order in (
                await session.scalars(select(Order).where(Order.task_id.in_(task_ids)))
            ).all()
        }
        if task_ids
        else {}
    )

    group_results: list[dict[str, object]] = []
    states: list[str] = []
    for item in items:
        task = tasks.get(item.task_id) if item.task_id else None
        task_state = (
            "STOPPED"
            if item.stop_result == "NOT_STARTED"
            else task.state
            if task
            else item.preview_status
        )
        if task is not None and task.state == "QUEUED" and task.id in running_tasks:
            task_state = "STARTING"
        states.append(task_state)
        order = orders.get(item.task_id) if item.task_id else None
        group_results.append(
            {
                "group_id": item.group_id,
                "sheet": item.sheet_name,
                "source_rows": item.source_rows,
                "preview_status": item.preview_status,
                "state": task_state,
                "task_id": str(item.task_id) if item.task_id else None,
                "task_status": task.state if task else None,
                "execution_error": task.last_error if task else None,
                "order_id": str(order.id) if order else None,
                "order_number": order.number if order else None,
                "issues": item.issues,
                "stop_result": item.stop_result,
                "candidate": item.candidate_json if task is None else None,
                "edit_version": item.edit_version,
            }
        )

    pending = any(
        state
        in {
            "QUEUED",
            "STARTING",
            "RUNNING",
            "WAITING_INPUT",
            "WAITING_APPROVAL",
            "SUBMITTING",
        }
        for state in states
    )
    if batch.stop_requested:
        batch_state = "STOPPING" if pending else "STOPPED"
    elif pending:
        batch_state = (
            "RUNNING" if any(state != "QUEUED" for state in states) else "QUEUED"
        )
    elif states and all(state == "COMPLETED" for state in states):
        batch_state = "COMPLETED"
    else:
        batch_state = "PARTIAL"

    if batch.state != batch_state:
        batch.state = batch_state
        batch.updated_at = datetime.now(UTC)
        await session.flush()

    return {
        "batch_id": str(batch.id),
        "artifact_id": str(batch.artifact_id),
        "state": batch_state,
        "stop_requested": batch.stop_requested,
        "created_at": batch.created_at.isoformat(),
        "summary": {
            "group_count": len(items),
            "task_count": len(task_ids),
            "running_count": sum(
                state in {"STARTING", "RUNNING", "SUBMITTING"} for state in states
            ),
            "waiting_input_count": sum(state == "WAITING_INPUT" for state in states),
            "waiting_approval_count": sum(
                state == "WAITING_APPROVAL" for state in states
            ),
            "result_check_count": sum(state == "SUBMITTING" for state in states),
            "completed_count": sum(state == "COMPLETED" for state in states),
            "stopped_count": sum(state == "STOPPED" for state in states),
            "needs_correction_count": sum(
                item.preview_status == "NEEDS_CORRECTION" for item in items
            ),
            "over_quota_count": sum(
                item.preview_status == "OVER_QUOTA_REVIEW" for item in items
            ),
            "blocked_count": sum(state == "BLOCKED_RULE" for state in states),
            "failed_count": sum(state in {"FAILED", "REJECTED"} for state in states),
        },
        "groups": group_results,
    }


async def stop_batch(
    session: AsyncSession, *, batch_id: UUID, context: AuthContext
) -> dict[str, object]:
    if BUSINESS_OPERATOR not in context.roles:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCESS_DENIED", "message": "当前账号无权停止批次"},
        )
    # 与 try_claim_job 使用相同的全局短事务锁，确定某项任务是在停止前还是停止后领取。
    await session.execute(text("SELECT pg_advisory_xact_lock(74192831)"))
    batch = await session.scalar(
        select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update()
    )
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "批次不存在"},
        )
    require_business_access(context, batch.department_id)
    batch.stop_requested = True
    items = list(
        (
            await session.scalars(
                select(ImportBatchItem)
                .where(
                    ImportBatchItem.batch_id == batch.id,
                    ImportBatchItem.task_id.is_not(None),
                )
                .with_for_update()
            )
        ).all()
    )
    task_ids = [item.task_id for item in items if item.task_id is not None]
    jobs = (
        list(
            (
                await session.scalars(
                    select(TaskJob)
                    .where(TaskJob.task_id.in_(task_ids), TaskJob.state == "READY")
                    .with_for_update()
                )
            ).all()
        )
        if task_ids
        else []
    )
    ready_task_ids = {job.task_id for job in jobs}
    stopped_task_ids: set[UUID] = set()
    tasks_by_id: dict[UUID, AgentTask] = {}
    if ready_task_ids:
        tasks = list(
            (
                await session.scalars(
                    select(AgentTask)
                    .where(
                        AgentTask.id.in_(ready_task_ids), AgentTask.state == "QUEUED"
                    )
                    .with_for_update()
                )
            ).all()
        )
        tasks_by_id = {task.id: task for task in tasks}
        for job in jobs:
            if job.task_id not in tasks_by_id:
                continue
            stopped_task_ids.add(job.task_id)
            job.state = "FAILED"
            job.worker_id = None
            job.heartbeat_at = None
            task = tasks_by_id[job.task_id]
            task.state = "FAILED"
            task.last_error = {
                "code": "BATCH_STOPPED",
                "message": "批次已停止，任务尚未开始",
            }
            task.projection_version += 1
            await append_task_event(
                session,
                task_id=task.id,
                event_type="task.status_changed",
                payload={
                    "state": "FAILED",
                    "reason": "BATCH_STOPPED",
                    "projection_version": task.projection_version,
                },
            )
    items_by_task = {item.task_id: item for item in items if item.task_id is not None}
    for task_id in stopped_task_ids:
        item = items_by_task.get(task_id)
        if item is not None:
            item.stop_result = "NOT_STARTED"
    if stopped_task_ids:
        ready_commands = list(
            (
                await session.scalars(
                    select(TaskCommand).where(
                        TaskCommand.task_id.in_(stopped_task_ids),
                        TaskCommand.state == "READY",
                    )
                )
            ).all()
        )
        for command in ready_commands:
            command.state = "REJECTED"
            command.result_ref = {"code": "BATCH_STOPPED", "state": "STOPPED"}

    for item in items:
        if (
            item.task_id is not None
            and item.task_id not in stopped_task_ids
            and item.stop_result != "NOT_STARTED"
        ):
            item.stop_result = "ALREADY_STARTED"
    session.add(
        AuditEvent(
            id=uuid4(),
            task_id=None,
            actor_id=context.user_id,
            action="import.batch_stop_requested",
            object_ref={"batch_id": str(batch.id)},
            after_ref={
                "stopped_before_start": sum(
                    item.stop_result == "NOT_STARTED" for item in items
                ),
                "already_started_or_finished": sum(
                    item.stop_result == "ALREADY_STARTED" for item in items
                ),
            },
            result="SUCCESS",
        )
    )
    await session.flush()
    return await load_batch(session, batch_id=batch.id, context=context)
