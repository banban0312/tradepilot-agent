"""Excel 批次命令：父批次与每个有效订单组的独立持久任务。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import BUSINESS_OPERATOR, AuthContext, require_business_access
from app.models import (
    AgentTask,
    AuditEvent,
    Contract,
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
            "completed_count": sum(state == "COMPLETED" for state in states),
            "stopped_count": sum(state == "STOPPED" for state in states),
            "needs_correction_count": sum(
                item.preview_status == "NEEDS_CORRECTION" for item in items
            ),
            "over_quota_count": sum(
                item.preview_status == "OVER_QUOTA_REVIEW" for item in items
            ),
            "failed_count": sum(
                state in {"FAILED", "REJECTED", "BLOCKED_RULE"} for state in states
            ),
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
