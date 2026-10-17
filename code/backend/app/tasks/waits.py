"""业务等待投影与中断命令核验；LangGraph checkpoint 是执行真相。"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentTask, TaskCommand, TaskWait
from app.observability.service import append_task_event
from app.tasks.service import StaleExecutionEpoch, TaskExecutionLease


async def project_wait(
    lease: TaskExecutionLease, details: dict[str, object]
) -> TaskWait:
    """从 checkpoint 快照幂等更新 UI 投影，并按当前 epoch 保护写入。"""
    if details.get("task_id") != str(lease.task_id):
        raise ValueError("等待点 task_id 与执行租约不匹配")
    checkpoint_ref = details.get("checkpoint_id")
    interrupt_id = details.get("interrupt_id")
    kind = details.get("kind")
    if not all(
        isinstance(value, str) and value for value in (checkpoint_ref, interrupt_id)
    ):
        raise ValueError("等待点缺少 checkpoint 或 interrupt 标识")
    if kind not in {"input", "review"}:
        raise ValueError("等待点类型无效")
    requested_state = details.get("projection_state")
    wait_state = (
        "BLOCKED_RULE"
        if kind == "input" and requested_state == "BLOCKED_RULE"
        else "WAITING_INPUT"
        if kind == "input"
        else "WAITING_APPROVAL"
    )
    async with lease.write_session() as session:
        active = await session.scalar(
            select(TaskWait)
            .where(TaskWait.task_id == lease.task_id, TaskWait.active)
            .with_for_update()
        )
        if (
            active is not None
            and active.checkpoint_ref == checkpoint_ref
            and active.interrupt_id == interrupt_id
        ):
            active.payload = details
            wait = active
        else:
            if active is not None:
                active.active = False
                await session.flush()
            wait = TaskWait(
                id=uuid4(),
                task_id=lease.task_id,
                checkpoint_ref=checkpoint_ref,
                interrupt_id=interrupt_id,
                kind="INPUT" if kind == "input" else "APPROVAL",
                draft_version=details.get("draft_version")
                if isinstance(details.get("draft_version"), int)
                else None,
                payload=details,
                active=True,
            )
            session.add(wait)
        task = await session.get(AgentTask, lease.task_id, with_for_update=True)
        if task is None or task.execution_epoch != lease.epoch:
            raise StaleExecutionEpoch("等待状态投影 epoch 已过期")
        task.state = wait_state
        task.checkpoint_ref = checkpoint_ref
        task.current_step = "await_input" if kind == "input" else "await_review"
        task.projection_version += 1
        await session.flush()
        await append_task_event(
            session,
            task_id=lease.task_id,
            event_type="task.status_changed",
            payload={
                "state": wait_state,
                "projection_version": task.projection_version,
            },
        )
        return wait


async def bind_wait_command(
    session: AsyncSession,
    *,
    task_id: UUID,
    checkpoint_ref: str,
    interrupt_id: str,
    command_id: UUID,
    expected_version: int | None = None,
) -> TaskWait:
    """命令入队事务内绑定当前等待，防止陈旧页面提交旧中断。"""
    wait = await session.scalar(
        select(TaskWait)
        .where(
            TaskWait.task_id == task_id,
            TaskWait.active,
            TaskWait.checkpoint_ref == checkpoint_ref,
            TaskWait.interrupt_id == interrupt_id,
        )
        .with_for_update()
    )
    if wait is None:
        raise ValueError("任务等待点已变化或已被消费")
    command = await session.get(TaskCommand, command_id)
    if command is None or command.task_id != task_id:
        raise ValueError("恢复命令不属于当前任务")
    if command.target_interrupt_id != interrupt_id:
        raise ValueError("恢复命令与当前 interrupt 不匹配")
    if expected_version is not None and wait.draft_version != expected_version:
        raise ValueError("恢复命令草稿版本已过期")
    if wait.pending_command_id not in (None, command_id):
        raise ValueError("当前等待点已有其他恢复命令")
    wait.pending_command_id = command_id
    return wait


async def consume_wait_command(
    lease: TaskExecutionLease,
    *,
    command_id: UUID,
    checkpoint_ref: str,
    interrupt_id: str,
) -> None:
    """图已成功恢复后消费命令；执行期间崩溃仍可由相同 checkpoint 重放。"""
    async with lease.write_session() as session:
        wait = await session.scalar(
            select(TaskWait)
            .where(
                TaskWait.task_id == lease.task_id,
                TaskWait.checkpoint_ref == checkpoint_ref,
                TaskWait.interrupt_id == interrupt_id,
            )
            .with_for_update()
        )
        if wait is None:
            raise ValueError("恢复等待投影不存在")
        if wait.consumed_command_id == command_id:
            return
        if not wait.active or wait.pending_command_id != command_id:
            raise ValueError("恢复命令不是当前等待点的待执行命令")
        wait.consumed_command_id = command_id
        wait.active = False


async def complete_resume_projection(
    lease: TaskExecutionLease,
    *,
    command_id: UUID,
    checkpoint_ref: str,
    interrupt_id: str,
    result: Mapping[str, object],
    next_wait: dict[str, object] | None,
) -> None:
    """原等待消费与下一状态投影同事务提交，关闭恢复后的崩溃窗口。"""
    async with lease.write_session() as session:
        previous = await session.scalar(
            select(TaskWait)
            .where(
                TaskWait.task_id == lease.task_id,
                TaskWait.checkpoint_ref == checkpoint_ref,
                TaskWait.interrupt_id == interrupt_id,
            )
            .with_for_update()
        )
        if previous is None:
            raise ValueError("恢复等待投影不存在")
        if previous.consumed_command_id == command_id:
            pass
        elif previous.active and previous.pending_command_id == command_id:
            previous.consumed_command_id = command_id
            previous.active = False
            await session.flush()
        else:
            raise ValueError("恢复命令不是当前等待点的待执行命令")

        task = await session.get(AgentTask, lease.task_id, with_for_update=True)
        if task is None or task.execution_epoch != lease.epoch:
            raise StaleExecutionEpoch("恢复状态投影 epoch 已过期")
        if next_wait is not None:
            if next_wait.get("task_id") != str(lease.task_id):
                raise ValueError("下一等待点不属于当前任务")
            next_checkpoint = next_wait.get("checkpoint_id")
            next_interrupt = next_wait.get("interrupt_id")
            next_kind = next_wait.get("kind")
            if not isinstance(next_checkpoint, str) or not isinstance(
                next_interrupt, str
            ):
                raise ValueError("下一等待点缺少检查点或中断标识")
            if next_kind not in {"input", "review"}:
                raise ValueError("下一等待点类型无效")
            active = await session.scalar(
                select(TaskWait)
                .where(TaskWait.task_id == lease.task_id, TaskWait.active)
                .with_for_update()
            )
            if active is not None and (
                active.checkpoint_ref == next_checkpoint
                and active.interrupt_id == next_interrupt
            ):
                active.payload = next_wait
            else:
                if active is not None:
                    active.active = False
                    await session.flush()
                session.add(
                    TaskWait(
                        id=uuid4(),
                        task_id=lease.task_id,
                        checkpoint_ref=next_checkpoint,
                        interrupt_id=next_interrupt,
                        kind="INPUT" if next_kind == "input" else "APPROVAL",
                        draft_version=(
                            next_wait.get("draft_version")
                            if isinstance(next_wait.get("draft_version"), int)
                            else None
                        ),
                        payload=next_wait,
                        active=True,
                    )
                )
            next_state = next_wait.get("projection_state")
            task.state = (
                "BLOCKED_RULE"
                if next_kind == "input" and next_state == "BLOCKED_RULE"
                else "WAITING_INPUT"
                if next_kind == "input"
                else "WAITING_APPROVAL"
            )
            task.checkpoint_ref = next_checkpoint
            task.current_step = (
                "await_input" if next_kind == "input" else "await_review"
            )
        else:
            next_state = result.get("projection_state")
            allowed = {
                "WAITING_INPUT",
                "WAITING_APPROVAL",
                "SUBMITTING",
                "BLOCKED_RULE",
                "FAILED",
                "COMPLETED",
                "REJECTED",
            }
            if next_state not in allowed:
                raise ValueError("恢复后的任务状态投影无效")
            task.state = str(next_state)
            task.checkpoint_ref = str(result.get("checkpoint_ref") or checkpoint_ref)
            task.current_step = {
                "SUBMITTING": "commit_order",
                "COMPLETED": "completed",
                "BLOCKED_RULE": "prepare_input_wait",
                "REJECTED": "rejected",
            }.get(task.state, "await_review")
        task.projection_version += 1
        await append_task_event(
            session,
            task_id=lease.task_id,
            event_type="task.status_changed",
            payload={
                "state": task.state,
                "projection_version": task.projection_version,
            },
        )
