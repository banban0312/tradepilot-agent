"""M5-05 历史任务筛选和真实轨迹查询验收。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from test_issue_017 import _new_failed_task, _operator_client
from test_issue_029 import _create_batch, _row, _upload, _workbook

from app.database import async_session_factory
from app.models import AgentTask, TaskEvent, TaskSession, TraceSpan


def _require_compose() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_033.ps1 在隔离 PostgreSQL Compose 环境执行")


@pytest.mark.asyncio
async def test_trace_pagination_metrics_redaction_and_history_filters() -> None:
    _require_compose()
    task_id, _, _, first_span_id = await _new_failed_task(retryable=True)
    south_task_id = uuid4()
    south_session_id = uuid4()
    south_user = UUID("00000000-0000-4000-8000-000000000101")
    south_department = UUID("00000000-0000-4000-8000-000000000002")
    wait_start = datetime.now(UTC) - timedelta(seconds=10)
    async with async_session_factory() as session, session.begin():
        session.add(
            TaskSession(id=south_session_id, owner_id=south_user, title="南区隔离任务")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=south_task_id,
                session_id=south_session_id,
                owner_id=south_user,
                department_id=south_department,
                task_type="SALES",
                state="FAILED",
            )
        )
        first = await session.get(TraceSpan, first_span_id)
        assert first is not None
        session.add(
            TraceSpan(
                id=uuid4(),
                trace_id=first.trace_id,
                task_id=task_id,
                parent_span_id=first.id,
                kind="TOOL",
                name="mcp.contract_lookup",
                execution_mode="LIVE",
                protocol="mcp.streamable_http",
                start_at=first.start_at + timedelta(milliseconds=1),
                end_at=first.start_at + timedelta(milliseconds=3),
                duration_ms=2,
                status="OK",
                input_ref={
                    "name": "mcp.contract_lookup",
                    "argument_fields": ["contract_id"],
                    "secret": "不要返回",
                },
                output_ref={
                    "result_fields": ["contract_id"],
                    "secret_value": "不得泄漏",
                },
                usage=None,
            )
        )
        for seq, state, emitted_at in (
            (1, "WAITING_INPUT", wait_start),
            (2, "WAITING_APPROVAL", wait_start + timedelta(seconds=4)),
            (3, "FAILED", wait_start + timedelta(seconds=7)),
        ):
            session.add(
                TaskEvent(
                    task_id=task_id,
                    seq=seq,
                    event_type="task.status_changed",
                    payload={"state": state},
                    emitted_at=emitted_at,
                )
            )
    client = await _operator_client()
    try:
        history = await client.get(
            "/api/v1/tasks", params={"state": "FAILED", "q": "螺纹钢", "limit": 20}
        )
        assert history.status_code == 200, history.text
        assert any(
            item["task_id"] == str(task_id) for item in history.json()["data"]["items"]
        )
        invalid_range = await client.get(
            "/api/v1/tasks",
            params={
                "created_after": datetime.now(UTC).isoformat(),
                "created_before": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            },
        )
        assert invalid_range.status_code == 422, invalid_range.text
        missing_timezone = await client.get(
            "/api/v1/tasks", params={"created_after": "2026-10-19T00:00:00"}
        )
        assert missing_timezone.status_code == 422, missing_timezone.text

        artifact_id = await _upload(client, _workbook([_row("历史树分组", "测试钢板")]))
        created_batch = await _create_batch(client, artifact_id, f"issue033-{uuid4()}")
        assert created_batch.status_code == 202, created_batch.text
        batch_id = created_batch.json()["data"]["batch_id"]
        history_tree = await client.get("/api/v1/tasks", params={"limit": 20})
        assert history_tree.status_code == 200, history_tree.text
        batch = next(
            item
            for item in history_tree.json()["data"]["batches"]
            if item["batch_id"] == batch_id
        )
        group = next(
            item for item in batch["groups"] if item["group_id"] == "历史树分组"
        )
        assert group["source_rows"] == [2]
        assert group["task_id"]
        assert group["task_state"] == "QUEUED"
        excel_history = await client.get(
            "/api/v1/tasks", params={"input_type": "EXCEL_GROUP", "limit": 20}
        )
        assert excel_history.status_code == 200, excel_history.text
        assert any(
            item["task_id"] == group["task_id"]
            for item in excel_history.json()["data"]["items"]
        )

        first_page = await client.get(
            f"/api/v1/tasks/{task_id}/trace", params={"limit": 1}
        )
        assert first_page.status_code == 200, first_page.text
        data = first_page.json()["data"]
        assert data["next_cursor"]
        assert data["metrics"]["usage_status"] == "unavailable"
        assert data["metrics"]["tokens"] is None
        assert data["metrics"]["usage_coverage"] == 0
        assert data["metrics"]["tool_duration_ms"] == 2
        waits = data["metrics"]["manual_waits"]
        assert [(wait["state"], wait["duration_ms"]) for wait in waits] == [
            ("WAITING_INPUT", 4000),
            ("WAITING_APPROVAL", 3000),
        ]

        second_page = await client.get(
            f"/api/v1/tasks/{task_id}/trace",
            params={"limit": 1, "cursor": data["next_cursor"]},
        )
        assert second_page.status_code == 200, second_page.text
        trace_spans = data["spans"] + second_page.json()["data"]["spans"]
        assert len(trace_spans) == 2
        tool = next(span for span in trace_spans if span["kind"] == "TOOL")
        assert tool["input"] == {
            "name": "mcp.contract_lookup",
            "argument_fields": ["contract_id"],
        }
        assert tool["output"] == {"result_fields": ["contract_id"]}
        assert tool["usage_status"] == "unavailable"
        assert all(span["usage"] is None for span in trace_spans)

        cross_department = await client.get(f"/api/v1/tasks/{south_task_id}/trace")
        assert cross_department.status_code == 404, cross_department.text
    finally:
        await client.aclose()
