"""M4-06 多模态文件、来源定位、人工确认与草稿版本验收。"""

from __future__ import annotations

import os
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.service import load_draft, save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import (
    AgentTask,
    AuditEvent,
    Draft,
    FieldEvidence,
    Order,
    TaskWait,
)

API_URL = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:5173")
NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
NORTH_DEPT = UUID("00000000-0000-4000-8000-000000000001")


def _text_pdf(text: str) -> bytes:
    """生成带可提取文本的最小 PDF，避免验收依赖 OCR 服务。"""

    def escape(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    lines = text.splitlines()
    operators = " ".join(f"({escape(line)}) Tj 0 -18 Td" for line in lines)
    stream = f"BT /F1 12 Tf 40 700 Td {operators} ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


async def _login() -> httpx.AsyncClient:
    assert os.getenv("TRADEPILOT_TEST_MODE") == "1", "请通过隔离 Compose 验收脚本运行"
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    client = httpx.AsyncClient(base_url=API_URL, timeout=45)
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": "demo-operator-north", "password": password},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Origin": ORIGIN, "X-CSRF-Token": response.json()["data"]["csrf_token"]}
    )
    return client


@pytest.mark.asyncio
async def test_multimodal_attachment_confirmation_is_authorized_and_versioned() -> None:
    client = await _login()
    try:
        before_orders = await _order_count()
        upload = await client.post(
            "/api/v1/artifacts",
            data={"parse_intent": "contract_pdf"},
            files={
                "file": (
                    "数量来源.pdf",
                    _text_pdf(
                        "quantity: 20 tons; source evidence for order draft\n"
                        "quantity: 25 tons; conflicting candidate"
                    ),
                    "application/pdf",
                )
            },
        )
        assert upload.status_code == 201, upload.text
        artifact_id = UUID(upload.json()["data"]["artifact_id"])

        parsed_response = await client.post(f"/api/v1/artifacts/{artifact_id}/parse")
        assert parsed_response.status_code == 200, parsed_response.text
        parsed = parsed_response.json()["data"]
        quantities = [row for row in parsed["fields"] if row["field"] == "quantity"]
        assert len(quantities) == 2
        assert all(row["status"] == "CONFLICT" for row in quantities)
        quantity = next(row for row in quantities if row["value"] == "20")
        assert quantity["value"] == "20"
        assert quantity["page_number"] == 1
        assert quantity["locator"]["type"] == "text_range"

        idempotency_key = f"issue027-create-{uuid4()}"
        create_body = {
            "order_type": "SALES",
            "input": {
                "text": "根据已上传文件整理订单草稿，字段不明确时等待我补充。",
                "artifact_ids": [str(artifact_id)],
            },
        }
        created = await client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": idempotency_key},
            json=create_body,
        )
        assert created.status_code == 202, created.text
        task_id = UUID(created.json()["data"]["task_id"])
        replay = await client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": idempotency_key},
            json=create_body,
        )
        assert replay.status_code == 202, replay.text
        assert UUID(replay.json()["data"]["task_id"]) == task_id
        typed_tasks = await client.get("/api/v1/tasks?input_type=TEXT_AND_FILES")
        assert typed_tasks.status_code == 200, typed_tasks.text
        listed = next(
            row
            for row in typed_tasks.json()["data"]["items"]
            if row["task_id"] == str(task_id)
        )
        assert listed["input_type"] == "TEXT_AND_FILES"
        linked = await client.get(f"/api/v1/tasks/{task_id}/artifacts")
        assert linked.status_code == 200, linked.text
        assert [row["artifact_id"] for row in linked.json()["data"]["items"]] == [
            str(artifact_id)
        ]

        checkpoint_id, interrupt_id, draft_id = (
            f"issue027-cp-{uuid4()}",
            f"issue027-int-{uuid4()}",
            uuid4(),
        )
        actor = AuthContext(
            user_id=NORTH_USER,
            login="demo-operator-north",
            roles=frozenset({BUSINESS_OPERATOR}),
            department_ids=frozenset({NORTH_DEPT}),
        )
        async with async_session_factory() as session, session.begin():
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.state = "WAITING_INPUT"
            task.current_step = "await_input"
            task.checkpoint_ref = checkpoint_id
            draft = Draft(
                id=draft_id, task_id=task_id, current_version=0, lock_state="EDITABLE"
            )
            session.add(draft)
            await session.flush()
            saved = await save_candidate(
                session,
                draft_id=draft_id,
                expected_version=0,
                candidate=CandidateOrder(
                    order_type="SALES",
                    items=(CandidateOrderItem(raw_name="钢材"),),
                ),
                actor=actor,
            )
            session.add(
                TaskWait(
                    id=uuid4(),
                    task_id=task_id,
                    checkpoint_ref=checkpoint_id,
                    interrupt_id=interrupt_id,
                    kind="INPUT",
                    draft_version=saved.version,
                    payload={"kind": "input", "missing_fields": []},
                    active=True,
                )
            )

        body = {
            "expected_version": 1,
            "interrupt_id": interrupt_id,
            "checkpoint_id": checkpoint_id,
            "patch": {
                "items": [
                    {
                        "material_id": None,
                        "raw_name": "钢材",
                        "specification": None,
                        "quantity": quantity["value"],
                        "unit": quantity.get("unit"),
                        "unit_price": None,
                    }
                ]
            },
            "file_confirmations": [
                {
                    "field_path": "items[0].quantity",
                    "artifact_id": str(artifact_id),
                    "page_number": quantity["page_number"],
                    "locator": quantity["locator"],
                    "raw_value": quantity["raw_text"],
                }
            ],
        }
        applied = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={"Idempotency-Key": f"issue027-apply-{uuid4()}"},
            json=body,
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["version"] == 2
        stale = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={"Idempotency-Key": f"issue027-stale-{uuid4()}"},
            json=body,
        )
        assert stale.status_code == 409, stale.text

        async with async_session_factory() as session:
            draft_snapshot = await load_draft(session, draft_id)
            assert draft_snapshot is not None and draft_snapshot.version == 2
            assert draft_snapshot.candidate is not None
            assert draft_snapshot.candidate.items[0].quantity == Decimal(20)
            evidence = await session.scalar(
                select(FieldEvidence).where(
                    FieldEvidence.draft_id == draft_id,
                    FieldEvidence.version == 2,
                    FieldEvidence.field_path == "items[0].quantity",
                    FieldEvidence.artifact_id == artifact_id,
                )
            )
            assert evidence is not None
            assert evidence.source_type == "USER_EDIT"
            assert evidence.confirmed_by == NORTH_USER
            assert evidence.locator == {**quantity["locator"], "page_number": 1}
            assert evidence.raw_value == quantity["raw_text"]
            assert evidence.accepted_value == "20"
            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.task_id == task_id,
                    AuditEvent.action == "draft.file_field_confirmed",
                )
            )
            assert audit is not None and audit.actor_id == NORTH_USER
        assert await _order_count() == before_orders
    finally:
        await client.aclose()


async def _order_count() -> int:
    async with async_session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Order)) or 0)
