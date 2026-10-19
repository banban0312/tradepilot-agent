"""M5-01 批次父子任务、并发上限、停止边界与幂等验收。"""

from __future__ import annotations

import io
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.database import async_session_factory
from app.models import AgentTask, AuditEvent, ImportBatchItem, TaskJob
from app.tasks.service import MAX_ACTIVE_TASKS, try_claim_job

API_URL = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:5173")
HEADERS = [
    "订单分组",
    "订单类型",
    "合同编号",
    "项目",
    "地址",
    "联系人",
    "交期",
    "物料名称",
    "规格",
    "数量",
    "单位",
    "含税单价",
]


def _workbook(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单明细"
    sheet.append(HEADERS)
    for values in rows:
        sheet.append(values)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _row(group: str, name: str, quantity: object = 1) -> list[object]:
    return [
        group,
        "销售",
        "TP-DEMO-SALES-N1",
        "华北示例工程",
        "北京市朝阳区示例路 1 号",
        "张先生",
        "2026-11-01",
        name,
        "Q235 8mm",
        quantity,
        "吨",
        100,
    ]


@asynccontextmanager
async def _client(login: str = "demo-operator-north"):
    assert os.getenv("TRADEPILOT_TEST_MODE") == "1", "请通过隔离 Compose 验收脚本运行"
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    client = httpx.AsyncClient(base_url=API_URL, timeout=45)
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": login, "password": password},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Origin": ORIGIN, "X-CSRF-Token": response.json()["data"]["csrf_token"]}
    )
    try:
        yield client
    finally:
        await client.aclose()


