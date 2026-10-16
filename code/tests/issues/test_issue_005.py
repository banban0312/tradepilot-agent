"""验证合同关联、字段规则及只读额度预检。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.rules import precheck_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import ContractBalance

NORTH = UUID("00000000-0000-4000-8000-000000000001")
CONTRACT_ID = UUID("00000000-0000-4000-8000-000000000050")
INACTIVE_CONTRACT_ID = UUID("00000000-0000-4000-8000-000000000052")
SOUTH_CONTRACT_ID = UUID("00000000-0000-4000-8000-000000000053")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000010")
PROJECT_ID = UUID("00000000-0000-4000-8000-000000000020")
MATERIAL_ID = UUID("00000000-0000-4000-8000-000000000030")


def _actor(department_id: UUID = NORTH) -> AuthContext:
    return AuthContext(
        user_id=UUID("00000000-0000-4000-8000-000000000100"),
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({department_id}),
    )


def _candidate(
    *,
    quantity: str = "62.857143",
    contract_id: UUID = CONTRACT_ID,
    order_type: str = "SALES",
    delivery_date: date = date(2026, 10, 20),
    unit: str = "吨",
    unit_price: str | None = "3500",
    material_id: UUID | None = MATERIAL_ID,
    raw_name: str = "螺纹钢",
    specification: str = "HRB400 12mm",
) -> CandidateOrder:
    return CandidateOrder(
        order_type=order_type,  # type: ignore[arg-type]
        contract_id=contract_id,
        customer_id=CUSTOMER_ID,
        project_id=PROJECT_ID,
        delivery_date=delivery_date,
        address="北京市朝阳区示例工地",
        contact="张先生",
        items=(
            CandidateOrderItem(
                material_id=material_id,
                raw_name=raw_name,
                specification=specification,
                quantity=quantity,
                unit=unit,
                unit_price=unit_price,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_excess_and_equal_amount_prechecks_do_not_reserve_balance() -> None:
    async with async_session_factory() as session, session.begin():
        before = await session.get(ContractBalance, CONTRACT_ID)
        assert before is not None and before.occupied_amount == Decimal("0.00")

        over = await precheck_candidate(
            session,
            candidate=_candidate(),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert over.contract_amount == Decimal("150000.00")
        assert over.occupied_amount == Decimal("0.00")
        assert over.remaining_amount == Decimal("150000.00")
        assert over.requested_amount == Decimal("220000.00")
        assert over.excess_amount == Decimal("70000.00")
        assert not over.can_proceed_to_approval
        assert "CONTRACT_AMOUNT_EXCEEDED" in {issue.code for issue in over.issues}

        equal = await precheck_candidate(
            session,
            candidate=_candidate(quantity="42.857143"),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert equal.requested_amount == Decimal("150000.00")
        assert equal.excess_amount == Decimal("0.00")
        assert equal.can_proceed_to_approval

        after = await session.get(ContractBalance, CONTRACT_ID)
        assert after is not None and after.occupied_amount == Decimal("0.00")
        assert after.occupied_order_count == 0


@pytest.mark.asyncio
async def test_contract_direction_status_and_department_scope_are_enforced() -> None:
    async with async_session_factory() as session:
        wrong_direction = await precheck_candidate(
            session,
            candidate=_candidate(order_type="PURCHASE"),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "CONTRACT_TYPE_MISMATCH" in {
            issue.code for issue in wrong_direction.issues
        }

        inactive = await precheck_candidate(
            session,
            candidate=_candidate(contract_id=INACTIVE_CONTRACT_ID),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "CONTRACT_INACTIVE" in {issue.code for issue in inactive.issues}
        assert not inactive.can_proceed_to_approval

        with pytest.raises(HTTPException) as denied:
            await precheck_candidate(
                session,
                candidate=_candidate(contract_id=SOUTH_CONTRACT_ID),
                actor=_actor(),
                business_date=date(2026, 10, 16),
            )
        assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_customer_project_and_contract_term_must_match() -> None:
    async with async_session_factory() as session:
        candidate = _candidate(delivery_date=date(2024, 12, 31))
        result = await precheck_candidate(
            session,
            candidate=candidate,
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        codes = {issue.code for issue in result.issues}
        assert "DELIVERY_DATE_IN_PAST" in codes
        assert "DELIVERY_OUTSIDE_CONTRACT_TERM" in codes

        mismatched = candidate.model_copy(
            update={"customer_id": UUID("00000000-0000-4000-8000-000000000011")}
        )
        result = await precheck_candidate(
            session,
            candidate=mismatched,
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "CONTRACT_CUSTOMER_MISMATCH" in {issue.code for issue in result.issues}


@pytest.mark.asyncio
async def test_units_prices_quantity_limits_and_material_ambiguity() -> None:
    async with async_session_factory() as session:
        wrong_unit = await precheck_candidate(
            session,
            candidate=_candidate(unit="箱"),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "MATERIAL_UNIT_MISMATCH" in {issue.code for issue in wrong_unit.issues}

        wrong_price = await precheck_candidate(
            session,
            candidate=_candidate(unit_price="3600"),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "CONTRACT_PRICE_MISMATCH" in {issue.code for issue in wrong_price.issues}

        missing_price = await precheck_candidate(
            session,
            candidate=_candidate(quantity="1", unit_price=None),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert missing_price.can_proceed_to_approval
        assert missing_price.candidate.items[0].unit_price == Decimal("3500.000000")
        price_source = next(
            evidence
            for evidence in missing_price.candidate.evidence
            if evidence.field_path == "items[0].unit_price"
        )
        assert price_source.source_type.value == "MASTER_DATA"

        zero_quantity = await precheck_candidate(
            session,
            candidate=_candidate(quantity="0"),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "INVALID_QUANTITY" in {issue.code for issue in zero_quantity.issues}

        incomplete = _candidate().model_copy(update={"contact": ""})
        incomplete_preview = await precheck_candidate(
            session,
            candidate=incomplete,
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert incomplete_preview.requested_amount == Decimal("220000.00")
        assert not incomplete_preview.can_proceed_to_approval

        too_many = await precheck_candidate(
            session,
            candidate=_candidate(quantity="101"),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "QUANTITY_LIMIT_EXCEEDED" in {issue.code for issue in too_many.issues}

        ambiguous = await precheck_candidate(
            session,
            candidate=_candidate(
                material_id=None, raw_name="HRB400E", specification="HRB400E"
            ),
            actor=_actor(),
            business_date=date(2026, 10, 16),
        )
        assert "MATERIAL_CONFIRMATION_REQUIRED" in {
            issue.code for issue in ambiguous.issues
        }
        assert {
            material.specification for material in ambiguous.material_candidates[0]
        } >= {
            "HRB400 12mm",
            "HRB400 16mm",
        }
        assert ambiguous.candidate.items[0].material_id is None
