"""M4-03 图片真实 OCR、候选证据与附件权限验收。"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select

from app.config import settings
from app.database import async_session_factory
from app.models import AgentTask, AuditEvent, InputArtifact, Order, TaskApproval
from app.parsing.image import parse_image
from app.parsing.pdf import PdfParseError
from app.storage.files import storage_path

API_URL = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:5173")


def _receipt_image(*, conflicting_quantity: bool = False) -> bytes:
    lines = [
        "CONTRACT NO: IMG-2026-024",
        "Customer: TradePilot Buyer Ltd",
        "Material: Portland cement",
        "Specification: P.O. 42.5",
        "Quantity: 500 T",
        *(["Quantity: 400 T"] if conflicting_quantity else []),
        "Total Amount: 220000 CNY",
        "Delivery Date: 2026-10-20",
        "Recipient: Li Ming",
        "Delivery Address: Kowloon Bay Warehouse",
    ]
    font = ImageFont.load_default(size=48)
    image = Image.new("RGB", (2400, 100 + len(lines) * 100), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((40, 30 + index * 100), line, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_real_tesseract_image_ocr_returns_version_hash_boxes_and_candidates(
    tmp_path: Path,
) -> None:
    content = _receipt_image()
    path = tmp_path / "receipt.png"
    path.write_bytes(content)

    parsed = parse_image(path, hashlib.sha256(content).hexdigest())

    assert parsed["state"] in {"COMPLETE", "PARTIAL"}, parsed
    assert parsed["media_type"] == "image/png"
    assert parsed["ocr_engine"] == "tesseract"
    assert parsed["ocr_version"]
    assert parsed["input_sha256"] == hashlib.sha256(content).hexdigest()
    assert parsed["elapsed_ms"] >= 0
    lines = parsed["pages"][0]["lines"]
    assert any("IMG-2026-024" in line["text"] for line in lines), lines
    assert all(line["locator"]["type"] == "bbox" for line in lines)
    fields = {field["field"]: field for field in parsed["fields"]}
    assert fields["quantity"]["value"] == "500"
    assert fields["quantity"]["unit"].casefold() in {"t", "ton", "tons"}
    assert fields["amount"]["value"] == "220000"
    assert fields["delivery_address"]["value"] == "Kowloon Bay Warehouse"
    assert all(field["confirmation_required"] for field in parsed["fields"])
    assert all(field["confidence"] is None for field in parsed["fields"])


def test_conflicting_image_candidates_require_confirmation(tmp_path: Path) -> None:
    content = _receipt_image(conflicting_quantity=True)
    path = tmp_path / "conflict.png"
    path.write_bytes(content)

    parsed = parse_image(path, hashlib.sha256(content).hexdigest())

    assert parsed["state"] == "PARTIAL"
    quantities = [field for field in parsed["fields"] if field["field"] == "quantity"]
    assert {field["value"] for field in quantities} == {"500", "400"}
    assert all(field["status"] == "CONFLICT" for field in quantities)


def test_ocr_timeout_is_reported_without_fabricating_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.parsing.image as parser

    content = _receipt_image()
    path = tmp_path / "timeout.png"
    path.write_bytes(content)

    def timeout(_image: bytes, *, timeout: float) -> list[object]:
        assert timeout > 0
        raise PdfParseError("OCR_TIMEOUT", "图片 OCR 超时")

    monkeypatch.setattr(parser, "_ocr_image", timeout)
    with pytest.raises(PdfParseError, match="图片 OCR 超时") as error:
        parse_image(path, hashlib.sha256(content).hexdigest())
    assert error.value.code == "OCR_TIMEOUT"


async def _login(client: httpx.AsyncClient, login: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": login, "password": os.environ["DEMO_PASSWORD"]},
    )
    assert response.status_code == 200, response.text
    client.headers["Origin"] = ORIGIN
    return str(response.json()["data"]["csrf_token"])


@pytest.mark.asyncio
async def test_image_parse_endpoint_authorizes_audits_and_has_no_business_side_effects() -> (
    None
):
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_024.ps1 在隔离 PostgreSQL Compose 环境执行")
    content = _receipt_image()
    async with httpx.AsyncClient(base_url=API_URL, timeout=90) as owner:
        csrf = await _login(owner, "demo-operator-north")
        uploaded = await owner.post(
            "/api/v1/artifacts",
            headers={"X-CSRF-Token": csrf},
            data={"parse_intent": "receipt_image"},
            files={"file": ("磅单.png", content, "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
        artifact_id = UUID(uploaded.json()["data"]["artifact_id"])
        csrf_missing = await owner.post(f"/api/v1/artifacts/{artifact_id}/parse")
        assert csrf_missing.status_code == 403
        async with async_session_factory() as session:
            before = (
                await session.scalar(select(func.count()).select_from(Order)),
                await session.scalar(select(func.count()).select_from(TaskApproval)),
                await session.scalar(select(func.count()).select_from(AgentTask)),
            )

        response = await owner.post(
            f"/api/v1/artifacts/{artifact_id}/parse",
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        result = response.json()["data"]
        assert result["input_sha256"] == uploaded.json()["data"]["sha256"]
        assert result["ocr_engine"] == "tesseract"
        assert result["elapsed_ms"] >= 0
        async with async_session_factory() as session:
            after = (
                await session.scalar(select(func.count()).select_from(Order)),
                await session.scalar(select(func.count()).select_from(TaskApproval)),
                await session.scalar(select(func.count()).select_from(AgentTask)),
            )
            assert after == before
            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "artifact.parsed",
                    AuditEvent.input_hash == result["input_sha256"],
                )
            )
            assert audit is not None
            assert audit.after_ref["elapsed_ms"] >= 0

    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as other_department:
        csrf = await _login(other_department, "demo-operator-south")
        denied = await other_department.post(
            f"/api/v1/artifacts/{artifact_id}/parse",
            headers={"X-CSRF-Token": csrf},
        )
        assert denied.status_code == 404


@pytest.mark.asyncio
async def test_parse_rejects_xlsx_and_detects_corrupted_stored_image() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_024.ps1 在隔离 PostgreSQL Compose 环境执行")
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        csrf = await _login(client, "demo-operator-north")
        content = _receipt_image()
        upload = await client.post(
            "/api/v1/artifacts",
            headers={"X-CSRF-Token": csrf},
            data={"parse_intent": "receipt_image"},
            files={"file": ("integrity.png", content, "image/png")},
        )
        assert upload.status_code == 201, upload.text
        artifact_id = UUID(upload.json()["data"]["artifact_id"])
        # 通过附件 API 对非法类型重解析应被拒绝；无相关权限/格式时不能进解析器。
        xlsx_like = await client.post(
            "/api/v1/artifacts",
            headers={"X-CSRF-Token": csrf},
            data={"parse_intent": "order_xlsx"},
            files={"file": ("not-a-workbook.xlsx", b"bad", "application/octet-stream")},
        )
        assert xlsx_like.status_code == 400
        assert xlsx_like.json()["error"]["code"] == "FILE_UNSUPPORTED"

        async with async_session_factory() as session:
            artifact = await session.get(InputArtifact, artifact_id)
            assert artifact is not None
            path = storage_path(settings.attachments_root, artifact.storage_key)
        path.write_bytes(b"tampered")
        response = await client.post(
            f"/api/v1/artifacts/{artifact_id}/parse",
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "FILE_INTEGRITY_FAILED"
        async with async_session_factory() as session:
            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "artifact.parse_failed",
                    AuditEvent.input_hash == upload.json()["data"]["sha256"],
                )
            )
            assert audit is not None
            assert audit.after_ref["code"] == "FILE_INTEGRITY_FAILED"
            assert audit.after_ref["elapsed_ms"] >= 0
