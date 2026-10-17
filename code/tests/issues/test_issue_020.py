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
from app.drafts.service import save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import (
    AgentTask,
    AuditEvent,
    Contract,
    ContractBalance,
    ContractLine,
    ContractLineBalance,
    Draft,
    DraftItem,
    DraftRevision,
    Material,
    MaterialAlias,
    Order,
    OrderItem,
    QuotaOccupation,
    TaskApproval,
    TaskCommand,
    TaskSession,
)
from app.orders.commit import commit_approved_order

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


async def _create_approved_submission(
    candidate: CandidateOrder, *, title: str
) -> tuple[UUID, UUID, UUID, UUID]:
    task_id, session_id, draft_id = uuid4(), uuid4(), uuid4()
    command_id, approval_id = uuid4(), uuid4()
    checkpoint_id, interrupt_id = f"m3-06-{uuid4()}", f"m3-06-{uuid4()}"
    operator = AuthContext(
        user_id=NORTH_OPERATOR,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({NORTH}),
    )

    async with async_session_factory() as session, session.begin():
        session.add(TaskSession(id=session_id, owner_id=NORTH_OPERATOR, title=title))
        await session.flush()
        session.add(
            AgentTask(
                id=task_id,
                session_id=session_id,
                owner_id=NORTH_OPERATOR,
                department_id=NORTH,
                task_type="SALES",
                state="SUBMITTING",
                current_step="commit_order",
                checkpoint_ref=checkpoint_id,
            )
        )
        session.add(
            Draft(
                id=draft_id,
                task_id=task_id,
                current_version=0,
                lock_state="EDITABLE",
            )
        )
        await session.flush()
        saved = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=0,
            candidate=candidate,
            actor=operator,
        )
        payload = {
            "decision": "APPROVE",
            "draft_id": str(draft_id),
            "content_hash": saved.content_hash,
            "checkpoint_id": checkpoint_id,
            "interrupt_id": interrupt_id,
        }
        command = TaskCommand(
            id=command_id,
            task_id=task_id,
            actor_id=UUID("00000000-0000-4000-8000-000000000102"),
            kind="REVIEW",
            payload=payload,
            expected_version=saved.version,
            target_interrupt_id=interrupt_id,
            request_id=f"issue020-{uuid4()}",
            payload_hash="a" * 64,
        )
        session.add(command)
        await session.flush()
        session.add(
            TaskApproval(
                id=approval_id,
                task_id=task_id,
                command_id=command_id,
                draft_id=draft_id,
                actor_id=command.actor_id,
                department_id=NORTH,
                version=saved.version,
                content_hash=saved.content_hash,
                checkpoint_ref=checkpoint_id,
                interrupt_id=interrupt_id,
                decision="APPROVE",
            )
        )
        draft = await session.get(Draft, draft_id)
        assert draft is not None
        draft.lock_state = "SUBMITTING"

    return task_id, draft_id, command_id, approval_id


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

        # 余额账本缺失时不能把未知占用伪装成零，也不能继续修改合同。
        async with async_session_factory() as session:
            balance = await session.get(ContractBalance, NORTH_CONTRACT)
            assert balance is not None
            saved_balance = (
                balance.occupied_amount,
                balance.occupied_order_count,
                balance.version,
            )
            await session.delete(balance)
            await session.commit()
        try:
            for path in (
                "/api/v1/admin/contracts",
                f"/api/v1/admin/contracts/{contract['id']}",
            ):
                missing_ledger = await admin.get(path)
                assert missing_ledger.status_code == 503, missing_ledger.text
                assert missing_ledger.json()["error"]["code"] == (
                    "CONTRACT_BALANCE_UNAVAILABLE"
                )
            blocked_edit = await admin.patch(
                f"/api/v1/admin/contracts/{contract['id']}",
                json={
                    "expected_version": contract["version"],
                    "total_amount": contract["total_amount"],
                },
            )
            assert blocked_edit.status_code == 503, blocked_edit.text
            assert blocked_edit.json()["error"]["code"] == (
                "CONTRACT_BALANCE_UNAVAILABLE"
            )
        finally:
            async with async_session_factory() as session:
                if await session.get(ContractBalance, NORTH_CONTRACT) is None:
                    session.add(
                        ContractBalance(
                            contract_id=NORTH_CONTRACT,
                            occupied_amount=saved_balance[0],
                            occupied_order_count=saved_balance[1],
                            version=saved_balance[2],
                        )
                    )
                    await session.commit()

        # 已配置数量上限的合同明细缺账时，也不能跳过履约数量保护。
        detail_response = await admin.get(f"/api/v1/admin/contracts/{contract['id']}")
        assert detail_response.status_code == 200, detail_response.text
        contract_detail = detail_response.json()["data"]
        assert contract_detail["lines"]
        line_data = contract_detail["lines"][0]
        line_id = UUID(line_data["id"])
        async with async_session_factory() as session:
            line = await session.get(ContractLine, line_id)
            line_balance = await session.get(ContractLineBalance, line_id)
            assert line is not None and line_balance is not None
            saved_max_quantity = line.max_quantity
            saved_occupied_quantity = line_balance.occupied_quantity
            line.max_quantity = Decimal("999999999.000000")
            await session.delete(line_balance)
            await session.commit()
        try:
            blocked_quantity_edit = await admin.patch(
                f"/api/v1/admin/contracts/{contract['id']}",
                json={
                    "expected_version": contract["version"],
                    "lines": [
                        {
                            "line_id": str(line_id),
                            "material_id": line_data["material_id"],
                            "unit": line_data["unit"],
                            "unit_price": line_data["unit_price"],
                            "max_quantity": "1.000000",
                        }
                    ],
                },
            )
            assert blocked_quantity_edit.status_code == 503, blocked_quantity_edit.text
            assert blocked_quantity_edit.json()["error"]["code"] == (
                "CONTRACT_LINE_BALANCE_UNAVAILABLE"
            )
        finally:
            async with async_session_factory() as session:
                line = await session.get(ContractLine, line_id)
                assert line is not None
                line.max_quantity = saved_max_quantity
                if await session.get(ContractLineBalance, line_id) is None:
                    session.add(
                        ContractLineBalance(
                            contract_line_id=line_id,
                            occupied_quantity=saved_occupied_quantity,
                        )
                    )
                await session.commit()

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
        unsupported_currency = await admin.patch(
            f"/api/v1/admin/contracts/{contract['id']}",
            json={"expected_version": contract["version"], "currency": "USD"},
        )
        assert unsupported_currency.status_code == 422

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

        # 数字化 UUID 避免匹配器把随机字母数字片段误认为规格型号。
        marker = f"TP-验证别名-{uuid4().int}"
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


