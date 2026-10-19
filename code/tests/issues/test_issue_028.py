"""M4-07 固定标注集、真实文件解析与异常验收。"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.service import save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import AgentTask, Draft, Order, TaskSession, TaskWait
from app.parsing.image import parse_image
from app.parsing.pdf import parse_pdf, sha256_file

API_URL = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:5173")
TESTS_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = TESTS_ROOT / "fixtures" / "m4_multimodal_cases.jsonl"
XLSX_HEADERS = (
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
)
NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
NORTH_DEPT = UUID("00000000-0000-4000-8000-000000000001")
NORTH_CONTRACT = UUID("00000000-0000-4000-8000-000000000050")
NORTH_CUSTOMER = UUID("00000000-0000-4000-8000-000000000010")
NORTH_PROJECT = UUID("00000000-0000-4000-8000-000000000020")


def _cases() -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines()
    ]


def _text_pdf(lines: list[str]) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        }
    )
    operations = " ".join(
        f"({line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')}) Tj 0 -22 Td"
        for line in lines
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 13 Tf 40 740 Td {operations} ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _scan_pdf(lines: list[str]) -> bytes:
    image = Image.new("RGB", (2400, max(360, len(lines) * 135 + 40)), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=64)
    for index, line in enumerate(lines):
        draw.text((50, 25 + index * 130), line, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=200)
    return output.getvalue()


def _mixed_pdf(text_lines: list[str], scan_lines: list[str]) -> bytes:
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_text_pdf(text_lines))))
    writer.append(PdfReader(io.BytesIO(_scan_pdf(scan_lines))))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _workbook(rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单明细"
    sheet.append(XLSX_HEADERS)
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _parse_case(case: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    if case["kind"] == "pdf_text":
        content = _text_pdf(case["lines"])
        path = tmp_path / f"{case['id']}.pdf"
        path.write_bytes(content)
        return parse_pdf(path, sha256_file(path))
    if case["kind"] == "pdf_scan":
        content = _scan_pdf(case["lines"])
        path = tmp_path / f"{case['id']}.pdf"
        path.write_bytes(content)
        return parse_pdf(path, sha256_file(path))
    if case["kind"] == "pdf_mixed":
        content = _mixed_pdf(case["text_lines"], case["scan_lines"])
        path = tmp_path / f"{case['id']}.pdf"
        path.write_bytes(content)
        return parse_pdf(path, sha256_file(path))
    if case["kind"] == "image":
        path = TESTS_ROOT / case["fixture"]
        return parse_image(path, hashlib.sha256(path.read_bytes()).hexdigest())
    raise AssertionError(f"不支持的固定文件类型：{case['kind']}")


def test_fixed_labeled_files_measure_exact_match_coverage_and_ambiguity(
    tmp_path: Path,
) -> None:
    cases = [case for case in _cases() if case["kind"] != "xlsx"]
    results = {case["id"]: _parse_case(case, tmp_path) for case in cases}
    expected_count = 0
    found_count = 0
    exact_count = 0
    predicted_count = 0
    ambiguous_count = 0
    ambiguous_pass_count = 0
    mismatches: list[dict[str, object]] = []

    for case in cases:
        result = results[case["id"]]
        fields = result["fields"]
        by_name: dict[str, list[dict[str, Any]]] = {}
        for field in fields:
            by_name.setdefault(field["field"], []).append(field)
        for field_name, expected in case.get("expected", {}).items():
            expected_count += 1
            candidates = by_name.get(field_name, [])
            predicted_count += len(candidates)
            found_count += bool(candidates)
            matched = any(row["value"] == expected for row in candidates)
            exact_count += matched
            if not matched:
                mismatches.append(
                    {
                        "case": case["id"],
                        "field": field_name,
                        "expected": expected,
                        "actual": [row["value"] for row in candidates],
                    }
                )
        for field_name, expected_values in case.get("expected_conflicts", {}).items():
            ambiguous_count += 1
            candidates = by_name.get(field_name, [])
            actual_values = {row["value"] for row in candidates}
            routed = (
                actual_values == set(expected_values)
                and all(row["status"] == "CONFLICT" for row in candidates)
                and all(row["confirmation_required"] for row in candidates)
            )
            ambiguous_pass_count += routed
            assert routed, f"{case['id']} {field_name}: {candidates}"

        if case["kind"] == "pdf_scan":
            assert result["pages"][0]["mode"] == "OCR"
        if case["kind"] == "pdf_mixed":
            assert [page["mode"] for page in result["pages"]] == ["TEXT", "OCR"]
            for field_name, expected_page in case["expected_pages"].items():
                assert (
                    next(row for row in fields if row["field"] == field_name)[
                        "page_number"
                    ]
                    == expected_page
                )

    precision = exact_count / predicted_count if predicted_count else 0
    accuracy = exact_count / expected_count if expected_count else 0
    coverage = found_count / expected_count if expected_count else 0
    ambiguity_rate = ambiguous_pass_count / ambiguous_count if ambiguous_count else 1
    report = {
        "dataset": "m4-multimodal-fixed-v1",
        "cases": len(cases),
        "expected_key_fields": expected_count,
        "exact_matches": exact_count,
        "candidate_precision": round(precision, 4),
        "key_field_exact_match_rate": round(accuracy, 4),
        "key_field_coverage": round(coverage, 4),
        "ambiguous_fields": ambiguous_count,
        "ambiguity_routed_for_confirmation": round(ambiguity_rate, 4),
        "ocr": "本地 Tesseract 实际识别；未调用在线 OCR 服务",
        "mismatches": mismatches,
    }
    print("M4-07 固定标注集：" + json.dumps(report, ensure_ascii=False, sort_keys=True))
    assert precision >= 0.90, report
    assert accuracy >= 0.90, report
    assert coverage >= 0.90, report
    assert ambiguity_rate == 1.0, report


async def _client() -> httpx.AsyncClient:
    assert os.getenv("TRADEPILOT_TEST_MODE") == "1", "请通过隔离 Compose 验收脚本运行"
    password = os.getenv("DEMO_PASSWORD", "")
    assert len(password) >= 12
    client = httpx.AsyncClient(base_url=API_URL, timeout=60)
    login = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": "demo-operator-north", "password": password},
    )
    assert login.status_code == 200, login.text
    client.headers.update(
        {"Origin": ORIGIN, "X-CSRF-Token": login.json()["data"]["csrf_token"]}
    )
    return client


@pytest.mark.asyncio
async def test_excel_fixed_fixture_preserves_multirow_groups_and_conflict_locations() -> (
    None
):
    case = next(case for case in _cases() if case["kind"] == "xlsx")
    client = await _client()
    try:
        before = await _order_count()
        content = _workbook(case["rows"])
        uploaded = await client.post(
            "/api/v1/artifacts",
            data={"parse_intent": "order_xlsx"},
            files={"file": ("m4-07-fixture.xlsx", content, "application/octet-stream")},
        )
        assert uploaded.status_code == 201, uploaded.text
        preview = await client.post(
            "/api/v1/imports/preview",
            json={"artifact_id": uploaded.json()["data"]["artifact_id"]},
        )
        assert preview.status_code == 200, preview.text
        data = preview.json()["data"]
        for key, value in case["expected"]["summary"].items():
            assert data["summary"][key] == value
        groups = {row["group_id"]: row for row in data["groups"]}
        ready = groups["多明细组"]
        assert ready["status"] == "READY_FOR_REVIEW"
        assert len(ready["candidate"]["items"]) == case["expected"]["ready_group_items"]
        assert ready["total_amount"] == "500.00"
        conflict = groups["主档冲突组"]
        assert conflict["status"] == "NEEDS_CORRECTION"
        assert any(
            issue["code"] == "GROUP_FIELD_CONFLICT"
            and issue["locator"]["cell"] == case["expected"]["conflict_cell"]
            for issue in conflict["issues"]
        )
        assert await _order_count() == before
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_image_alias_and_missing_specification_require_real_material_choice() -> (
    None
):
    case = next(case for case in _cases() if case["kind"] == "image")
    image_path = TESTS_ROOT / case["fixture"]
    parsed = parse_image(
        image_path, hashlib.sha256(image_path.read_bytes()).hexdigest()
    )
    material_name = next(
        row["value"] for row in parsed["fields"] if row["field"] == "material"
    )
    unit = next(
        row.get("unit") for row in parsed["fields"] if row["field"] == "quantity"
    )
    assert material_name == case["expected"]["material"]

    task_id, draft_version = await _create_waiting_task(material_name, unit)
    client = await _client()
    try:
        before_orders = await _order_count()
        response = await client.post(
            f"/api/v1/tasks/{task_id}/material-matches",
            json={"expected_version": draft_version, "item_index": 0},
        )
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert "MATERIAL_CONFIRMATION_REQUIRED" in {
            issue["code"] for issue in result["issues"]
        }
        candidates = result["data"]["candidates"]
        assert {row["code"] for row in candidates} == {
            "TP-DEMO-RB12",
            "TP-DEMO-RB16",
        }
        assert all(row["requires_confirmation"] for row in candidates)
        assert {row["specification"] for row in candidates} == {
            "HRB400 12mm",
            "HRB400 16mm",
        }
        assert await _order_count() == before_orders
    finally:
        await client.aclose()


async def _create_waiting_task(raw_name: str, unit: str | None) -> tuple[UUID, int]:
    task_id, session_id, draft_id = uuid4(), uuid4(), uuid4()
    checkpoint_id, interrupt_id = f"issue028-cp-{uuid4()}", f"issue028-int-{uuid4()}"
    actor = AuthContext(
        user_id=NORTH_USER,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({NORTH_DEPT}),
    )
    async with async_session_factory() as session, session.begin():
        session.add(
            TaskSession(id=session_id, owner_id=NORTH_USER, title="M4-07 图片物料确认")
        )
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=NORTH_USER,
                department_id=NORTH_DEPT,
                task_type="SALES",
                state="WAITING_INPUT",
                current_step="await_input",
                checkpoint_ref=checkpoint_id,
            )
        )
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
                contract_id=NORTH_CONTRACT,
                customer_id=NORTH_CUSTOMER,
                project_id=NORTH_PROJECT,
                currency="CNY",
                items=(
                    CandidateOrderItem(
                        raw_name=raw_name,
                        specification=None,
                        quantity="20",
                        unit=unit,
                        unit_price="3500",
                    ),
                ),
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
                payload={"kind": "input", "missing_fields": ["specification"]},
                active=True,
            )
        )
    return task_id, saved.version


async def _order_count() -> int:
    async with async_session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Order)) or 0)
