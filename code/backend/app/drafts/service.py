"""草稿版本持久化；调用方负责提交其外层业务事务。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import BUSINESS_OPERATOR, AuthContext, require_role
from app.models import (
    ContractLine,
    Draft,
    DraftItem,
    DraftRevision,
    FieldEvidence,
    TaskApproval,
)
from app.observability.service import append_task_event

from .types import (
    CandidateOrder,
    CandidateOrderItem,
    DraftValidationError,
    DraftValidationIssue,
    FieldEvidenceInput,
    FieldSource,
    candidate_amounts,
    validate_candidate,
)


class DraftConflictError(RuntimeError):
    """草稿版本已变化或草稿已锁定。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DraftSaveResult:
    draft_id: UUID
    version: int
    content_hash: str
    total_amount: Decimal | None
    is_validated: bool
    issues: tuple[DraftValidationIssue, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    draft_id: UUID
    version: int
    lock_state: str
    candidate: CandidateOrder | None
    content_hash: str | None
    total_amount: Decimal | None
    is_validated: bool
    issues: tuple[DraftValidationIssue, ...]


def _json_value(value: Any) -> Any:
    """把来源证据和扩展字段转换为可稳定序列化的 JSON 值。"""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, (UUID, date)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"不支持保存的 JSON 值类型：{type(value).__name__}")


def canonical_content(candidate: CandidateOrder) -> bytes:
    """只哈希业务内容；来源证据、操作者和界面状态不改变业务摘要。"""
    content = _json_value(candidate.model_dump(mode="python", exclude={"evidence"}))
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return encoded.encode("utf-8")


def content_hash(candidate: CandidateOrder) -> str:
    return hashlib.sha256(canonical_content(candidate)).hexdigest()


def _issues_from_validation(value: dict[str, Any]) -> tuple[DraftValidationIssue, ...]:
    return tuple(
        DraftValidationIssue.model_validate(item) for item in value.get("issues", [])
    )


