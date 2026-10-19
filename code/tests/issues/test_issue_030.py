"""M5-02 批次分组修正、重新校验和乐观版本保护。"""

from __future__ import annotations

from uuid import UUID

import pytest
from test_issue_029 import _client, _create_batch, _row, _upload, _workbook


@pytest.mark.asyncio
async def test_corrected_import_group_is_revalidated_and_versioned() -> None:
    async with _client() as client:
        row = _row("补齐地址组", "钢板 A")
        row[4] = None
        artifact_id = await _upload(client, _workbook([row]))
        created = await _create_batch(client, artifact_id, "issue030-correction-001")
        assert created.status_code == 202, created.text
        initial = created.json()["data"]
        batch_id = UUID(initial["batch_id"])
        group = initial["groups"][0]
        assert group["preview_status"] == "NEEDS_CORRECTION"
        assert group["task_id"] is None
        assert group["candidate"]["address"] is None
        assert group["edit_version"] == 1

        candidate = {**group["candidate"], "address": "北京市朝阳区批次验收路 1 号"}
        corrected = await client.patch(
            f"/api/v1/imports/{batch_id}/groups",
            json={
                "sheet": group["sheet"],
                "group_id": group["group_id"],
                "expected_version": group["edit_version"],
                "candidate": candidate,
            },
        )
        assert corrected.status_code == 200, corrected.text
        repaired = corrected.json()["data"]["groups"][0]
        assert repaired["preview_status"] == "READY_FOR_REVIEW"
        assert repaired["task_id"] is not None
        assert repaired["source_rows"] == group["source_rows"]
        assert repaired["edit_version"] == 2

        stale = await client.patch(
            f"/api/v1/imports/{batch_id}/groups",
            json={
                "sheet": group["sheet"],
                "group_id": group["group_id"],
                "expected_version": 1,
                "candidate": candidate,
            },
        )
        assert stale.status_code == 409, stale.text
        unchanged = await client.get(f"/api/v1/imports/{batch_id}")
        assert unchanged.status_code == 200, unchanged.text
        persisted = unchanged.json()["data"]["groups"][0]
        assert persisted["task_id"] == repaired["task_id"]
        assert persisted["edit_version"] == 2
