"""M3-06 主数据权限、合同额度、物料别名及审计集成验证。"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.rules import precheck_candidate
from app.drafts.types import CandidateOrder
from app.models import (
    AuditEvent,
    Contract,
    ContractBalance,
    Draft,
    DraftItem,
    DraftRevision,
    Material,
    MaterialAlias,
)

API_URL = os.getenv("TRADEPILOT_API_URL", "http://api:8000")
APP_ORIGIN = "http://localhost:5173"
NORTH = UUID("00000000-0000-4000-8000-000000000001")
NORTH_OPERATOR = UUID("00000000-0000-4000-8000-000000000100")
NORTH_CONTRACT = UUID("00000000-0000-4000-8000-000000000050")
INACTIVE_CONTRACT = UUID("00000000-0000-4000-8000-000000000052")


async def _login(client: httpx.AsyncClient, login: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": os.environ["DEMO_PASSWORD"]},
        headers={"Origin": APP_ORIGIN},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_master_data_permissions_contract_rules_and_alias_audit() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_020.ps1 在隔离 PostgreSQL Compose 环境执行")
    assert os.getenv("DEMO_MODE", "").casefold() == "true"
    assert os.getenv("DEMO_PASSWORD")

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as admin:
        admin_identity = await _login(admin, "demo-admin")
        assert "system_admin" in admin_identity["roles"]
        csrf = str(admin_identity["csrf_token"])
        admin.headers.update({"Origin": APP_ORIGIN, "X-CSRF-Token": csrf})

        ledger = await admin.get("/api/v1/admin/contracts")
        assert ledger.status_code == 200, ledger.text
        contract = next(
            item
            for item in ledger.json()["data"]["items"]
            if item["number"] == "TP-DEMO-SALES-N1"
        )
        assert contract["occupied_amount"] == "0.00"
        assert contract["remaining_amount"] == contract["total_amount"]

        # 台账统计是服务端事实，不能通过合同写接口伪造；额度下限来自余额账本。
        csrf_missing = await admin.patch(
            f"/api/v1/admin/contracts/{contract['id']}",
            json={"expected_version": contract["version"], "status": "ACTIVE"},
            headers={"Origin": APP_ORIGIN, "X-CSRF-Token": ""},
        )
        assert csrf_missing.status_code == 403
        forged_balance = await admin.patch(
            f"/api/v1/admin/contracts/{contract['id']}",
            json={
                "expected_version": contract["version"],
                "occupied_amount": "1",
            },
        )
        assert forged_balance.status_code == 422

        async with async_session_factory() as session:
            balance = await session.get(ContractBalance, NORTH_CONTRACT)
            assert balance is not None
            old_occupied = balance.occupied_amount
            balance.occupied_amount = Decimal("150001.00")
            await session.commit()
        try:
            below_balance = await admin.patch(
                f"/api/v1/admin/contracts/{contract['id']}",
                json={
                    "expected_version": contract["version"],
                    "total_amount": "150000.00",
                },
            )
            assert below_balance.status_code == 409, below_balance.text
            assert below_balance.json()["error"]["code"] == (
                "CONTRACT_TOTAL_BELOW_OCCUPIED"
            )
        finally:
            async with async_session_factory() as session:
                balance = await session.get(ContractBalance, NORTH_CONTRACT)
                assert balance is not None
                balance.occupied_amount = old_occupied
                await session.commit()

        async with async_session_factory() as session:
            inactive = await session.get(Contract, INACTIVE_CONTRACT)
            assert inactive is not None and inactive.status == "INACTIVE"
            operator = AuthContext(
                user_id=NORTH_OPERATOR,
                login="demo-operator-north",
                roles=frozenset({BUSINESS_OPERATOR}),
                department_ids=frozenset({NORTH}),
            )
            result = await precheck_candidate(
                session,
                candidate=CandidateOrder(
                    order_type="SALES", contract_id=INACTIVE_CONTRACT
                ),
                actor=operator,
                business_date=date.today(),
            )
            assert "CONTRACT_INACTIVE" in {issue.code for issue in result.issues}

        async with httpx.AsyncClient(base_url=API_URL, timeout=20) as operator:
            await _login(operator, "demo-operator-north")
            denied = await operator.get("/api/v1/admin/contracts")
            assert denied.status_code == 403

        marker = f"TP-验证别名-{uuid4().hex}"
        created_ids: list[str] = []
        snapshot_draft_id = uuid4()
        try:
            material_payload = {
                "name": "验收标准物料",
                "specification": "验收规格",
                "unit": "件",
                "active": True,
                "aliases": [marker],
            }
            first = await admin.post(
                "/api/v1/admin/materials",
                json={"code": f"VERIFY-{uuid4().hex[:16]}", **material_payload},
            )
            assert first.status_code == 201, first.text
            first_data = first.json()["data"]
            created_ids.append(first_data["id"])
            second = await admin.post(
                "/api/v1/admin/materials",
                json={
                    "code": f"VERIFY-{uuid4().hex[:16]}",
                    **material_payload,
                    "name": "另一个验收标准物料",
                },
            )
            assert second.status_code == 201, second.text
            second_data = second.json()["data"]
            created_ids.append(second_data["id"])

            async with async_session_factory() as session:
                session.add(
                    Draft(
                        id=snapshot_draft_id,
                        current_version=1,
                        lock_state="EDITABLE",
                    )
                )
                session.add(
                    DraftRevision(
                        draft_id=snapshot_draft_id,
                        version=1,
                        content_hash="a" * 64,
                        validation={},
                        changed_by=UUID(admin_identity["user_id"]),
                    )
                )
                await session.flush()
                session.add(
                    DraftItem(
                        draft_id=snapshot_draft_id,
                        version=1,
                        line_no=1,
                        material_id=UUID(first_data["id"]),
                        raw_name="历史业务名称",
                        specification="历史规格",
                        quantity=Decimal("3.000000"),
                        unit="件",
                        unit_price=Decimal("12.000000"),
                        amount=Decimal("36.00"),
                    )
                )
                await session.commit()

            preview = await admin.get(
                "/api/v1/admin/materials/match-preview",
                params={"raw_name": marker},
            )
            assert preview.status_code == 200, preview.text
            candidates = preview.json()["data"]["candidates"]
            assert {row["material_id"] for row in candidates} == set(created_ids)
            assert all(row["requires_confirmation"] for row in candidates)

            changed = await admin.patch(
                f"/api/v1/admin/materials/{created_ids[0]}",
                json={
                    "expected_version": first_data["version"],
                    "name": "已更新验收标准物料",
                    "aliases": [marker, f"{marker}-新别名"],
                },
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["data"]["version"] == first_data["version"] + 1
            stale = await admin.patch(
                f"/api/v1/admin/materials/{created_ids[0]}",
                json={
                    "expected_version": first_data["version"],
                    "name": "旧版本覆盖",
                },
            )
            assert stale.status_code == 409
            async with async_session_factory() as session:
                aliases = list(
                    (
                        await session.scalars(
                            select(MaterialAlias).where(
                                MaterialAlias.material_id == UUID(created_ids[0])
                            )
                        )
                    ).all()
                )
                assert all(row.normalized_alias for row in aliases)
                event = await session.scalar(
                    select(AuditEvent)
                    .where(
                        AuditEvent.action == "admin.material.updated",
                        AuditEvent.object_ref["id"].as_string() == created_ids[0],
                    )
                    .order_by(AuditEvent.created_at.desc())
                )
                assert event is not None and event.actor_id == UUID(
                    admin_identity["user_id"]
                )
                assert event.before_ref["name"] == "验收标准物料"
                assert event.after_ref["name"] == "已更新验收标准物料"
                snapshot = await session.get(DraftItem, (snapshot_draft_id, 1, 1))
                assert snapshot is not None
                assert (snapshot.raw_name, snapshot.specification, snapshot.unit) == (
                    "历史业务名称",
                    "历史规格",
                    "件",
                )
        finally:
            async with async_session_factory() as session:
                snapshot = await session.get(DraftItem, (snapshot_draft_id, 1, 1))
                revision = await session.get(DraftRevision, (snapshot_draft_id, 1))
                draft = await session.get(Draft, snapshot_draft_id)
                if snapshot is not None:
                    await session.delete(snapshot)
                    await session.flush()
                if revision is not None:
                    await session.delete(revision)
                    await session.flush()
                if draft is not None:
                    await session.delete(draft)
                    await session.flush()
                for material_id in map(UUID, created_ids):
                    await session.execute(
                        MaterialAlias.__table__.delete().where(
                            MaterialAlias.material_id == material_id
                        )
                    )
                    material = await session.get(Material, material_id)
                    if material is not None:
                        await session.delete(material)
                await session.commit()
