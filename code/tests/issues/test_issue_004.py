"""验证订单候选、Decimal 金额、字段来源和草稿版本持久化。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.context import BUSINESS_OPERATOR, SYSTEM_ADMIN, AuthContext
from app.database import async_session_factory
from app.drafts.service import (
    DraftConflictError,
    content_hash,
    load_draft,
    save_candidate,
)
from app.drafts.types import (
    CandidateOrder,
    CandidateOrderItem,
    DraftValidationError,
    FieldEvidenceInput,
    FieldSource,
    candidate_amounts,
    line_amount,
    validate_candidate,
)
from app.models import Draft, DraftItem, DraftRevision, FieldEvidence

NORTH = UUID("00000000-0000-4000-8000-000000000001")
OPERATOR_ID = UUID("00000000-0000-4000-8000-000000000100")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000010")
PROJECT_ID = UUID("00000000-0000-4000-8000-000000000020")
MATERIAL_ID = UUID("00000000-0000-4000-8000-000000000030")
CONTRACT_ID = UUID("00000000-0000-4000-8000-000000000050")


def _operator() -> AuthContext:
    return AuthContext(
        user_id=OPERATOR_ID,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({NORTH}),
    )


def _candidate(
    *, quantity: str = "200", evidence: tuple[FieldEvidenceInput, ...] = ()
) -> CandidateOrder:
    return CandidateOrder(
        order_type="SALES",
        contract_id=CONTRACT_ID,
        customer_id=CUSTOMER_ID,
        project_id=PROJECT_ID,
        delivery_date=date(2026, 10, 20),
        address="北京市朝阳区示例工地",
        contact="张先生",
        items=(
            CandidateOrderItem(
                material_id=MATERIAL_ID,
                raw_name="螺纹钢",
                specification="HRB400 12mm",
                quantity=quantity,
                unit="吨",
                unit_price="3500",
            ),
        ),
        evidence=evidence,
    )


def test_candidate_and_validated_draft_are_separate_types() -> None:
    candidate = CandidateOrder(items=(CandidateOrderItem(raw_name="待识别物料"),))
    with pytest.raises(DraftValidationError) as error:
        validate_candidate(candidate)
    assert {issue.field for issue in error.value.issues} >= {
        "contract_id",
        "items[0].quantity",
    }

    complete = validate_candidate(_candidate())
    assert complete.total_amount == Decimal("700000.00")
    assert complete.currency == "CNY"
    assert not hasattr(complete, "approval_id")


def test_decimal_rounding_examples_and_precision_boundaries() -> None:
    candidate = CandidateOrder(
        items=(
            CandidateOrderItem(quantity="200", unit_price="3500"),
            CandidateOrderItem(quantity="180", unit_price="3500"),
        )
    )
    amounts, total = candidate_amounts(candidate)
    assert amounts == (Decimal("700000.00"), Decimal("630000.00"))
    assert total == Decimal("1330000.00")
    assert line_amount(Decimal(1), Decimal("0.005")) == Decimal("0.01")
    assert line_amount(Decimal(1), Decimal("0.004")) == Decimal("0.00")

    with pytest.raises(ValidationError, match="6 位小数"):
        CandidateOrderItem(quantity="1.0000001")
    with pytest.raises(ValidationError, match="十进制字符串"):
        CandidateOrderItem(quantity=0.1)
    with pytest.raises(ValidationError, match=r"numeric\(18,6\)"):
        CandidateOrderItem(unit_price="1000000000000")


def test_hash_is_canonical_for_business_values_and_excludes_evidence() -> None:
    first = _candidate(
        evidence=(
            FieldEvidenceInput(
                field_path="items[0].quantity",
                source_type=FieldSource.USER_TEXT,
                raw_value="200 吨",
                accepted_value="200",
            ),
        )
    )
    changed_evidence = _candidate(
        evidence=(
            FieldEvidenceInput(
                field_path="items[0].quantity",
                source_type=FieldSource.USER_EDIT,
                raw_value="200 吨",
                accepted_value="200",
            ),
        )
    )
    changed_business = _candidate(quantity="201")
    assert content_hash(first) == content_hash(changed_evidence)
    assert content_hash(_candidate(quantity="200.0")) == content_hash(
        _candidate(quantity="200")
    )
    assert content_hash(first) != content_hash(changed_business)


def test_client_cannot_supply_identity_or_approval_fields() -> None:
    with pytest.raises(ValidationError, match="approval_id"):
        CandidateOrder.model_validate({"approval_id": str(uuid4())})
    with pytest.raises(ValidationError, match="confirmed_by"):
        FieldEvidenceInput.model_validate(
            {
                "field_path": "address",
                "source_type": "USER_EDIT",
                "confirmed_by": str(OPERATOR_ID),
            }
        )
    with pytest.raises(ValidationError, match="approval_id"):
        CandidateOrder(business_fields={"extra": {"approval_id": "forged"}})


@pytest.mark.asyncio
async def test_save_load_versions_hash_conflict_and_source_provenance() -> None:
    draft_id = uuid4()
    evidence = FieldEvidenceInput(
        field_path="items[0].quantity",
        source_type=FieldSource.USER_EDIT,
        raw_value="200 吨",
        accepted_value="200",
        locator={"message": 3},
    )
    async with async_session_factory() as session, session.begin():
        saved = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=0,
            candidate=_candidate(evidence=(evidence,)),
            actor=_operator(),
        )
        assert saved.created and saved.version == 1 and saved.is_validated
        assert saved.total_amount == Decimal("700000.00")

        no_change = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=1,
            candidate=_candidate(),
            actor=_operator(),
        )
        assert not no_change.created and no_change.version == 1
        assert no_change.content_hash == saved.content_hash

        snapshot = await load_draft(session, draft_id)
        assert snapshot is not None and snapshot.version == 1
        assert snapshot.candidate is not None
        assert snapshot.candidate.items[0].quantity == Decimal("200.000000")
        assert snapshot.candidate.evidence[0].source_type == FieldSource.USER_EDIT
        stored_revision = await session.get(DraftRevision, (draft_id, 1))
        assert stored_revision is not None and stored_revision.changed_by == OPERATOR_ID
        stored_evidence = await session.scalar(
            select(FieldEvidence).where(FieldEvidence.draft_id == draft_id)
        )
        assert (
            stored_evidence is not None and stored_evidence.confirmed_by == OPERATOR_ID
        )
        assert stored_evidence.raw_value == "200 吨"
        stored_item = await session.get(DraftItem, (draft_id, 1, 1))
        assert stored_item is not None and stored_item.amount == Decimal("700000.00")

        updated = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=1,
            candidate=_candidate(quantity="180"),
            actor=_operator(),
        )
        assert updated.version == 2 and updated.content_hash != saved.content_hash
        assert updated.total_amount == Decimal("630000.00")
        old = await load_draft(session, draft_id, version=1)
        current = await load_draft(session, draft_id)
        assert old is not None and old.candidate is not None
        assert current is not None and current.candidate is not None
        assert old.candidate.items[0].quantity == Decimal("200.000000")
        assert current.candidate.items[0].quantity == Decimal("180.000000")

        with pytest.raises(DraftConflictError) as conflict:
            await save_candidate(
                session,
                draft_id=draft_id,
                expected_version=1,
                candidate=_candidate(quantity="190"),
                actor=_operator(),
            )
        assert conflict.value.code == "DRAFT_VERSION_CONFLICT"
        header = await session.get(Draft, draft_id)
        assert header is not None and header.current_version == 2


@pytest.mark.asyncio
async def test_incomplete_candidate_is_persisted_but_never_validated() -> None:
    draft_id = uuid4()
    async with async_session_factory() as session, session.begin():
        saved = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=0,
            candidate=CandidateOrder(
                items=(CandidateOrderItem(raw_name="未确认物料"),)
            ),
            actor=_operator(),
        )
        assert saved.version == 1 and not saved.is_validated
        assert saved.total_amount is None
        assert "MISSING_FIELD" in {issue.code for issue in saved.issues}
        loaded = await load_draft(session, draft_id)
        assert loaded is not None and loaded.candidate is not None
        assert not loaded.is_validated
        revision = await session.get(DraftRevision, (draft_id, 1))
        assert revision is not None and revision.validation["valid"] is False
        header = await session.get(Draft, draft_id)
        assert header is not None
        header.lock_state = "SUBMITTED"
        await session.flush()
        with pytest.raises(DraftConflictError) as locked:
            await save_candidate(
                session,
                draft_id=draft_id,
                expected_version=1,
                candidate=CandidateOrder(),
                actor=_operator(),
            )
        assert locked.value.code == "DRAFT_LOCKED"


@pytest.mark.asyncio
async def test_non_operator_cannot_save_drafts() -> None:
    administrator = AuthContext(
        user_id=UUID("00000000-0000-4000-8000-000000000103"),
        login="demo-admin",
        roles=frozenset({SYSTEM_ADMIN}),
        department_ids=frozenset({NORTH}),
    )
    async with async_session_factory() as session:
        with pytest.raises(HTTPException) as forbidden:
            await save_candidate(
                session,
                draft_id=uuid4(),
                expected_version=0,
                candidate=CandidateOrder(),
                actor=administrator,
            )
        assert forbidden.value.status_code == 403