async def _upload(client: httpx.AsyncClient, content: bytes) -> UUID:
    response = await client.post(
        "/api/v1/artifacts",
        data={"parse_intent": "order_xlsx"},
        files={"file": ("批次验收.xlsx", content, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["artifact_id"])


async def _create_batch(
    client: httpx.AsyncClient, artifact_id: UUID, key: str
) -> httpx.Response:
    return await client.post(
        "/api/v1/imports",
        headers={"Idempotency-Key": key},
        json={"artifact_id": str(artifact_id)},
    )


@pytest.mark.asyncio
async def test_import_batch_persistence_idempotency_concurrency_and_stop() -> None:
    assert MAX_ACTIVE_TASKS == 5, "批次子任务必须沿用受控的 5 路 Worker 并发上限"
    async with _client() as client:
        mixed_rows = [
            _row("正常组", "钢板 A", 2),
            _row("正常组", "钢板 B", 3),
            _row("超额组", "大量钢板", 2000),
            _row("缺失组", "缺规格钢板"),
        ]
        mixed_rows[-1][8] = None
        artifact_id = await _upload(client, _workbook(mixed_rows))
        created = await _create_batch(client, artifact_id, "issue029-mixed-001")
        assert created.status_code == 202, created.text
        data = created.json()["data"]
        batch_id = UUID(data["batch_id"])
        groups = {group["group_id"]: group for group in data["groups"]}
        assert data["summary"]["group_count"] == 3
        assert data["summary"]["task_count"] == 1
        assert groups["正常组"]["task_id"]
        assert groups["正常组"]["source_rows"] == [2, 3]
        assert groups["超额组"]["task_id"] is None
        assert groups["超额组"]["preview_status"] == "OVER_QUOTA_REVIEW"
        assert groups["缺失组"]["task_id"] is None
        assert groups["缺失组"]["preview_status"] == "NEEDS_CORRECTION"

        replay = await _create_batch(client, artifact_id, "issue029-mixed-001")
        assert replay.status_code == 202, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["data"]["batch_id"] == str(batch_id)
        assert replay.json()["data"]["summary"]["task_count"] == 1
        async with async_session_factory() as session:
            created_audits = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "import.batch_created")
            )
            assert created_audits == 1

        changed_artifact = await _upload(
            client, _workbook([_row("不同文件", "钢板 C")])
        )
        conflict = await _create_batch(client, changed_artifact, "issue029-mixed-001")
        assert conflict.status_code == 409, conflict.text

        snapshot = await client.get(f"/api/v1/imports/{batch_id}")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["data"]["groups"] == data["groups"]
        async with _client("demo-operator-south") as other_department:
            hidden = await other_department.get(f"/api/v1/imports/{batch_id}")
            assert hidden.status_code == 404, hidden.text
        async with async_session_factory() as session:
            item = await session.scalar(
                select(ImportBatchItem).where(
                    ImportBatchItem.batch_id == batch_id,
                    ImportBatchItem.group_id == "正常组",
                )
            )
            assert item is not None and item.task_id == UUID(
                groups["正常组"]["task_id"]
            )
            task = await session.get(AgentTask, item.task_id)
            assert task is not None and task.state == "QUEUED"
            job = await session.scalar(
                select(TaskJob).where(
                    TaskJob.task_id == item.task_id, TaskJob.state == "READY"
                )
            )
            assert job is not None
        stopped_mixed = await client.post(f"/api/v1/imports/{batch_id}/stop")
        assert stopped_mixed.status_code == 200, stopped_mixed.text
        assert stopped_mixed.json()["data"]["summary"]["stopped_count"] == 1
        async with async_session_factory() as session:
            stop_audits = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "import.batch_stop_requested")
            )
            assert stop_audits == 1

        six_groups = [_row(f"并发组-{index}", f"钢板-{index}") for index in range(1, 7)]
        concurrent_artifact = await _upload(client, _workbook(six_groups))
        concurrent_response = await _create_batch(
            client, concurrent_artifact, "issue029-concurrency-001"
        )
        assert concurrent_response.status_code == 202, concurrent_response.text
        concurrent_data = concurrent_response.json()["data"]
        concurrent_id = UUID(concurrent_data["batch_id"])
        assert concurrent_data["summary"]["task_count"] == 6
        task_ids = {UUID(group["task_id"]) for group in concurrent_data["groups"]}

        # 优先领取本批次作业，避免前置回归留下的 READY 作业影响并发断言。
        async with async_session_factory() as session, session.begin():
            jobs = list(
                (
                    await session.scalars(
                        select(TaskJob).where(TaskJob.task_id.in_(task_ids))
                    )
                ).all()
            )
            assert len(jobs) == 6
            for job in jobs:
                job.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed: list[UUID] = []
        for _ in range(MAX_ACTIVE_TASKS + 1):
            async with async_session_factory() as session, session.begin():
                job = await try_claim_job(
                    session,
                    worker_id="issue029-test-worker",
                    supported_kinds=frozenset({"RUN_TASK"}),
                )
                if job is not None:
                    assert job.task_id in task_ids
                    claimed.append(job.task_id)
        assert len(claimed) == MAX_ACTIVE_TASKS
        async with async_session_factory() as session:
            ready = list(
                (
                    await session.scalars(
                        select(TaskJob).where(
                            TaskJob.task_id.in_(task_ids), TaskJob.state == "READY"
                        )
                    )
                ).all()
            )
            assert len(ready) == 1

        stopped = await client.post(f"/api/v1/imports/{concurrent_id}/stop")
        assert stopped.status_code == 200, stopped.text
        stopped_data = stopped.json()["data"]
        assert stopped_data["stop_requested"] is True
        assert stopped_data["state"] == "STOPPING"
        assert stopped_data["summary"]["running_count"] == MAX_ACTIVE_TASKS
        assert stopped_data["summary"]["stopped_count"] == 1
        assert (
            sum(group["state"] == "STARTING" for group in stopped_data["groups"])
            == MAX_ACTIVE_TASKS
        )
        assert sum(group["state"] == "STOPPED" for group in stopped_data["groups"]) == 1

        async with async_session_factory() as session:
            ready_after_stop = await session.scalar(
                select(TaskJob.id).where(
                    TaskJob.task_id.in_(task_ids), TaskJob.state == "READY"
                )
            )
            assert ready_after_stop is None
            running_count = await session.scalar(
                select(TaskJob.id).where(
                    TaskJob.task_id.in_(task_ids), TaskJob.state == "RUNNING"
                )
            )
            assert running_count is not None

        repeated_stop = await client.post(f"/api/v1/imports/{concurrent_id}/stop")
        assert repeated_stop.status_code == 200, repeated_stop.text
        assert repeated_stop.json()["data"]["summary"]["stopped_count"] == 1
        after_restart = await client.get(f"/api/v1/imports/{concurrent_id}")
        assert after_restart.status_code == 200, after_restart.text
        assert after_restart.json()["data"]["stop_requested"] is True
        assert after_restart.json()["data"]["summary"]["stopped_count"] == 1
