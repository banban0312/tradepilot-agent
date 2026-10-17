"""按保留期清理附件对象；保留审计元数据并保护活动任务和订单证据。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial

import anyio
from sqlalchemy import exists, select

from app.config import settings
from app.database import async_session_factory, engine
from app.models import AgentTask, FieldEvidence, InputArtifact, Order, TaskArtifact
from app.storage.files import storage_path

_ACTIVE_TASK_STATES = (
    "QUEUED",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "SUBMITTING",
    "BLOCKED_RULE",
)


async def cleanup_expired_artifacts(*, now: datetime | None = None) -> int:
    """先事务性标记可清理记录，再删除对象；失败后可安全重试。"""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=settings.artifact_retention_days)
    removable: list[InputArtifact] = []
    async with async_session_factory() as session, session.begin():
        artifacts = list(
            (
                await session.scalars(
                    select(InputArtifact)
                    .where(
                        InputArtifact.state.in_(
                            ("STORED", "PARSED", "FAILED", "DELETED")
                        ),
                        InputArtifact.created_at < cutoff,
                    )
                    .order_by(InputArtifact.created_at, InputArtifact.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for artifact in artifacts:
            if artifact.state != "DELETED":
                active_link = exists(
                    select(TaskArtifact.task_id)
                    .join(AgentTask, AgentTask.id == TaskArtifact.task_id)
                    .where(
                        TaskArtifact.artifact_id == artifact.id,
                        AgentTask.state.in_(_ACTIVE_TASK_STATES),
                    )
                )
                order_evidence = exists(
                    select(Order.id)
                    .join(TaskArtifact, TaskArtifact.task_id == Order.task_id)
                    .where(TaskArtifact.artifact_id == artifact.id)
                ) | exists(
                    select(FieldEvidence.id).where(
                        FieldEvidence.artifact_id == artifact.id
                    )
                )
                protected = await session.scalar(select(active_link | order_evidence))
                if protected:
                    continue
                artifact.state = "DELETED"
            removable.append(artifact)
    removed = 0
    for artifact in removable:
        path = storage_path(settings.attachments_root, artifact.storage_key)
        if path.is_symlink():
            # Do not follow or remove a path that was replaced with a symlink.
            continue
        try:
            await anyio.to_thread.run_sync(partial(path.unlink, missing_ok=True))
            removed += 1
        except OSError:
            # DELETED rows are retried on the next run; metadata remains auditable.
            continue
    return removed


async def main() -> None:
    try:
        removed = await cleanup_expired_artifacts()
        print(f"已清理附件对象：{removed}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
