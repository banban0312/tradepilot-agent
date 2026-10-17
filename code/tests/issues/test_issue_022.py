"""M4-01 附件上传、类型限制、内容鉴权与哈希验收。"""

from __future__ import annotations

import hashlib
import io
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import AgentTask, InputArtifact
from app.storage.cleanup import cleanup_expired_artifacts
from app.storage.files import storage_path

API_URL = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:5173")


def _pdf(*, pages: int = 1, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("sample-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color=(32, 64, 96)).save(output, format="PNG")
    return output.getvalue()


def _xlsx(*, detail_rows: int = 1) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["物料", "数量"])
    for number in range(detail_rows):
        worksheet.append([f"样例物料-{number}", number + 1])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


async def _login(client: httpx.AsyncClient, login: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": login, "password": os.environ["DEMO_PASSWORD"]},
    )
    assert response.status_code == 200, response.text
    client.headers["Origin"] = ORIGIN
    return str(response.json()["data"]["csrf_token"])


async def _upload(
    client: httpx.AsyncClient,
    csrf: str,
    filename: str,
    content: bytes,
    intent: str = "contract_pdf",
    task_id: UUID | None = None,
) -> httpx.Response:
    form = {"parse_intent": intent}
    if task_id is not None:
        form["task_id"] = str(task_id)
    return await client.post(
        "/api/v1/artifacts",
        headers={"X-CSRF-Token": csrf},
        data=form,
        files={"file": (filename, content, "application/octet-stream")},
    )


@pytest.mark.asyncio
async def test_artifact_upload_hash_storage_and_department_authorization() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_022.ps1 在隔离 PostgreSQL Compose 环境执行")
    assert os.getenv("DEMO_MODE", "").casefold() == "true"
    content = _pdf()

    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as owner:
        csrf = await _login(owner, "demo-operator-north")
        uploaded = await _upload(owner, csrf, "合同样例.pdf", content)
        assert uploaded.status_code == 201, uploaded.text
        envelope = uploaded.json()
        artifact = envelope["data"]
        assert envelope["request_id"]
        assert artifact["state"] == "STORED"
        assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
        assert artifact["metadata"]["page_count"] == 1
        assert not {"storage_key", "path", "storage_path"}.intersection(artifact)

        artifact_id = UUID(artifact["artifact_id"])
        async with async_session_factory() as session:
            record = await session.get(InputArtifact, artifact_id)
            assert record is not None and record.state == "STORED"
            assert record.sha256 == artifact["sha256"]
            assert len(record.storage_key) == 32
            assert "/" not in record.storage_key and "\\" not in record.storage_key

        body = await owner.get(f"/api/v1/artifacts/{artifact_id}/content")
        assert body.status_code == 200
        assert body.content == content
        assert body.headers["cache-control"] == "private, no-store"
        assert body.headers["x-content-type-options"] == "nosniff"

        metadata = await owner.get(f"/api/v1/artifacts/{artifact_id}")
        assert metadata.status_code == 200
        assert metadata.json()["data"]["state"] == "STORED"

        task_response = await owner.post(
            "/api/v1/tasks",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"issue22-{artifact_id}",
            },
            json={
                "order_type": "SALES",
                "input": {"text": "附件访问权限验收文本", "artifact_ids": []},
            },
        )
        assert task_response.status_code == 202, task_response.text
        task_id = UUID(task_response.json()["data"]["task_id"])
        linked_upload = await _upload(
            owner,
            csrf,
            "任务关联合同.pdf",
            content,
            task_id=task_id,
        )
        assert linked_upload.status_code == 201, linked_upload.text
        linked_artifact_id = linked_upload.json()["data"]["artifact_id"]

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as other_department:
        await _login(other_department, "demo-approver-north")
        same_department_unlinked = await other_department.get(
            f"/api/v1/artifacts/{artifact_id}/content"
        )
        assert same_department_unlinked.status_code == 404
        linked_content = await other_department.get(
            f"/api/v1/artifacts/{linked_artifact_id}/content"
        )
        assert linked_content.status_code == 200
        assert linked_content.content == content
        linked_list = await other_department.get(f"/api/v1/tasks/{task_id}/artifacts")
        assert linked_list.status_code == 200
        assert (
            linked_list.json()["data"]["items"][0]["artifact_id"] == linked_artifact_id
        )

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as other_department:
        await _login(other_department, "demo-operator-south")
        unauthorized = await other_department.get(
            f"/api/v1/artifacts/{artifact_id}/content"
        )
        assert unauthorized.status_code == 404, unauthorized.text

    linked_uuid = UUID(linked_artifact_id)
    old_created_at = datetime.now(UTC) - timedelta(days=31)
    async with async_session_factory() as session, session.begin():
        record = await session.get(InputArtifact, linked_uuid)
        assert record is not None
        record.created_at = old_created_at
        storage_key = record.storage_key
    object_path = storage_path(settings.attachments_root, storage_key)
    assert await cleanup_expired_artifacts() == 0
    assert object_path.is_file()

    async with async_session_factory() as session, session.begin():
        task = await session.get(AgentTask, task_id)
        assert task is not None
        task.state = "COMPLETED"
    assert await cleanup_expired_artifacts() == 1
    assert not object_path.exists()
    async with async_session_factory() as session:
        deleted = await session.get(InputArtifact, linked_uuid)
        assert deleted is not None and deleted.state == "DELETED"


