"""M3-05 正式订单列表、筛选、详情快照和部门隔离验收。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_issue_016 import _new_contract, _prepare, _submit

from app.database import async_session_factory
from app.models import FieldEvidence, Order, OrderItem, QuotaOccupation, TaskApproval

API_URL = os.getenv("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:5173")


async def _login(client: httpx.AsyncClient, login: str) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": login, "password": os.environ["DEMO_PASSWORD"]},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Origin": ORIGIN})


@pytest.mark.asyncio
async def test_order_list_filters_details_and_department_scope() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_019.ps1 在隔离 PostgreSQL Compose 环境执行")
    assert os.getenv("DEMO_MODE", "").casefold() == "true"
    assert os.getenv("DEMO_PASSWORD")

    contract_id, line_id = await _new_contract()
    prepared = await _prepare(contract_id, line_id)
    task_id = prepared[0]
    async with async_session_factory() as session, session.begin():
        approval = await session.get(TaskApproval, prepared[2])
        assert approval is not None
        session.add(
            FieldEvidence(
                id=uuid4(),
                draft_id=approval.draft_id,
                version=approval.version,
                field_path="items[0].raw_name",
                source_type="FILE_PAGE",
                artifact_id=uuid4(),
                message_id=uuid4(),
                locator={"page": 2},
                raw_value="仅用于隔离验收的敏感原文",
                accepted_value="螺纹钢",
                confirmed_by=UUID("00000000-0000-4000-8000-000000000100"),
            )
        )
    created = await _submit(prepared)
    assert created.state == "COMPLETED" and created.order_id is not None
    replay = await _submit(prepared)
    assert replay.order_id == created.order_id and replay.replayed

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as north:
        await _login(north, "demo-operator-north")
        today = datetime.now(UTC).date().isoformat()
        response = await north.get(
            "/api/v1/orders",
            params={
                "type": "SALES",
                "contract": "ISSUE16-",
                "project": "华北示例工程",
                "state": "CREATED",
                "from": today,
                "to": today,
                "limit": 10,
                "offset": 0,
            },
        )
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        result = next(
            item for item in page["items"] if item["id"] == str(created.order_id)
        )
        assert page["total"] >= 1
        assert result["task_id"] == str(task_id)
        assert result["type"] == "SALES" and result["state"] == "CREATED"
        assert result["contract_number"].startswith("ISSUE16-")

        detail_response = await north.get(f"/api/v1/orders/{created.order_id}")
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()["data"]
        assert detail["id"] == str(created.order_id)
        assert detail["task"]["id"] == str(task_id)
        assert detail["task"]["trace_url"] == f"/tasks/{task_id}"
        assert detail["operator"]["login"] == "demo-operator-north"
        assert detail["approval"]["id"] == str(prepared[2])
        assert detail["approval"]["version"] >= 1
        assert detail["approval"]["content_hash"]
        assert detail["approval"]["snapshot_amount"] == detail["total_amount"]
        assert len(detail["items"]) == 1
        assert detail["items"][0]["amount"] == detail["total_amount"]
        assert any(event["action"] == "order.committed" for event in detail["audit"])
        source = next(
            entry
            for entry in detail["sources"]
            if entry["field_path"] == "items[0].raw_name"
        )
        assert source["locator"] == {"page": 2}
        assert not {
            "artifact_id",
            "message_id",
            "raw_value",
            "accepted_value",
        }.intersection(source)
        assert all(
            "object_ref" not in event and "after_ref" not in event
            for event in detail["audit"]
        )
        assert detail["contract"]["balance_available"] is True
        assert detail["contract"]["remaining_amount"] is not None

        empty = await north.get(
            "/api/v1/orders", params={"type": "PURCHASE", "contract": "ISSUE16-"}
        )
        assert empty.status_code == 200 and empty.json()["data"]["items"] == []
        invalid_range = await north.get(
            "/api/v1/orders", params={"from": "2026-12-31", "to": "2026-01-01"}
        )
        assert invalid_range.status_code == 422

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as south:
        await _login(south, "demo-operator-south")
        hidden_detail = await south.get(f"/api/v1/orders/{created.order_id}")
        assert hidden_detail.status_code == 404
        hidden_list = await south.get(
            "/api/v1/orders", params={"contract": result["contract_number"]}
        )
        assert hidden_list.status_code == 200
        assert hidden_list.json()["data"]["items"] == []

    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.id == created.order_id)
            )
            == 1
        )
        order = await session.get(Order, created.order_id)
        assert (
            order is not None
            and order.approved_version == detail["approval"]["version"]
        )
        assert await session.scalar(
            select(func.count())
            .select_from(OrderItem)
            .where(OrderItem.order_id == created.order_id)
        ) == len(detail["items"])
        assert (
            await session.scalar(
                select(func.count())
                .select_from(QuotaOccupation)
                .where(QuotaOccupation.order_id == created.order_id)
            )
            == 1
        )
        # 重放提交只返回原始结果；列表与详情查询本身不写入业务数据。
        assert order.task_id == UUID(detail["task"]["id"])


@pytest.mark.asyncio
async def test_order_detail_does_not_expose_other_department_or_accept_unknown_state() -> (
    None
):
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_019.ps1 在隔离 PostgreSQL Compose 环境执行")
    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        await _login(client, "demo-operator-north")
        invalid_state = await client.get("/api/v1/orders", params={"state": "WAITING"})
        assert invalid_state.status_code == 422
        async with httpx.AsyncClient(base_url=API_URL, timeout=20) as unauthenticated:
            response = await unauthenticated.get("/api/v1/orders")
            assert response.status_code == 401
