"""M5-03 批次结果核实、失败定位与安全恢复入口。"""

from __future__ import annotations

import pytest
from test_issue_017 import _new_failed_task, _operator_client


@pytest.mark.asyncio
async def test_failed_span_lookup_is_task_scoped_and_redacts_payloads() -> None:
    task_id, _, _, span_id = await _new_failed_task(retryable=True)
    other_task_id, _, _, other_span_id = await _new_failed_task(retryable=True)
    client = await _operator_client()
    try:
        response = await client.get(f"/api/v1/tasks/{task_id}/trace/{span_id}")
        assert response.status_code == 200, response.text
        span = response.json()["data"]
        assert span["task_id"] == str(task_id)
        assert span["span_id"] == str(span_id)
        assert span["name"] == "model.function_calling"
        assert span["status"] == "ERROR"
        assert span["error"] == {"code": "MODEL_UNAVAILABLE", "retryable": True}
        assert "input_ref" not in span and "output_ref" not in span

        cross_task = await client.get(f"/api/v1/tasks/{task_id}/trace/{other_span_id}")
        assert cross_task.status_code == 404, cross_task.text

        stale_task = await client.get(f"/api/v1/tasks/{other_task_id}/trace/{span_id}")
        assert stale_task.status_code == 404, stale_task.text
    finally:
        await client.aclose()
