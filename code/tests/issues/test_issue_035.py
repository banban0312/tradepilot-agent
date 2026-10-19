"""M5-07 混合 Excel 批次与数据库不变量验收。"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_issue_029 import _client, _create_batch, _row, _upload, _workbook

from app.database import async_session_factory
from app.models import (
    AgentTask,
    ImportBatch,
    ImportBatchItem,
    Order,
    TaskApproval,
    TaskJob,
)


@pytest.mark.asyncio
async def test_mixed_batch_keeps_invalid_groups_out_of_tasks_and_orders() -> None:
    """Excel 超额/缺失组保留来源问题，但不能入队、审批或创建订单。"""
    async with _client() as client:
        valid = _row("有效组", "钢板 A", 1)
        over_quota = _row("超额组", "大量钢板", 2000)
        missing = _row("缺字段组", "钢板 C", 1)
        missing[8] = None
        artifact_id = await _upload(client, _workbook([valid, over_quota, missing]))

        first = await _create_batch(client, artifact_id, "issue035-mixed-invariants")
        assert first.status_code == 202, first.text
        data = first.json()["data"]
        batch_id = UUID(data["batch_id"])
        groups = {item["group_id"]: item for item in data["groups"]}
        assert data["summary"]["group_count"] == 3
        assert data["summary"]["task_count"] == 1
        assert groups["有效组"]["task_id"]
        assert groups["有效组"]["source_rows"] == [2]
        assert groups["超额组"]["preview_status"] == "OVER_QUOTA_REVIEW"
        assert groups["超额组"]["task_id"] is None
        assert groups["缺字段组"]["preview_status"] == "NEEDS_CORRECTION"
        assert groups["缺字段组"]["task_id"] is None

        replay = await _create_batch(client, artifact_id, "issue035-mixed-invariants")
        assert replay.status_code == 202, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["data"]["batch_id"] == str(batch_id)

        task_id = UUID(groups["有效组"]["task_id"])
        async with async_session_factory() as session:
            batch = await session.get(ImportBatch, batch_id)
            item_rows = list(
                (
                    await session.scalars(
                        select(ImportBatchItem).where(
                            ImportBatchItem.batch_id == batch_id
                        )
                    )
                ).all()
            )
            assert batch is not None and batch.artifact_id == artifact_id
            assert len(item_rows) == 3
            valid_item = next(item for item in item_rows if item.group_id == "有效组")
            assert valid_item.task_id == task_id
            assert valid_item.source_rows == [2]
            child = await session.get(AgentTask, task_id)
            assert child is not None and child.state == "QUEUED"
            job = await session.scalar(
                select(TaskJob).where(TaskJob.task_id == task_id)
            )
            assert job is not None and job.state == "READY"
            invalid_task_ids = [
                item.task_id for item in item_rows if item.group_id != "有效组"
            ]
            assert all(task_id is None for task_id in invalid_task_ids)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskApproval)
                    .where(TaskApproval.task_id == task_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.task_id == task_id)
                )
                == 0
            )

        stopped = await client.post(f"/api/v1/imports/{batch_id}/stop")
        assert stopped.status_code == 200, stopped.text
        async with async_session_factory() as session:
            child = await session.get(AgentTask, task_id)
            assert child is not None and child.state == "FAILED"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.task_id == task_id)
                )
                == 0
            )
