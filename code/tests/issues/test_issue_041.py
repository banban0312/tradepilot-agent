"""M6-06 性能容量基线的可重复测量。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from app.config import settings
from app.storage.files import (
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    MAX_XLSX_BYTES,
    MAX_XLSX_ROWS,
    _validate_xlsx,
)
from app.tasks.service import MAX_ACTIVE_TASKS
from openpyxl import Workbook


def _xlsx_with_rows(count: int) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["订单分组", "物料", "数量"])
    for index in range(count):
        sheet.append([f"分组-{index:03d}", f"物料-{index:03d}", index + 1])
    result = io.BytesIO()
    workbook.save(result)
    workbook.close()
    return result.getvalue()


def test_issue_041_capacity_limits_match_prd() -> None:
    assert MAX_ACTIVE_TASKS == settings.max_concurrency == 5
    assert MAX_IMAGE_BYTES == 10 * 1024 * 1024
    assert MAX_PDF_BYTES == 20 * 1024 * 1024
    assert MAX_XLSX_BYTES == 10 * 1024 * 1024
    assert MAX_PDF_PAGES == 20
    assert MAX_XLSX_ROWS == 500


def test_issue_041_100_row_xlsx_parse_under_ten_seconds(tmp_path: Path) -> None:
    """计时真实 XLSX ZIP/工作表校验，不把文件生成时间混入解析耗时。"""
    path = tmp_path / "issue041-100-rows.xlsx"
    path.write_bytes(_xlsx_with_rows(100))
    started = time.perf_counter()
    result = _validate_xlsx(path)
    elapsed = time.perf_counter() - started
    assert result["row_count"] == 100
    assert elapsed <= 10, f"100 行 XLSX 校验耗时 {elapsed:.3f}s，超过 10s"


@pytest.mark.asyncio
async def test_issue_041_measure_100_business_api_samples() -> None:
    """记录真实 API 请求耗时；报告原始样本，阈值结论由交付证据据实判断。"""
    assert os.getenv("TRADEPILOT_TEST_MODE") == "1", "必须在隔离 Compose 环境运行"
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    origin = os.getenv("APP_ORIGIN", "http://localhost:5173")
    base_url = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
    timings_ms: list[float] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": origin},
            json={"login": "demo-admin", "password": password},
        )
        assert login.status_code == 200, login.text
        csrf_token = login.json()["data"]["csrf_token"]
        headers = {"Origin": origin, "X-CSRF-Token": csrf_token}
        for _ in range(100):
            started = time.perf_counter()
            response = await client.get(
                "/api/v1/admin/contracts?limit=100", headers=headers
            )
            timings_ms.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200, response.text

    ordered = sorted(timings_ms)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    evidence_root = Path(os.getenv("TRADEPILOT_ISSUE041_EVIDENCE", "/tmp"))
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "api-latency.json").write_text(
        json.dumps(
            {
                "endpoint": "GET /api/v1/admin/contracts?limit=100",
                "measurement_scope": "HTTP API response time; page feedback is measured by the browser smoke",
                "sample_count": len(timings_ms),
                "p50_ms": round(ordered[49], 3),
                "p95_ms": round(p95, 3),
                "p99_ms": round(ordered[98], 3),
                "max_ms": round(ordered[-1], 3),
                "samples_ms": [round(value, 3) for value in timings_ms],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert len(timings_ms) == 100
    assert p95 <= 1000, f"本地业务 API P95 {p95:.3f}ms 超过 1000ms"


@pytest.mark.asyncio
async def test_issue_041_100_row_xlsx_upload_under_ten_seconds() -> None:
    """通过真实上传 API 测量 100 行表格校验、存储和元数据提交。"""
    from test_issue_029 import _client, _row, _workbook

    rows = [
        _row(f"M6-06 性能组-{index:03d}", f"钢板-{index:03d}") for index in range(100)
    ]
    content = _workbook(rows)
    started = time.perf_counter()
    async with _client() as client:
        response = await client.post(
            "/api/v1/artifacts",
            data={"parse_intent": "order_xlsx"},
            files={
                "file": (
                    "issue041-100-rows.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    elapsed = time.perf_counter() - started
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["metadata"]["row_count"] == 100
    assert elapsed <= 10, f"100 行 XLSX API 上传耗时 {elapsed:.3f}s，超过 10s"
    evidence_root = Path(os.getenv("TRADEPILOT_ISSUE041_EVIDENCE", "/tmp"))
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "xlsx-upload.json").write_text(
        json.dumps(
            {
                "sample_rows": 100,
                "response_status": response.status_code,
                "elapsed_seconds": round(elapsed, 4),
                "artifact_id": data["artifact_id"],
                "metadata": data["metadata"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_issue_041_recovery_p95_with_20_stale_tasks() -> None:
    """20 个过期运行任务一批，重复 20 批测量 PostgreSQL 恢复耗时。"""
    from app.database import async_session_factory, engine
    from app.tasks.service import create_task, enqueue_command, recover_abandoned_jobs
    from sqlalchemy import text

    owner_id = UUID("00000000-0000-4000-8000-000000000100")
    department_id = UUID("00000000-0000-4000-8000-000000000001")
    timings_ms: list[float] = []
    for batch in range(20):
        job_ids: list[UUID] = []
        task_ids: list[UUID] = []
        async with async_session_factory() as session, session.begin():
            for index in range(20):
                task = await create_task(
                    session,
                    owner_id=owner_id,
                    department_id=department_id,
                    task_type="SALES",
                    task_id=uuid4(),
                )
                command = await enqueue_command(
                    session,
                    task_id=task.id,
                    actor_id=owner_id,
                    kind="START",
                    payload={"text": f"隔离恢复负载-{batch}-{index}"},
                    request_id=f"issue041-recovery-{batch}-{index}-{uuid4()}",
                )
                await session.execute(
                    text(
                        "UPDATE agent_tasks SET state='RUNNING', execution_epoch=1 WHERE id=:id"
                    ),
                    {"id": task.id},
                )
                await session.execute(
                    text(
                        "UPDATE jobs SET state='RUNNING', worker_id=:worker, heartbeat_at=now()-interval '1 hour', execution_epoch=1 WHERE id=:id"
                    ),
                    {"worker": f"issue041-recovery-{batch}", "id": command.job_id},
                )
                job_ids.append(command.job_id)
                task_ids.append(task.id)

        started = time.perf_counter()
        async with engine.connect() as connection, connection.begin():
            recovered = await recover_abandoned_jobs(
                connection, stale_before=datetime.now(UTC) - timedelta(minutes=2)
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert recovered == 20, f"第 {batch + 1} 批仅恢复 {recovered}/20 个过期任务"
        timings_ms.append(elapsed_ms)
        async with async_session_factory() as session, session.begin():
            for task_id, job_id in zip(task_ids, job_ids, strict=True):
                await session.execute(
                    text("UPDATE agent_tasks SET state='FAILED' WHERE id=:id"),
                    {"id": task_id},
                )
                await session.execute(
                    text("UPDATE jobs SET state='DONE' WHERE id=:id"),
                    {"id": job_id},
                )

    ordered = sorted(timings_ms)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    evidence_root = Path(os.getenv("TRADEPILOT_ISSUE041_EVIDENCE", "/tmp"))
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "recovery-latency.json").write_text(
        json.dumps(
            {
                "stale_tasks_per_batch": 20,
                "batch_count": len(timings_ms),
                "recovered_tasks": len(timings_ms) * 20,
                "p50_ms": round(ordered[9], 3),
                "p95_ms": round(p95, 3),
                "max_ms": round(ordered[-1], 3),
                "samples_ms": [round(value, 3) for value in timings_ms],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert p95 <= 2000, f"20 个过期任务恢复 P95 {p95:.3f}ms 超过 2000ms"


@pytest.mark.asyncio
async def test_issue_041_seed_waiting_checkpoint_for_backup_restore() -> None:
    """在当前配置 schema 生成真实 PostgreSQL WAITING_INPUT 检查点。"""
    from app.agent.workflow import (
        GRAPH_VERSION,
        build_task_graph,
        task_graph_config,
        wait_details,
    )
    from app.auth.context import BUSINESS_OPERATOR, AuthContext
    from app.database import async_session_factory, engine
    from app.drafts.service import save_candidate
    from app.drafts.types import CandidateOrder, FieldEvidenceInput, FieldSource
    from app.models import AgentTask, Draft, FieldEvidence, TaskArtifact, TaskWait
    from app.tasks.checkpointer import EpochFencedPostgresSaver
    from app.tasks.service import TaskExecutionLease, create_task, enqueue_command
    from app.tasks.waits import project_wait
    from sqlalchemy import select, text
    from test_issue_013 import FixedWorkflowServices

    owner_id = UUID("00000000-0000-4000-8000-000000000100")
    department_id = UUID("00000000-0000-4000-8000-000000000001")
    async with async_session_factory() as session, session.begin():
        task = await create_task(
            session,
            owner_id=owner_id,
            department_id=department_id,
            task_type="SALES",
            task_id=uuid4(),
        )
        task_id = task.id
        start = await enqueue_command(
            session,
            task_id=task_id,
            actor_id=owner_id,
            kind="START",
            payload={"text": "螺纹钢 200 吨"},
            request_id=f"issue041-{uuid4()}",
        )
        await session.execute(
            text(
                "UPDATE jobs SET state='DONE', worker_id=NULL, heartbeat_at=NULL, execution_epoch=NULL WHERE state IN ('READY','RUNNING') AND id != :job"
            ),
            {"job": start.job_id},
        )
        await session.execute(
            text(
                "UPDATE agent_tasks SET state='FAILED' WHERE state IN ('QUEUED','RUNNING') AND id IN (SELECT task_id FROM jobs WHERE state='DONE' AND id != :job)"
            ),
            {"job": start.job_id},
        )
        worker_id = f"issue041-{uuid4()}"
        await session.execute(
            text(
                "UPDATE jobs SET state='RUNNING', worker_id=:worker, heartbeat_at=now(), attempts=1, execution_epoch=0 WHERE id=:job"
            ),
            {"worker": worker_id, "job": start.job_id},
        )

    assert worker_id
    connection = await engine.connect()
    lease = await TaskExecutionLease.acquire(
        connection,
        task_id=task_id,
        job_id=start.job_id,
        worker_id=worker_id,
    )
    try:
        saver = EpochFencedPostgresSaver(lease)
        await saver.setup()
        graph = build_task_graph(FixedWorkflowServices(), checkpointer=saver)
        await graph.ainvoke(
            {
                "task_id": str(task_id),
                "graph_version": GRAPH_VERSION,
                "command_id": str(start.command_id),
                "command_kind": "START",
                "input_text": "螺纹钢 200 吨",
                "input_at": "2026-10-20T10:00:00+08:00",
            },
            config=task_graph_config(task_id),
        )
        details = wait_details(await graph.aget_state(task_graph_config(task_id)))
        assert details is not None and details["kind"] == "input"
        await project_wait(lease, details)

        # 生成可追溯到真实上传附件的来源证据，供协调备份恢复检查。
        from test_issue_029 import _client

        async with _client() as client:
            upload = await client.post(
                "/api/v1/artifacts",
                data={"parse_intent": "order_xlsx"},
                files={
                    "file": (
                        "issue041-source-evidence.xlsx",
                        _xlsx_with_rows(1),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )
        assert upload.status_code == 201, upload.text
        artifact_id = UUID(upload.json()["data"]["artifact_id"])
        draft_id = uuid4()
        async with async_session_factory() as session, session.begin():
            session.add(TaskArtifact(task_id=task_id, artifact_id=artifact_id))
            session.add(
                Draft(
                    id=draft_id,
                    task_id=task_id,
                    current_version=0,
                    lock_state="EDITABLE",
                )
            )
            saved = await save_candidate(
                session,
                draft_id=draft_id,
                expected_version=0,
                candidate=CandidateOrder(
                    order_type="SALES",
                    address="来源证据恢复测试地址",
                    evidence=(
                        FieldEvidenceInput(
                            field_path="address",
                            source_type=FieldSource.FILE_CELL,
                            artifact_id=artifact_id,
                            locator={"sheet": "Sheet", "cell": "A1"},
                            raw_value="原始地址",
                            accepted_value="来源证据恢复测试地址",
                        ),
                    ),
                ),
                actor=AuthContext(
                    user_id=owner_id,
                    login="demo-operator-north",
                    roles=frozenset({BUSINESS_OPERATOR}),
                    department_ids=frozenset({department_id}),
                ),
            )
            assert saved.version == 1
            evidence = await session.scalar(
                select(FieldEvidence).where(FieldEvidence.draft_id == draft_id)
            )
            assert evidence is not None and evidence.artifact_id == artifact_id
            assert evidence.source_type == "FILE_CELL"
    finally:
        await lease.release()
        await connection.close()

    evidence_root = Path(os.getenv("TRADEPILOT_ISSUE041_EVIDENCE", "/tmp"))
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "checkpoint-fixture.json").write_text(
        json.dumps(
            {
                "task_id": str(task_id),
                "job_id": str(start.job_id),
                "worker_id": worker_id,
                "checkpoint_id": details["checkpoint_id"],
                "interrupt_id": details["interrupt_id"],
                "graph_version": GRAPH_VERSION,
                "checkpoint_schema": settings.checkpoint_schema,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    async with async_session_factory() as session:
        task = await session.get(AgentTask, task_id)
        wait = await session.scalar(
            __import__("sqlalchemy")
            .select(TaskWait)
            .where(TaskWait.task_id == task_id, TaskWait.active.is_(True))
        )
        assert task is not None and task.state == "WAITING_INPUT"
        assert wait is not None and wait.checkpoint_ref == details["checkpoint_id"]


@pytest.mark.asyncio
async def test_issue_041_resume_restored_checkpoint() -> None:
    """在 pg_restore 产生的克隆库继续同一等待任务，证明 checkpoint 可用。"""
    from app.agent.workflow import GRAPH_VERSION, build_task_graph, resume_task_graph
    from app.database import async_session_factory, engine
    from app.models import AgentTask, FieldEvidence, InputArtifact, TaskJob, TaskWait
    from app.tasks.checkpointer import EpochFencedPostgresSaver
    from app.tasks.service import TaskExecutionLease
    from sqlalchemy import select, text
    from test_issue_013 import FixedWorkflowServices

    evidence_root = Path(os.getenv("TRADEPILOT_ISSUE041_EVIDENCE", "/tmp"))
    fixture = json.loads(
        (evidence_root / "checkpoint-fixture.json").read_text(encoding="utf-8")
    )
    task_id = UUID(fixture["task_id"])
    job_id = UUID(fixture["job_id"])
    async with async_session_factory() as session:
        task = await session.get(AgentTask, task_id)
        job = await session.get(TaskJob, job_id)
        wait = await session.scalar(
            select(TaskWait).where(
                TaskWait.task_id == task_id, TaskWait.active.is_(True)
            )
        )
        assert task is not None and task.state == "WAITING_INPUT"
        assert (
            job is not None
            and job.state == "RUNNING"
            and job.worker_id == fixture["worker_id"]
        )
        assert wait is not None
        assert wait.checkpoint_ref == fixture["checkpoint_id"]
        assert wait.interrupt_id == fixture["interrupt_id"]
        checkpoint_count = await session.scalar(
            text(
                f"SELECT count(*) FROM {settings.checkpoint_schema}.checkpoints WHERE thread_id=:task_id"
            ),
            {"task_id": str(task_id)},
        )
        assert checkpoint_count and checkpoint_count >= 2

    connection = await engine.connect()
    lease = await TaskExecutionLease.acquire(
        connection,
        task_id=task_id,
        job_id=job_id,
        worker_id=fixture["worker_id"],
    )
    try:
        saver = EpochFencedPostgresSaver(lease)
        await saver.setup()
        graph = build_task_graph(FixedWorkflowServices(), checkpointer=saver)
        result = await resume_task_graph(
            graph,
            task_id=task_id,
            command_id=uuid4(),
            checkpoint_id=fixture["checkpoint_id"],
            interrupt_id=fixture["interrupt_id"],
            graph_version=GRAPH_VERSION,
        )
        assert result["task_id"] == fixture["task_id"]
        assert result["projection_state"] == "WAITING_APPROVAL"
    finally:
        await lease.release()
        await connection.close()

    restored_root = Path(os.environ["TRADEPILOT_ISSUE041_RESTORED_ATTACHMENTS"])
    manifest_path = evidence_root / "attachments-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    async with async_session_factory() as session:
        artifacts = list(
            (
                await session.scalars(
                    select(InputArtifact).where(InputArtifact.state != "DELETED")
                )
            ).all()
        )
        source_evidence = list(
            (
                await session.scalars(
                    select(FieldEvidence).where(
                        FieldEvidence.source_type.in_(("FILE_PAGE", "FILE_CELL"))
                    )
                )
            ).all()
        )
    assert artifacts, "验收快照必须含有关联数据库记录的附件"
    artifact_ids = {artifact.id for artifact in artifacts}
    assert source_evidence, "恢复库必须包含文件页/单元格字段来源证据"
    assert all(evidence.artifact_id in artifact_ids for evidence in source_evidence), (
        "恢复后的字段证据必须关联快照中的附件记录"
    )
    for artifact in artifacts:
        relative = f"objects/{artifact.storage_key}"
        path = restored_root / relative
        assert relative in manifest and path.is_file(), f"附件恢复缺失：{relative}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256
