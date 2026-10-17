"""任务 API 与 SSE 的真实 HTTP、PostgreSQL 集成验收。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, func, select

from app.agent.workflow import (
    GRAPH_VERSION,
    build_task_graph,
    resume_task_graph,
    task_graph_config,
    wait_details,
)
from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.database import async_session_factory
from app.drafts.service import load_draft, save_candidate
from app.drafts.types import CandidateOrder, CandidateOrderItem
from app.models import (
    AgentTask,
    Draft,
    Order,
    TaskCommand,
    TaskEvent,
    TaskEventCursor,
    TaskJob,
    TaskWait,
)
from app.observability.service import append_task_event, finish_span, start_span
from app.tasks.routes import _decode_cursor, _encode_cursor, _sse
from app.worker.workflow import DatabaseWorkflowServices

_NORTH_USER = UUID("00000000-0000-4000-8000-000000000100")
_NORTH_DEPT = UUID("00000000-0000-4000-8000-000000000001")
_PUBLIC_ORIGIN = os.environ.get("APP_ORIGIN") or "http://api:8000"


class _DraftPatchResumeServices:
    def __init__(self) -> None:
        self.validate_calls = 0
        self.executed_versions: list[int | None] = []
        self.executed_prompts: list[str] = []

    async def execute_tools(self, state):
        self.executed_versions.append(state.get("draft_version"))
        self.executed_prompts.append(state["parsed_input"])
        return {}

    async def validate_draft(self, state):
        self.validate_calls += 1
        return {
            "draft_id": "00000000-0000-4000-8000-000000000014",
            "draft_version": state.get("draft_version"),
            "missing_fields": (
                [{"field": "items[0].specification", "message": "请补充物料规格"}]
                if self.validate_calls == 1
                else []
            ),
        }

    async def verify_persisted_draft(self, state):
        return {
            "exists": True,
            "draft_id": state["draft_id"],
            "draft_version": state["draft_version"],
            "content_hash": "a" * 64,
            "reviewable": True,
        }

    async def load_resume_command(self, command_id: UUID, *, state):
        return {
            "id": str(command_id),
            "text": "按已保存的草稿字段重新校验",
            "expected_version": 5,
            "input_at": "2026-10-17T09:00:00+08:00",
        }


def _demo_password() -> str:
    password = os.environ.get("DEMO_PASSWORD", "")
    if os.environ.get("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("此集成测试只能在隔离 Compose 验证环境中运行")
    if os.environ.get("DEMO_MODE") != "true" or len(password) < 12:
        raise RuntimeError("隔离验证环境必须显式设置有效的演示账号密码")
    return password


async def _login(client: httpx.AsyncClient, username: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": _PUBLIC_ORIGIN},
        json={"login": username, "password": _demo_password()},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _create_task(
    client: httpx.AsyncClient,
    csrf: str,
    *,
    key: str | None = None,
    order_type: str = "SALES",
) -> tuple[dict[str, object], str]:
    idempotency_key = key or f"issue014-{uuid4()}"
    response = await client.post(
        "/api/v1/tasks",
        headers={
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": idempotency_key,
        },
        json={"order_type": order_type, "input": {"text": "螺纹钢 2 吨"}},
    )
    assert response.status_code == 202, response.text
    return response.json()["data"], idempotency_key


def _candidate() -> CandidateOrder:
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
                quantity=Decimal(2),
                unit="吨",
                unit_price=Decimal(3500),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_create_list_detail_idempotency_department_scope_and_error_envelope() -> (
    None
):
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=15) as north:
        identity = await _login(north, "demo-operator-north")
        csrf = identity["csrf_token"]
        before_orders = await _order_count()
        result, key = await _create_task(north, csrf)

        assert result["state"] == "QUEUED"
        assert result["task_id"] and result["command_id"]
        assert result["status_url"] == f"/api/v1/tasks/{result['task_id']}"
        assert "order_id" not in result and "order_ref" not in result

        replay = await north.post(
            "/api/v1/tasks",
            headers={
                "Origin": _PUBLIC_ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": key,
            },
            json={"order_type": "SALES", "input": {"text": "螺纹钢 2 吨"}},
        )
        assert replay.status_code == 202
        assert replay.json()["data"]["task_id"] == result["task_id"]
        assert replay.json()["data"]["command_id"] == result["command_id"]

        changed_type = await north.post(
            "/api/v1/tasks",
            headers={
                "Origin": _PUBLIC_ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": key,
            },
            json={"order_type": "PURCHASE", "input": {"text": "螺纹钢 2 吨"}},
        )
        assert changed_type.status_code == 409
        assert changed_type.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        invalid = await north.post(
            "/api/v1/tasks",
            headers={
                "Origin": _PUBLIC_ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"issue014-invalid-{uuid4()}",
                "X-Request-ID": "00000000-0000-4000-8000-000000000014",
            },
            json={"order_type": "NOT_A_TYPE", "input": {"text": "secret-marker"}},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "INPUT_INVALID"
        assert invalid.json()["request_id"] == "00000000-0000-4000-8000-000000000014"
        assert "secret-marker" not in invalid.text

        detail = await north.get(f"/api/v1/tasks/{result['task_id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["state"] == "QUEUED"
        assert detail.json()["data"]["order_ref"] is None

        async with httpx.AsyncClient(
            base_url="http://api:8000", timeout=15
        ) as approver:
            approver_identity = await _login(approver, "demo-approver-north")
            readable = await approver.get(f"/api/v1/tasks/{result['task_id']}")
            assert readable.status_code == 200
            denied_write = await approver.post(
                f"/api/v1/tasks/{result['task_id']}/inputs",
                headers={
                    "Origin": _PUBLIC_ORIGIN,
                    "X-CSRF-Token": approver_identity["csrf_token"],
                    "Idempotency-Key": f"approver-write-{uuid4()}",
                },
                json={
                    "interrupt_id": "not-used",
                    "checkpoint_id": "not-used",
                    "text": "不得由审批角色修改任务",
                },
            )
            assert denied_write.status_code == 403
            assert denied_write.json()["error"]["code"] == "ACCESS_DENIED"

        second, _ = await _create_task(north, csrf)
        listing = await north.get("/api/v1/tasks", params={"limit": 1})
        assert listing.status_code == 200
        first_page = listing.json()["data"]
        assert len(first_page["items"]) == 1
        assert first_page["next_cursor"]
        listed_ids = [first_page["items"][0]["task_id"]]
        cursor = first_page["next_cursor"]
        for _ in range(100):
            if cursor is None:
                break
            next_page = await north.get(
                "/api/v1/tasks", params={"limit": 1, "cursor": cursor}
            )
            assert next_page.status_code == 200
            page = next_page.json()["data"]
            assert len(page["items"]) == 1
            listed_ids.extend(item["task_id"] for item in page["items"])
            cursor = page["next_cursor"]
        else:
            pytest.fail("任务列表分页未在 100 页内结束")
        assert len(listed_ids) == len(set(listed_ids))
        assert {result["task_id"], second["task_id"]} <= set(listed_ids)

        async with httpx.AsyncClient(base_url="http://api:8000", timeout=15) as south:
            await _login(south, "demo-operator-south")
            hidden = await south.get(f"/api/v1/tasks/{result['task_id']}")
            assert hidden.status_code == 404
            assert hidden.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    async with async_session_factory() as session:
        commands = list(
            (
                await session.scalars(
                    select(TaskCommand).where(
                        TaskCommand.task_id == UUID(result["task_id"])
                    )
                )
            ).all()
        )
        jobs = list(
            (
                await session.scalars(
                    select(TaskJob).where(TaskJob.task_id == UUID(result["task_id"]))
                )
            ).all()
        )
    assert len(commands) == len(jobs) == 1
    assert await _order_count() == before_orders


@pytest.mark.asyncio
async def test_input_wait_binding_replay_conflict_and_no_side_effect_on_stale_wait() -> (
    None
):
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=15) as client:
        identity = await _login(client, "demo-operator-north")
        result, _ = await _create_task(client, identity["csrf_token"])
        task_id = UUID(result["task_id"])
        checkpoint_id = f"issue014-checkpoint-{uuid4()}"
        interrupt_id = f"issue014-interrupt-{uuid4()}"
        async with async_session_factory() as session, session.begin():
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.state = "WAITING_INPUT"
            task.checkpoint_ref = checkpoint_id
            task.current_step = "await_input"
            session.add(
                TaskWait(
                    id=uuid4(),
                    task_id=task_id,
                    checkpoint_ref=checkpoint_id,
                    interrupt_id=interrupt_id,
                    kind="INPUT",
                    draft_version=4,
                    payload={"missing_fields": ["delivery_date"]},
                    active=True,
                )
            )

        headers = {
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": identity["csrf_token"],
        }
        empty = await client.post(
            f"/api/v1/tasks/{task_id}/inputs",
            headers={**headers, "Idempotency-Key": f"empty-{uuid4()}"},
            json={
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "text": "   ",
            },
        )
        assert empty.status_code == 422
        assert empty.json()["error"]["code"] == "INPUT_INVALID"

        stale = await client.post(
            f"/api/v1/tasks/{task_id}/inputs",
            headers={**headers, "Idempotency-Key": f"stale-{uuid4()}"},
            json={
                "interrupt_id": interrupt_id,
                "checkpoint_id": "old-checkpoint",
                "expected_draft_version": 4,
                "text": "2026 年 11 月 1 日交货",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "WAIT_CONFLICT"

        conflict = await client.post(
            f"/api/v1/tasks/{task_id}/inputs",
            headers={**headers, "Idempotency-Key": f"version-{uuid4()}"},
            json={
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "expected_draft_version": 3,
                "text": "2026 年 11 月 1 日交货",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"

        key = f"input-{uuid4()}"
        accepted = await client.post(
            f"/api/v1/tasks/{task_id}/inputs",
            headers={**headers, "Idempotency-Key": key},
            json={
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "text": "2026 年 11 月 1 日交货",
            },
        )
        assert accepted.status_code == 202
        assert accepted.json()["data"]["task_id"] == str(task_id)
        command_id = accepted.json()["data"]["command_id"]

        replay = await client.post(
            f"/api/v1/tasks/{task_id}/inputs",
            headers={**headers, "Idempotency-Key": key},
            json={
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "text": "2026 年 11 月 1 日交货",
            },
        )
        assert replay.status_code == 202
        assert replay.json()["data"]["command_id"] == command_id

        async with async_session_factory() as session:
            commands = list(
                (
                    await session.scalars(
                        select(TaskCommand).where(TaskCommand.task_id == task_id)
                    )
                ).all()
            )
            wait = await session.scalar(
                select(TaskWait).where(TaskWait.task_id == task_id, TaskWait.active)
            )
            events = list(
                (
                    await session.scalars(
                        select(TaskEvent).where(TaskEvent.task_id == task_id)
                    )
                ).all()
            )
        assert len(commands) == 2
        assert wait is not None and wait.pending_command_id == UUID(command_id)
        assert any(event.event_type == "task.status_changed" for event in events)

        services = DatabaseWorkflowServices.__new__(DatabaseWorkflowServices)
        services.session_factory = async_session_factory
        services.task = SimpleNamespace(id=task_id)
        resume_command = await services.load_resume_command(UUID(command_id), state={})
        assert resume_command["expected_version"] is None


@pytest.mark.asyncio
async def test_draft_patch_optimistic_version_contract_scope_and_idempotency() -> None:
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=20) as client:
        identity = await _login(client, "demo-operator-north")
        result, _ = await _create_task(client, identity["csrf_token"])
        task_id = UUID(result["task_id"])
        draft_id = uuid4()
        checkpoint_id = f"issue014-draft-checkpoint-{uuid4()}"
        interrupt_id = f"issue014-draft-interrupt-{uuid4()}"
        actor = AuthContext(
            user_id=_NORTH_USER,
            login="demo-operator-north",
            roles=frozenset({BUSINESS_OPERATOR}),
            department_ids=frozenset({_NORTH_DEPT}),
        )
        async with async_session_factory() as session, session.begin():
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.state = "WAITING_INPUT"
            task.checkpoint_ref = checkpoint_id
            task.current_step = "await_input"
            draft = Draft(
                id=draft_id,
                task_id=task_id,
                current_version=0,
                lock_state="EDITABLE",
            )
            session.add(draft)
            await session.flush()
            saved = await save_candidate(
                session,
                draft_id=draft_id,
                expected_version=0,
                candidate=_candidate(),
                actor=actor,
            )
            assert saved.version == 1
            session.add(
                TaskWait(
                    id=uuid4(),
                    task_id=task_id,
                    checkpoint_ref=checkpoint_id,
                    interrupt_id=interrupt_id,
                    kind="INPUT",
                    draft_version=1,
                    payload={"missing_fields": []},
                    active=True,
                )
            )

        headers = {
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": identity["csrf_token"],
        }
        forbidden_contract = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={**headers, "Idempotency-Key": f"foreign-contract-{uuid4()}"},
            json={
                "expected_version": 1,
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "patch": {"contract_id": "00000000-0000-4000-8000-000000000053"},
            },
        )
        assert forbidden_contract.status_code == 404

        async with async_session_factory() as session, session.begin():
            current = await load_draft(session, draft_id)
            assert current is not None and current.candidate is not None
            concurrent = await save_candidate(
                session,
                draft_id=draft_id,
                expected_version=1,
                candidate=current.candidate.model_copy(
                    update={"contact": "并发联系人"}
                ),
                actor=actor,
            )
            assert concurrent.version == 2

        conflict = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={**headers, "Idempotency-Key": f"old-version-{uuid4()}"},
            json={
                "expected_version": 1,
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "patch": {"contact": "王女士"},
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"

        async with async_session_factory() as session, session.begin():
            wait = await session.scalar(
                select(TaskWait).where(TaskWait.task_id == task_id, TaskWait.active)
            )
            assert wait is not None
            wait.draft_version = 2

        key = f"patch-{uuid4()}"
        body = {
            "expected_version": 2,
            "interrupt_id": interrupt_id,
            "checkpoint_id": checkpoint_id,
            "patch": {"contact": "王女士"},
        }
        updated = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={**headers, "Idempotency-Key": key},
            json=body,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["version"] == 3
        assert updated.json()["data"]["validated"] is True

        detail = await client.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        assert [item["text"] for item in detail.json()["data"]["inputs"]] == [
            "螺纹钢 2 吨"
        ]

        replay = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={**headers, "Idempotency-Key": key},
            json=body,
        )
        assert replay.status_code == 200
        assert replay.json()["data"] == updated.json()["data"]

        changed_replay = await client.patch(
            f"/api/v1/tasks/{task_id}/draft",
            headers={**headers, "Idempotency-Key": key},
            json={**body, "patch": {"contact": "其他联系人"}},
        )
        assert changed_replay.status_code == 409
        assert changed_replay.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        async with async_session_factory() as session:
            draft = await session.get(Draft, draft_id)
            commands = list(
                (
                    await session.scalars(
                        select(TaskCommand).where(
                            TaskCommand.task_id == task_id,
                            TaskCommand.kind == "INPUT",
                        )
                    )
                ).all()
            )
            events = list(
                (
                    await session.scalars(
                        select(TaskEvent).where(TaskEvent.task_id == task_id)
                    )
                ).all()
            )
        assert draft is not None and draft.current_version == 3
        assert len(commands) == 1
        assert any(event.event_type == "draft.updated" for event in events)


@pytest.mark.asyncio
async def test_sse_replay_and_expired_cursor_signal_do_not_execute_task_again() -> None:
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=15) as client:
        identity = await _login(client, "demo-operator-north")
        result, _ = await _create_task(client, identity["csrf_token"])
        task_id = UUID(result["task_id"])
        actor = AuthContext(
            user_id=_NORTH_USER,
            login="demo-operator-north",
            roles=frozenset({BUSINESS_OPERATOR}),
            department_ids=frozenset({_NORTH_DEPT}),
        )
        async with async_session_factory() as session, session.begin():
            span = await start_span(
                session,
                task_id=task_id,
                actor=actor,
                name="mcp.search_contracts",
                kind="TOOL",
                execution_mode="LIVE",
                protocol="MCP",
            )
            await finish_span(
                session,
                span_id=span.id,
                status="OK",
                duration_ms=12,
                output_ref={"result_count": 1},
            )
            task = await session.get(AgentTask, task_id)
            assert task is not None
            task.state = "FAILED"
            task.last_error = {"code": "EXAMPLE_FAILURE"}
            await append_task_event(
                session,
                task_id=task_id,
                event_type="task.failed",
                payload={"code": "EXAMPLE_FAILURE"},
            )

        replay = await client.get(
            f"/api/v1/tasks/{task_id}/events", params={"after_id": 0}
        )
        assert replay.status_code == 200
        assert replay.headers["content-type"].startswith("text/event-stream")
        assert "id: 1\n" in replay.text and "id: 5\n" in replay.text
        assert "event: tool.completed" in replay.text
        assert "event: task.failed" in replay.text
        assert "EXAMPLE_FAILURE" in replay.text

        async with async_session_factory() as session, session.begin():
            await session.execute(delete(TaskEvent).where(TaskEvent.task_id == task_id))
        expired = await client.get(
            f"/api/v1/tasks/{task_id}/events",
            headers={"Last-Event-ID": "0"},
        )
        assert expired.status_code == 200
        assert "event: task.resync_required" in expired.text
        assert '"reason":"cursor_expired"' in expired.text
        snapshot = await client.get(f"/api/v1/tasks/{task_id}")
        assert snapshot.status_code == 200
        assert snapshot.json()["data"]["state"] == "FAILED"

        async with async_session_factory() as session:
            commands = list(
                (
                    await session.scalars(
                        select(TaskCommand).where(TaskCommand.task_id == task_id)
                    )
                ).all()
            )
            cursor = await session.get(TaskEventCursor, task_id)
    assert len(commands) == 1
    assert cursor is not None and cursor.next_seq == 6


@pytest.mark.asyncio
async def test_draft_patch_resume_uses_command_version_instead_of_stale_checkpoint() -> (
    None
):
    task_id = uuid4()
    command_id = uuid4()
    services = _DraftPatchResumeServices()
    graph = build_task_graph(services, checkpointer=InMemorySaver())
    await graph.ainvoke(
        {
            "task_id": str(task_id),
            "graph_version": GRAPH_VERSION,
            "command_id": str(uuid4()),
            "command_kind": "CREATE",
            "input_text": "螺纹钢 2 吨",
            "input_at": "2026-10-17T09:00:00+08:00",
            "draft_version": 2,
        },
        config=task_graph_config(task_id),
    )
    wait = wait_details(await graph.aget_state(task_graph_config(task_id)))
    assert wait is not None and wait["draft_version"] == 2

    result = await resume_task_graph(
        graph,
        task_id=task_id,
        command_id=command_id,
        checkpoint_id=wait["checkpoint_id"],
        interrupt_id=wait["interrupt_id"],
        graph_version=GRAPH_VERSION,
    )

    assert result["draft_version"] == 5
    assert services.executed_versions == [2, 5]
    assert "当前草稿版本：5" in services.executed_prompts[-1]
    assert result["projection_state"] == "WAITING_APPROVAL"


async def _order_count() -> int:
    async with async_session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Order)) or 0)


def test_task_list_cursor_and_sse_frame_are_stable_and_safe() -> None:
    created_at = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
    task_id = uuid4()
    assert _decode_cursor(_encode_cursor(created_at, task_id)) == (created_at, task_id)

    frame = _sse(7, "unsafe event name", {"message": "草稿已更新"})
    assert frame.startswith("id: 7\nevent: unsafe_event_name\n")
    assert '"message":"草稿已更新"' in frame
