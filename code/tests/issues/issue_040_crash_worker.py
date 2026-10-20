"""故障注入子进程：在正式订单事务提交边界强制退出。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import UUID

from sqlalchemy import select

from app.database import async_session_factory
from app.models import AgentTask, TaskWait
from app.orders.commit import commit_approved_order


async def _run(payload: dict[str, str]) -> None:
    mode = payload["mode"]
    if mode == "inspect_wait":
        task_id = UUID(payload["task_id"])
        async with async_session_factory() as session:
            task = await session.get(AgentTask, task_id)
            wait = await session.scalar(
                select(TaskWait).where(
                    TaskWait.task_id == task_id,
                    TaskWait.active.is_(True),
                )
            )
            if (
                task is None
                or wait is None
                or task.state != "WAITING_APPROVAL"
                or wait.kind != "APPROVAL"
                or not task.graph_version
                or not task.checkpoint_ref
                or task.checkpoint_ref != wait.checkpoint_ref
            ):
                raise AssertionError("新进程无法回查持久化的审批等待点")
            print(
                json.dumps(
                    {
                        "task_id": str(task.id),
                        "task_state": task.state,
                        "graph_version": task.graph_version,
                        "task_checkpoint_ref": task.checkpoint_ref,
                        "wait_checkpoint_ref": wait.checkpoint_ref,
                        "wait_kind": wait.kind,
                        "draft_version": wait.draft_version,
                    }
                )
            )
        return

    expected = {"before": 31, "after": 32}.get(mode)
    if expected is None:
        raise ValueError("仅允许事务提交前或提交后故障注入")

    async with async_session_factory() as session, session.begin():
        await commit_approved_order(
            session,
            task_id=UUID(payload["task_id"]),
            command_id=UUID(payload["command_id"]),
            approval_id=UUID(payload["approval_id"]),
        )
        if mode == "before":
            # 立即终止进程而不退出事务上下文；PostgreSQL 应回滚未提交事务。
            os._exit(expected)

    # 事务已确认提交，但进程在发送任何成功响应前退出，模拟结果未知。
    os._exit(expected)


if __name__ == "__main__":
    asyncio.run(_run(json.loads(sys.stdin.buffer.read())))