@pytest.mark.asyncio
async def test_inactivated_contract_is_blocked_inside_final_order_transaction() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_020.ps1 在隔离 PostgreSQL Compose 环境执行")

    candidate = CandidateOrder(
        order_type="SALES",
        contract_id=NORTH_CONTRACT,
        customer_id=UUID("00000000-0000-4000-8000-000000000010"),
        project_id=UUID("00000000-0000-4000-8000-000000000020"),
        delivery_date=date.today(),
        address="北京市朝阳区示例工地",
        contact="张先生",
        items=(
            CandidateOrderItem(
                material_id=UUID("00000000-0000-4000-8000-000000000030"),
                raw_name="螺纹钢",
                specification="HRB400 12mm",
                quantity=Decimal("2.000000"),
                unit="吨",
                unit_price=Decimal("3500.000000"),
            ),
        ),
    )
    task_id, draft_id, command_id, approval_id = await _create_approved_submission(
        candidate, title="AC-18 停用合同阻断"
    )

    try:
        async with async_session_factory() as session, session.begin():
            contract = await session.get(Contract, NORTH_CONTRACT)
            assert contract is not None and contract.status == "ACTIVE"
            contract.status = "INACTIVE"

        async with async_session_factory() as session, session.begin():
            result = await commit_approved_order(
                session,
                task_id=task_id,
                command_id=command_id,
                approval_id=approval_id,
            )
            assert result.state == "BLOCKED_RULE"
            assert result.code == "CONTRACT_INACTIVE"

        async with async_session_factory() as session:
            assert (
                await session.scalar(select(Order).where(Order.task_id == task_id))
                is None
            )
            assert (
                await session.scalar(
                    select(QuotaOccupation).where(QuotaOccupation.task_id == task_id)
                )
                is None
            )
            approval = await session.get(TaskApproval, approval_id)
            draft = await session.get(Draft, draft_id)
            balance = await session.get(ContractBalance, NORTH_CONTRACT)
            assert approval is not None and approval.invalidated_at is not None
            assert draft is not None and draft.lock_state == "EDITABLE"
            assert balance is not None and balance.occupied_amount == Decimal("0.00")
    finally:
        async with async_session_factory() as session, session.begin():
            contract = await session.get(Contract, NORTH_CONTRACT)
            assert contract is not None
            contract.status = "ACTIVE"