async def save_candidate(
    session: AsyncSession,
    *,
    draft_id: UUID,
    expected_version: int,
    candidate: CandidateOrder,
    actor: AuthContext,
) -> DraftSaveResult:
    """按乐观版本保存候选；数据库事务提交由上层控制。"""
    require_role(actor, BUSINESS_OPERATOR)
    if expected_version < 0:
        raise ValueError("expected_version 不能小于 0")

    candidate = await _fill_contract_prices(session, candidate)

    # 先在同一事务中确保草稿头存在；并发首次保存由主键冲突安全串行化。
    await session.execute(
        pg_insert(Draft)
        .values(id=draft_id, current_version=0, lock_state="EDITABLE")
        .on_conflict_do_nothing(index_elements=[Draft.id])
    )
    draft = await session.scalar(
        select(Draft).where(Draft.id == draft_id).with_for_update()
    )
    if draft is None:
        raise DraftConflictError("DRAFT_NOT_FOUND", "草稿不存在")
    if draft.lock_state != "EDITABLE":
        raise DraftConflictError("DRAFT_LOCKED", "草稿当前不可编辑")
    if draft.current_version != expected_version:
        raise DraftConflictError(
            "DRAFT_VERSION_CONFLICT", "草稿版本已变化，请刷新后重试"
        )

    digest = content_hash(candidate)
    current = None
    if draft.current_version > 0:
        current = await session.get(DraftRevision, (draft.id, draft.current_version))
    if current is not None and current.content_hash == digest:
        validation = current.validation
        return DraftSaveResult(
            draft_id=draft.id,
            version=current.version,
            content_hash=current.content_hash,
            total_amount=current.total_amount,
            is_validated=bool(validation.get("valid")),
            issues=_issues_from_validation(validation),
            created=False,
        )

    try:
        validate_candidate(candidate)
        issues: tuple[DraftValidationIssue, ...] = ()
        is_validated = True
    except DraftValidationError as exc:
        issues = exc.issues
        is_validated = False

    amounts, total = candidate_amounts(candidate)
    next_version = draft.current_version + 1
    revision = DraftRevision(
        draft_id=draft.id,
        version=next_version,
        contract_id=candidate.contract_id,
        order_type=candidate.order_type,
        customer_id=candidate.customer_id,
        project_id=candidate.project_id,
        currency=candidate.currency,
        delivery_date=candidate.delivery_date,
        address=candidate.address,
        contact=candidate.contact,
        business_fields=_json_value(candidate.business_fields),
        total_amount=total,
        content_hash=digest,
        validation={
            "valid": is_validated,
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
        changed_by=actor.user_id,
    )
    session.add(revision)
    # 显式先写父版本，避免 ORM 在没有 relationship 映射时先 flush 复合外键子行。
    await session.flush()
    for line_no, (item, amount) in enumerate(
        zip(candidate.items, amounts, strict=True), start=1
    ):
        session.add(
            DraftItem(
                draft_id=draft.id,
                version=next_version,
                line_no=line_no,
                material_id=item.material_id,
                raw_name=item.raw_name,
                specification=item.specification,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=item.unit_price,
                amount=amount,
            )
        )
    for evidence in candidate.evidence:
        session.add(
            FieldEvidence(
                id=uuid4(),
                draft_id=draft.id,
                version=next_version,
                field_path=evidence.field_path,
                source_type=evidence.source_type.value,
                artifact_id=evidence.artifact_id,
                message_id=evidence.message_id,
                locator=_json_value(evidence.locator)
                if evidence.locator is not None
                else None,
                raw_value=_json_value(evidence.raw_value),
                accepted_value=_json_value(evidence.accepted_value),
                confirmed_by=actor.user_id
                if evidence.source_type.value == "USER_EDIT"
                else None,
            )
        )
    draft.current_version = next_version
    if draft.task_id is not None:
        # Any changed business content invalidates prior approvals in the same transaction.
        from datetime import UTC, datetime

        approvals = await session.scalars(
            select(TaskApproval)
            .where(
                TaskApproval.draft_id == draft.id,
                TaskApproval.decision == "APPROVE",
                TaskApproval.invalidated_at.is_(None),
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        for approval in approvals:
            approval.invalidated_at = now
    await session.flush()
    if draft.task_id is not None:
        await append_task_event(
            session,
            task_id=draft.task_id,
            event_type="draft.updated",
            payload={
                "draft_id": str(draft.id),
                "version": next_version,
                "content_hash": digest,
            },
        )
    return DraftSaveResult(
        draft_id=draft.id,
        version=next_version,
        content_hash=digest,
        total_amount=total,
        is_validated=is_validated,
        issues=issues,
        created=True,
    )


async def _fill_contract_prices(
    session: AsyncSession, candidate: CandidateOrder
) -> CandidateOrder:
    """缺少单价时只补合同明细价格，并留下由服务端生成的来源证据。"""
    if candidate.contract_id is None:
        return candidate

    items = list(candidate.items)
    evidence = list(candidate.evidence)
    changed = False
    for index, item in enumerate(items):
        if item.unit_price is not None or item.material_id is None or item.unit is None:
            continue
        line = await session.scalar(
            select(ContractLine).where(
                ContractLine.contract_id == candidate.contract_id,
                ContractLine.material_id == item.material_id,
                ContractLine.unit == item.unit,
            )
        )
        if line is None or line.unit_price is None:
            continue
        items[index] = item.model_copy(update={"unit_price": line.unit_price})
        field_path = f"items[{index}].unit_price"
        evidence = [entry for entry in evidence if entry.field_path != field_path]
        evidence.append(
            FieldEvidenceInput(
                field_path=field_path,
                source_type=FieldSource.MASTER_DATA,
                locator={"contract_line_id": str(line.id)},
                accepted_value=format(line.unit_price, "f"),
            )
        )
        changed = True
    if not changed:
        return candidate
    return candidate.model_copy(
        update={"items": tuple(items), "evidence": tuple(evidence)}
    )


async def load_draft(
    session: AsyncSession, draft_id: UUID, *, version: int | None = None
) -> DraftSnapshot | None:
    """读取指定版本或草稿头当前指向的不可变快照。"""
    draft = await session.get(Draft, draft_id)
    if draft is None:
        return None
    selected_version = version if version is not None else draft.current_version
    if selected_version == 0:
        if version not in (None, 0):
            return None
        return DraftSnapshot(draft_id, 0, draft.lock_state, None, None, None, False, ())
    revision = await session.get(DraftRevision, (draft_id, selected_version))
    if revision is None:
        return None
    rows = await session.scalars(
        select(DraftItem)
        .where(DraftItem.draft_id == draft_id, DraftItem.version == selected_version)
        .order_by(DraftItem.line_no)
    )
    items = tuple(
        CandidateOrderItem(
            material_id=row.material_id,
            raw_name=row.raw_name,
            specification=row.specification,
            quantity=row.quantity,
            unit=row.unit,
            unit_price=row.unit_price,
        )
        for row in rows
    )
    evidence_rows = await session.scalars(
        select(FieldEvidence)
        .where(
            FieldEvidence.draft_id == draft_id,
            FieldEvidence.version == selected_version,
        )
        .order_by(FieldEvidence.field_path, FieldEvidence.id)
    )
    evidence = tuple(
        FieldEvidenceInput(
            field_path=row.field_path,
            source_type=row.source_type,
            artifact_id=row.artifact_id,
            message_id=row.message_id,
            locator=row.locator,
            raw_value=row.raw_value,
            accepted_value=row.accepted_value,
        )
        for row in evidence_rows
    )
    candidate = CandidateOrder(
        order_type=revision.order_type,
        contract_id=revision.contract_id,
        customer_id=revision.customer_id,
        project_id=revision.project_id,
        currency=revision.currency,
        delivery_date=revision.delivery_date,
        address=revision.address,
        contact=revision.contact,
        business_fields=revision.business_fields,
        items=items,
        evidence=evidence,
    )
    validation = revision.validation
    return DraftSnapshot(
        draft_id=draft_id,
        version=selected_version,
        lock_state=draft.lock_state,
        candidate=candidate,
        content_hash=revision.content_hash,
        total_amount=revision.total_amount,
        is_validated=bool(validation.get("valid")),
        issues=_issues_from_validation(validation),
    )