@pytest.mark.asyncio
async def test_rejects_unsupported_encrypted_and_oversized_pdf_without_storing() -> (
    None
):
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_022.ps1 在隔离 PostgreSQL Compose 环境执行")
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        csrf = await _login(client, "demo-operator-north")

        unsupported = await _upload(client, csrf, "misleading.pdf", b"not a PDF")
        assert unsupported.status_code == 400, unsupported.text
        assert unsupported.json()["error"]["code"] == "FILE_UNSUPPORTED"

        encrypted = await _upload(client, csrf, "encrypted.pdf", _pdf(encrypted=True))
        assert encrypted.status_code == 400, encrypted.text
        assert encrypted.json()["error"]["code"] == "FILE_UNREADABLE"

        too_many_pages = await _upload(client, csrf, "many.pdf", _pdf(pages=21))
        assert too_many_pages.status_code == 413, too_many_pages.text
        assert too_many_pages.json()["error"]["code"] == "FILE_TOO_LARGE"

        # 校验失败不会写入元数据或留下可下载记录。
        async with async_session_factory() as session:
            failed_name = await session.scalar(
                select(InputArtifact.id).where(
                    InputArtifact.original_name.in_(
                        ["misleading.pdf", "encrypted.pdf", "many.pdf"]
                    )
                )
            )
            assert failed_name is None


@pytest.mark.asyncio
async def test_accepts_png_and_xlsx_but_enforces_detail_row_limit() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_022.ps1 在隔离 PostgreSQL Compose 环境执行")
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        csrf = await _login(client, "demo-operator-north")

        image = _png()
        image_response = await _upload(
            client, csrf, "签收图片.png", image, intent="receipt_image"
        )
        assert image_response.status_code == 201, image_response.text
        image_data = image_response.json()["data"]
        assert image_data["sha256"] == hashlib.sha256(image).hexdigest()
        assert image_data["media_type"] == "image/png"
        assert image_data["metadata"]["width"] == 3

        spreadsheet = _xlsx(detail_rows=2)
        sheet_response = await _upload(
            client, csrf, "订单.xlsx", spreadsheet, intent="order_xlsx"
        )
        assert sheet_response.status_code == 201, sheet_response.text
        sheet_data = sheet_response.json()["data"]
        assert sheet_data["sha256"] == hashlib.sha256(spreadsheet).hexdigest()
        assert sheet_data["metadata"]["row_count"] == 2

        response = await _upload(
            client,
            csrf,
            "超行数.xlsx",
            _xlsx(detail_rows=501),
            intent="order_xlsx",
        )
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