@pytest.mark.asyncio
async def test_formal_order_snapshot_survives_master_material_update() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        pytest.skip("需由 verify_issue_020.ps1 在隔离 PostgreSQL Compose 环境执行")
    assert os.getenv("DEMO_MODE", "").casefold() == "true"
    assert os.getenv("DEMO_PASSWORD")

    material_id = UUID("00000000-0000-4000-8000-000000000030")
    candidate = CandidateOrder(
        order_type="SALES",
        contract_id=NORTH_CONTRACT,
        customer_id=UUID("00000000-0000-4000-8000-000000000010"),
        project_id=UUID("00000000-0000-4000-8000-000000000020"),
        delivery_date=date.today(),
        address="北京市朝阳区示例工地",
        contact="张先生",
        items=(
            CandidateOrderItem(
                material_id=material_id,
                raw_name="螺纹钢",
                specification="HRB400 12mm",
                quantity=Decimal("2.000000"),
                unit="吨",
                unit_price=Decimal("3500.000000"),
            ),
        ),
    )
    task_id, draft_id, command_id, approval_id = await _create_approved_submission(
        candidate, title="AC-18 主数据更新后的历史订单快照"
    )

    async with async_session_factory() as session:
        material = await session.get(Material, material_id)
        assert material is not None and material.active
        original_material = {
            "name": material.name,
            "specification": material.specification,
            "version": material.version,
        }
    async with async_session_factory() as session, session.begin():
        result = await commit_approved_order(
            session,
            task_id=task_id,
            command_id=command_id,
            approval_id=approval_id,
        )
        assert result.state == "COMPLETED"
        assert result.order_id is not None

    async with async_session_factory() as session:
        order = await session.scalar(select(Order).where(Order.task_id == task_id))
        approval = await session.get(TaskApproval, approval_id)
        assert order is not None and approval is not None
        revision = await session.get(DraftRevision, (draft_id, order.approved_version))
        order_item = await session.get(OrderItem, (order.id, 1))
        draft_item = await session.get(DraftItem, (draft_id, order.approved_version, 1))
        assert (
            revision is not None and order_item is not None and draft_item is not None
        )
        assert order.approved_version == approval.version == revision.version
        assert order.approved_content_hash == approval.content_hash
        assert revision.content_hash == approval.content_hash
        assert (order_item.raw_name, order_item.specification) == (
            candidate.items[0].raw_name,
            candidate.items[0].specification,
        )
        assert (draft_item.raw_name, draft_item.specification) == (
            candidate.items[0].raw_name,
            candidate.items[0].specification,
        )
        approved_snapshot = (
            order.approved_version,
            order.approved_content_hash,
            approval.version,
            approval.content_hash,
            revision.version,
            revision.content_hash,
            order_item.raw_name,
            order_item.specification,
            draft_item.raw_name,
            draft_item.specification,
        )

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as admin:
        identity = await _login(admin, "demo-admin")
        admin.headers.update(
            {
                "Origin": APP_ORIGIN,
                "X-CSRF-Token": str(identity["csrf_token"]),
            }
        )
        try:
            update = await admin.patch(
                f"/api/v1/admin/materials/{material_id}",
                json={
                    "expected_version": original_material["version"],
                    "name": "已更新的标准物料名称",
                    "specification": "更新后的标准规格",
                },
            )
            assert update.status_code == 200, update.text
            updated_material = update.json()["data"]
            assert updated_material["name"] == "已更新的标准物料名称"
            assert updated_material["specification"] == "更新后的标准规格"
            assert updated_material["version"] == original_material["version"] + 1

            async with async_session_factory() as session:
                order = await session.scalar(
                    select(Order).where(Order.task_id == task_id)
                )
                approval = await session.get(TaskApproval, approval_id)
                assert order is not None and approval is not None
                revision = await session.get(
                    DraftRevision, (draft_id, order.approved_version)
                )
                order_item = await session.get(OrderItem, (order.id, 1))
                draft_item = await session.get(
                    DraftItem, (draft_id, order.approved_version, 1)
                )
                material = await session.get(Material, material_id)

                assert revision is not None
                assert order_item is not None and draft_item is not None
                assert material is not None
                assert (
                    order.approved_version,
                    order.approved_content_hash,
                    approval.version,
                    approval.content_hash,
                    revision.version,
                    revision.content_hash,
                    order_item.raw_name,
                    order_item.specification,
                    draft_item.raw_name,
                    draft_item.specification,
                ) == approved_snapshot
                assert (material.name, material.specification) == (
                    "已更新的标准物料名称",
                    "更新后的标准规格",
                )
        finally:
            async with async_session_factory() as session:
                material = await session.get(Material, material_id)
                if material is not None and (
                    material.name != original_material["name"]
                    or material.specification != original_material["specification"]
                ):
                    restore_version = material.version
                else:
                    restore_version = None
            if restore_version is not None:
                restore = await admin.patch(
                    f"/api/v1/admin/materials/{material_id}",
                    json={
                        "expected_version": restore_version,
                        "name": original_material["name"],
                        "specification": original_material["specification"],
                    },
                )
                assert restore.status_code == 200, restore.text
