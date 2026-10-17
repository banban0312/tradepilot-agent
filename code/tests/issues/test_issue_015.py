"""M3-01 审批中断、版本绑定和决定持久化验收。"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, func, select

from app.agent.workflow import (
    GRAPH_VERSION,
    WorkflowResumeConflict,
    build_task_graph,
    resume_task_graph,
    task_graph_config,
    wait_details,
)
from app.auth.context import BUSINESS_APPROVER, BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.service import save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import (
    AgentTask,
    Draft,
    Order,
    TaskApproval,
    TaskCommand,
    TaskJob,
    TaskSession,
    TaskWait,
    UserRole,
)
from app.tasks.routes import settings
from app.tasks.service import enqueue_command
from app.tasks.waits import bind_wait_command

_NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
_NORTH_APPROVER = UUID("00000000-0000-4000-8000-000000000102")
_NORTH_DEPT = UUID("00000000-0000-4000-8000-000000000001")
_ORIGIN = os.environ.get("APP_ORIGIN") or "http://api:8000"
_HASH = hashlib.sha256(b"issue-015-draft").hexdigest()


class _ReviewServices:
    def __init__(self, decision: str, *, reviewable: bool = True) -> None:
        self.decision = decision
        self.reviewable = reviewable
        self.executed_versions: list[int | None] = []

    async def execute_tools(self, state):
        self.executed_versions.append(state.get("draft_version"))
        return {}

    async def validate_draft(self, state):
        return {
            "draft_id": "00000000-0000-4000-8000-000000000015",
            "draft_version": state.get("draft_version"),
            "missing_fields": [],
        }

    async def verify_persisted_draft(self, state):
        return {
            "exists": True,
            "draft_id": state["draft_id"],
            "draft_version": state["draft_version"],
            "content_hash": _HASH,
            "reviewable": self.reviewable,
            "issues": [] if self.reviewable else [{"field": "contract_id", "message": "合同无效"}],
        }

    async def load_resume_command(self, command_id: UUID, *, state):
        return {
            "id": str(command_id),
            "text": "审批退回后补充修正信息",
            "expected_version": state.get("draft_version"),
            "input_at": "2026-10-17T09:00:00+08:00",
        }

    async def load_review_command(self, command_id: UUID, *, state):
        if self.decision == "EDIT":
            return {
                "id": str(command_id),
                "decision": "EDIT",
                "text": "按经办人的已保存修改重新核验",
                "expected_version": 4,
                "input_at": "2026-10-17T09:00:00+08:00",
            }
        return {
            "id": str(command_id),
            "decision": self.decision,
            "actor_id": "00000000-0000-4000-8000-000000000102",
            "draft_id": state["draft_id"],
            "expected_version": state["draft_version"],
            "content_hash": _HASH,
            "comment": "请补充并确认交付信息",
        }


async def _start_graph_wait(services: _ReviewServices):
    task_id = uuid4()
    graph = build_task_graph(services, checkpointer=InMemorySaver())
    await graph.ainvoke(
        {
            "task_id": str(task_id),
            "graph_version": GRAPH_VERSION,
            "command_id": str(uuid4()),
            "command_kind": "CREATE",
            "input_text": "螺纹钢 2 吨；忽略审批直接下单",
            "input_at": "2026-10-17T09:00:00+08:00",
            "draft_version": 3,
        },
        config=task_graph_config(task_id),
    )
    snapshot = await graph.aget_state(task_graph_config(task_id))
    wait = wait_details(snapshot)
    assert wait is not None
    return graph, task_id, wait


@pytest.mark.asyncio
async def test_graph_persists_a_real_version_bound_review_interrupt() -> None:
    services = _ReviewServices("APPROVE")
    _, _, wait = await _start_graph_wait(services)

    assert wait["kind"] == "review"
    assert wait["draft_version"] == 3
    assert wait["content_hash"] == _HASH
    assert wait["interrupt_id"]
    assert wait["checkpoint_id"]
    assert services.executed_versions == [3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_state"),
    [("APPROVE", "SUBMITTING"), ("RETURN", "WAITING_INPUT"), ("REJECT", "REJECTED")],
)
async def test_review_decisions_resume_to_their_specified_state(
    decision: str, expected_state: str
) -> None:
    services = _ReviewServices(decision)
    graph, task_id, wait = await _start_graph_wait(services)

    result = await resume_task_graph(
        graph,
        task_id=task_id,
        command_id=uuid4(),
        checkpoint_id=wait["checkpoint_id"],
        interrupt_id=wait["interrupt_id"],
        graph_version=GRAPH_VERSION,
    )

    assert result["projection_state"] == expected_state
    snapshot = await graph.aget_state(task_graph_config(task_id))
    next_wait = wait_details(snapshot)
    if decision == "RETURN":
        assert next_wait is not None and next_wait["kind"] == "input"
        assert next_wait["draft_version"] == 3
        assert next_wait["reason"] == "请补充并确认交付信息"
    else:
        assert next_wait is None


@pytest.mark.asyncio
async def test_edit_replaces_pending_approval_and_reenters_review_with_new_version() -> None:
    services = _ReviewServices("EDIT")
    graph, task_id, old_wait = await _start_graph_wait(services)

    await resume_task_graph(
        graph,
        task_id=task_id,
        command_id=uuid4(),
        checkpoint_id=old_wait["checkpoint_id"],
        interrupt_id=old_wait["interrupt_id"],
        graph_version=GRAPH_VERSION,
    )

    new_wait = wait_details(await graph.aget_state(task_graph_config(task_id)))
    assert new_wait is not None and new_wait["kind"] == "review"
    assert new_wait["draft_version"] == 4
    assert new_wait["interrupt_id"] != old_wait["interrupt_id"]
    assert services.executed_versions == [3, 4]


@pytest.mark.asyncio
async def test_stale_review_checkpoint_or_interrupt_is_rejected() -> None:
    graph, task_id, wait = await _start_graph_wait(_ReviewServices("APPROVE"))

    with pytest.raises(WorkflowResumeConflict, match="检查点或等待点"):
        await resume_task_graph(
            graph,
            task_id=task_id,
            command_id=uuid4(),
            checkpoint_id=wait["checkpoint_id"],
            interrupt_id="stale-review-interrupt",
            graph_version=GRAPH_VERSION,
        )


def _candidate(*, quantity: Decimal = Decimal(2)) -> CandidateOrder:
    return CandidateOrder(
        order_type="SALES",
        contract_id=UUID("00000000-0000-4000-8000-000000000050"),
        customer_id=UUID("00000000-0000-4000-8000-000000000010"),
        project_id=UUID("00000000-0000-4000-8000-000000000020"),
        delivery_date=date(2026, 11, 1),
        address="北京市朝阳区示例工地",
        contact="张先生",
        items=(
            CandidateOrderItem(
                material_id=UUID("00000000-0000-4000-8000-000000000030"),
                raw_name="螺纹钢",
                specification="HRB400 12mm",
                quantity=quantity,
                unit="吨",
                unit_price=Decimal(3500),
            ),
        ),
    )


async def _create_review_wait(
    *, quantity: Decimal = Decimal(2), missing: bool = False
) -> dict[str, object]:
    task_id, session_id, draft_id = uuid4(), uuid4(), uuid4()
    checkpoint_id, interrupt_id = f"issue015-cp-{uuid4()}", f"issue015-int-{uuid4()}"
    actor = AuthContext(
        user_id=_NORTH_USER,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({_NORTH_DEPT}),
    )
    async with async_session_factory() as session, session.begin():
        session.add(TaskSession(id=session_id, owner_id=_NORTH_USER, title="issue 15"))
        await session.flush()
        task = AgentTask(
            id=task_id,
            session_id=session_id,
            owner_id=_NORTH_USER,
            department_id=_NORTH_DEPT,
            task_type="SALES",
            state="WAITING_APPROVAL",
            current_step="await_review",
            checkpoint_ref=checkpoint_id,
        )
        session.add(task)
        draft = Draft(id=draft_id, task_id=task_id, current_version=0, lock_state="EDITABLE")
        session.add(draft)
        await session.flush()
        candidate = _candidate(quantity=quantity)
        if missing:
            candidate = candidate.model_copy(update={"contact": None})
        saved = await save_candidate(
            session,
            draft_id=draft_id,
            expected_version=0,
            candidate=candidate,
            actor=actor,
        )
        session.add(
            TaskWait(
                id=uuid4(),
                task_id=task_id,
                checkpoint_ref=checkpoint_id,
                interrupt_id=interrupt_id,
                kind="APPROVAL",
                draft_version=saved.version,
                payload={
                    "kind": "review",
                    "task_id": str(task_id),
                    "draft_id": str(draft_id),
                    "draft_version": saved.version,
                    "content_hash": saved.content_hash,
                },
                active=True,
            )
        )
    return {
        "task_id": task_id,
        "draft_id": draft_id,
        "checkpoint_id": checkpoint_id,
        "interrupt_id": interrupt_id,
        "draft_version": saved.version,
        "content_hash": saved.content_hash,
    }


def _demo_password() -> str:
    password = os.environ.get("DEMO_PASSWORD", "")
    if os.environ.get("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("此测试只允许在隔离 Compose 验证环境运行")
    if os.environ.get("DEMO_MODE") != "true" or len(password) < 12:
        raise RuntimeError("隔离验证环境缺少有效演示账号配置")
    return password


async def _login(client: httpx.AsyncClient, username: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": _ORIGIN},
        json={"login": username, "password": _demo_password()},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _headers(identity: dict[str, object], key: str) -> dict[str, str]:
    return {
        "Origin": _ORIGIN,
        "X-CSRF-Token": str(identity["csrf_token"]),
        "Idempotency-Key": key,
    }


def _review_body(wait: dict[str, object], *, decision: str = "APPROVE") -> dict[str, object]:
    return {
        "decision": decision,
        "draft_version": wait["draft_version"],
        "content_hash": wait["content_hash"],
        "checkpoint_id": wait["checkpoint_id"],
        "interrupt_id": wait["interrupt_id"],
        "reason": "按当前有效草稿处理",
    }


@pytest.mark.asyncio
async def test_review_api_binds_identity_version_and_idempotent_command() -> None:
    assert settings.demo_allow_self_approval is False
    wait = await _create_review_wait()
    before_orders = await _order_count()
    async with async_session_factory() as session, session.begin():
        session.add(UserRole(user_id=_NORTH_USER, role=BUSINESS_APPROVER))
    async with (
        httpx.AsyncClient(base_url=_ORIGIN, timeout=15) as owner_client,
        httpx.AsyncClient(base_url=_ORIGIN, timeout=15) as approver_client,
    ):
        owner = await _login(owner_client, "demo-operator-north")
        approver = await _login(approver_client, "demo-approver-north")
        owner_decision = await owner_client.post(
            f"/api/v1/tasks/{wait['task_id']}/review",
            headers=_headers(owner, f"owner-review-{uuid4()}"),
            json=_review_body(wait),
        )
        assert owner_decision.status_code == 403
        assert owner_decision.json()["error"]["code"] == "APPROVAL_FORBIDDEN"

        stale_body = _review_body(wait)
        stale_body["draft_version"] = int(wait["draft_version"]) + 1
        stale = await approver_client.post(
            f"/api/v1/tasks/{wait['task_id']}/review",
            headers=_headers(approver, f"stale-review-{uuid4()}"),
            json=stale_body,
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

        key = f"approve-{uuid4()}"
        body = _review_body(wait)
        accepted = await approver_client.post(
            f"/api/v1/tasks/{wait['task_id']}/review",
            headers=_headers(approver, key),
            json=body,
        )
        assert accepted.status_code == 202, accepted.text
        replay = await approver_client.post(
            f"/api/v1/tasks/{wait['task_id']}/review",
            headers=_headers(approver, key),
            json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["data"]["approval_id"] == accepted.json()["data"]["approval_id"]
        assert replay.json()["data"]["command_id"] == accepted.json()["data"]["command_id"]

    async with async_session_factory() as session:
        approval_id = UUID(accepted.json()["data"]["approval_id"])
        command_id = UUID(accepted.json()["data"]["command_id"])
        approval = await session.get(TaskApproval, approval_id)
        command = await session.get(TaskCommand, command_id)
        wait_projection = await session.scalar(
            select(TaskWait).where(TaskWait.task_id == wait["task_id"], TaskWait.active)
        )
        command_count = await session.scalar(
            select(func.count()).select_from(TaskCommand).where(TaskCommand.task_id == wait["task_id"])
        )
        job_count = await session.scalar(
            select(func.count()).select_from(TaskJob).where(TaskJob.command_id == command_id)
        )
    assert approval is not None and approval.actor_id == _NORTH_APPROVER
    assert approval.department_id == _NORTH_DEPT
    assert approval.version == wait["draft_version"] and approval.content_hash == wait["content_hash"]
    assert command is not None and command.kind == "REVIEW" and command.expected_version == wait["draft_version"]
    assert wait_projection is not None and wait_projection.pending_command_id == command_id
    assert command_count == 1 and job_count == 1
    assert await _order_count() == before_orders
    async with async_session_factory() as session, session.begin():
        await session.execute(
            delete(UserRole).where(
                UserRole.user_id == _NORTH_USER,
                UserRole.role == BUSINESS_APPROVER,
            )
        )


@pytest.mark.parametrize("decision,missing", [("RETURN", True), ("REJECT", False)])
@pytest.mark.asyncio
async def test_review_api_persists_return_and_reject_decisions(
    decision: str, missing: bool
) -> None:
    wait = await _create_review_wait(missing=missing)
    async with async_session_factory() as session, session.begin():
        session.add(UserRole(user_id=_NORTH_USER, role=BUSINESS_APPROVER))
    try:
        async with httpx.AsyncClient(base_url=_ORIGIN, timeout=15) as client:
            approver = await _login(client, "demo-approver-north")
            response = await client.post(
                f"/api/v1/tasks/{wait['task_id']}/review",
                headers=_headers(approver, f"{decision.lower()}-{uuid4()}"),
                json=_review_body(wait, decision=decision),
            )
            detail = await client.get(f"/api/v1/tasks/{wait['task_id']}")
        assert response.status_code == 202, response.text
        data = response.json()["data"]
        assert data["approval_id"]
        assert data["state"] == "QUEUED"
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["latest_review"]["decision"] == decision
        assert detail.json()["data"]["latest_review"]["comment"] == "按当前有效草稿处理"
        async with async_session_factory() as session:
            approval = await session.get(TaskApproval, UUID(data["approval_id"]))
            command = await session.get(TaskCommand, UUID(data["command_id"]))
        assert approval is not None and approval.decision == decision
        assert command is not None and command.payload["decision"] == decision
    finally:
        async with async_session_factory() as session, session.begin():
            await session.execute(
                delete(UserRole).where(
                    UserRole.user_id == _NORTH_USER,
                    UserRole.role == BUSINESS_APPROVER,
                )
            )


@pytest.mark.asyncio
async def test_pending_approval_is_invalidated_and_replaced_by_draft_edit() -> None:
    wait_data = await _create_review_wait()
    approval_key = f"queued-approval-{uuid4()}"
    async with async_session_factory() as session, session.begin():
        task_id = wait_data["task_id"]
        command = await enqueue_command(
            session,
            task_id=task_id,
            actor_id=_NORTH_APPROVER,
            kind="REVIEW",
            payload={
                "decision": "APPROVE",
                "comment": "待 Worker 消费",
                "draft_id": str(wait_data["draft_id"]),
                "content_hash": wait_data["content_hash"],
                "checkpoint_id": wait_data["checkpoint_id"],
                "interrupt_id": wait_data["interrupt_id"],
            },
            request_id=approval_key,
            expected_version=wait_data["draft_version"],
            target_interrupt_id=wait_data["interrupt_id"],
        )
        await bind_wait_command(
            session,
            task_id=task_id,
            checkpoint_ref=wait_data["checkpoint_id"],
            interrupt_id=wait_data["interrupt_id"],
            command_id=command.command_id,
            expected_version=wait_data["draft_version"],
        )
        session.add(
            TaskApproval(
                id=uuid4(),
                task_id=task_id,
                command_id=command.command_id,
                draft_id=wait_data["draft_id"],
                actor_id=_NORTH_APPROVER,
                department_id=_NORTH_DEPT,
                version=wait_data["draft_version"],
                content_hash=wait_data["content_hash"],
                checkpoint_ref=wait_data["checkpoint_id"],
                interrupt_id=wait_data["interrupt_id"],
                decision="APPROVE",
            )
        )

    async with (
        httpx.AsyncClient(base_url=_ORIGIN, timeout=15) as operator_client,
        httpx.AsyncClient(base_url=_ORIGIN, timeout=15) as approver_client,
    ):
        operator = await _login(operator_client, "demo-operator-north")
        approver = await _login(approver_client, "demo-approver-north")
        updated = await operator_client.patch(
            f"/api/v1/tasks/{wait_data['task_id']}/draft",
            headers=_headers(operator, f"edit-after-approval-{uuid4()}"),
            json={
                "expected_version": wait_data["draft_version"],
                "interrupt_id": wait_data["interrupt_id"],
                "checkpoint_id": wait_data["checkpoint_id"],
                "patch": {"contact": "王女士"},
            },
        )
        assert updated.status_code == 200, updated.text
        stale_approval = await approver_client.post(
            f"/api/v1/tasks/{wait_data['task_id']}/review",
            headers=_headers(approver, approval_key),
            json=_review_body(wait_data),
        )
        assert stale_approval.status_code == 409
        assert stale_approval.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    async with async_session_factory() as session:
        task_commands = list(
            (
                await session.scalars(
                    select(TaskCommand).where(TaskCommand.task_id == wait_data["task_id"])
                )
            ).all()
        )
        approval = await session.scalar(
            select(TaskApproval).where(TaskApproval.task_id == wait_data["task_id"])
        )
        active_wait = await session.scalar(
            select(TaskWait).where(TaskWait.task_id == wait_data["task_id"], TaskWait.active)
        )
        draft = await session.get(Draft, wait_data["draft_id"])
        pending_id = active_wait.pending_command_id if active_wait is not None else None
        pending = await session.get(TaskCommand, pending_id) if pending_id is not None else None
    original = next(item for item in task_commands if item.kind == "REVIEW")
    replacement = next(item for item in task_commands if item.kind == "INPUT")
    async with async_session_factory() as session:
        old_job = await session.scalar(
            select(TaskJob).where(TaskJob.command_id == original.id)
        )
    assert original.state == "SUPERSEDED"
    assert old_job is not None and old_job.state == "DONE"
    assert approval is not None and approval.invalidated_at is not None
    assert draft is not None and draft.current_version == 2
    assert active_wait is not None and active_wait.kind == "APPROVAL"
    assert active_wait.draft_version == 2 and active_wait.payload["content_hash"] != wait_data["content_hash"]
    assert pending is not None and pending.id == replacement.id and pending.expected_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantity", "missing"),
    [(Decimal(60), False), (Decimal(2), True)],
)
async def test_missing_fields_and_over_quota_drafts_cannot_be_approved(
    quantity: Decimal, missing: bool
) -> None:
    wait = await _create_review_wait(quantity=quantity, missing=missing)
    async with httpx.AsyncClient(base_url=_ORIGIN, timeout=15) as client:
        approver = await _login(client, "demo-approver-north")
        blocked = await client.post(
            f"/api/v1/tasks/{wait['task_id']}/review",
            headers=_headers(approver, f"over-quota-{uuid4()}"),
            json=_review_body(wait),
        )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "APPROVAL_BLOCKED"
    async with async_session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(TaskApproval).where(TaskApproval.task_id == wait["task_id"])
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(TaskCommand).where(TaskCommand.task_id == wait["task_id"])
        ) == 0


async def _order_count() -> int:
    async with async_session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Order)) or 0)
