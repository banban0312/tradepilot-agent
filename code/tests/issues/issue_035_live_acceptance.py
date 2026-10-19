"""M5-07 在线联调：批次子任务、真实 DeepSeek/MCP、审批和订单事实。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select

sys.path.insert(0, "/app")

from test_issue_029 import _create_batch, _row, _upload, _workbook

from app.config import settings
from app.database import async_session_factory
from app.mcp_server.client import MCPToolClient
from app.mcp_server.contracts import MCPCallError, MCPUnavailable
from app.models import (
    AgentTask,
    AuditEvent,
    ConfigSnapshot,
    ImportBatch,
    ImportBatchItem,
    Order,
    QuotaOccupation,
    TaskApproval,
    TaskCommand,
    TaskEvent,
    TaskJob,
    TraceSpan,
    User,
)
from app.runtime_config import (
    config_for_snapshot,
    environment_config,
    new_snapshot,
)


def _require_live_environment() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("在线联调只允许运行在本脚本创建的隔离 Compose 环境")
    if os.getenv("TRADEPILOT_ONLINE_MODEL") != "1":
        raise RuntimeError("必须显式启用真实在线模型")
    if (
        settings.model_provider != "deepseek"
        or settings.model_configuration_state != "configured_unverified"
    ):
        raise RuntimeError("本机 DeepSeek 配置不完整；密钥值不会显示")
    if (
        not settings.mcp_context_signing_key
        or not settings.mcp_context_signing_key.get_secret_value()
    ):
        raise RuntimeError("隔离 MCP 上下文签名密钥未配置")


def _password() -> str:
    value = os.getenv("DEMO_PASSWORD", "")
    if len(value) < 12:
        raise RuntimeError("隔离演示密码未配置")
    return value


async def mcp_smoke() -> None:
    """回报异常类型与消息，不输出请求头或密钥。"""
    client = MCPToolClient(settings.mcp_url)
    try:
        tools = await client.list_tools()
    except (MCPCallError, MCPUnavailable) as exc:
        causes: list[str] = []
        current: BaseException | None = exc
        while current is not None and len(causes) < 5:
            causes.append(f"{type(current).__name__}: {current}")
            current = current.__cause__
        raise RuntimeError("MCP list_tools 诊断：" + " <- ".join(causes)) from exc
    names = sorted(getattr(tool, "name", "") for tool in tools)
    assert {
        "query_customer",
        "match_material",
        "get_contract",
        "check_contract_balance",
        "save_order_draft",
    } <= set(names)
    print("ISSUE035_MCP_LIST_TOOLS=verified")


async def prepare_live_queue() -> None:
    """清理回归残留队列和配置覆盖，恢复隔离环境的真实部署配置。"""
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("队列准备只允许运行在本脚本创建的隔离 Compose 环境")
    async with async_session_factory() as session, session.begin():
        jobs = list(
            (
                await session.scalars(
                    select(TaskJob)
                    .where(TaskJob.state.in_({"READY", "RUNNING"}))
                    .with_for_update()
                )
            ).all()
        )
        task_ids: set[UUID] = set()
        for job in jobs:
            job.state = "FAILED"
            job.worker_id = None
            job.heartbeat_at = None
            task_ids.add(job.task_id)
        for task_id in task_ids:
            task = await session.get(AgentTask, task_id, with_for_update=True)
            if task is not None and task.state in {"QUEUED", "RUNNING"}:
                task.state = "FAILED"
                task.last_error = {
                    "code": "TEST_FIXTURE_CLEANUP",
                    "message": "在线联调前隔离回归测试遗留作业",
                }
        snapshots = list(
            (
                await session.scalars(
                    select(ConfigSnapshot)
                    .where(ConfigSnapshot.is_current.is_(True))
                    .with_for_update()
                )
            ).all()
        )
        for snapshot in snapshots:
            snapshot.is_current = False
        if snapshots:
            await session.flush()
            latest_version = int(
                await session.scalar(select(func.max(ConfigSnapshot.version))) or 0
            )
            session.add(
                new_snapshot(
                    content=environment_config(),
                    version=latest_version + 1,
                    actor_id=None,
                )
            )
    print(f"ISSUE035_TEST_QUEUE_ISOLATED={len(jobs)}")
    print(f"ISSUE035_RUNTIME_CONFIG_RESET={len(snapshots)}")


async def _login(client: httpx.AsyncClient, login: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": settings.app_origin or "http://localhost:5173"},
        json={"login": login, "password": _password()},
    )
    assert response.status_code == 200, (
        f"隔离演示账号登录失败：HTTP {response.status_code}"
    )
    identity = response.json()["data"]
    client.headers.update(
        {
            "Origin": settings.app_origin or "http://localhost:5173",
            "X-CSRF-Token": identity["csrf_token"],
        }
    )
    return identity


async def _snapshot(client: httpx.AsyncClient, task_id: UUID) -> dict[str, object]:
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200, f"读取原任务失败：HTTP {response.status_code}"
    return response.json()["data"]


async def _wait_state(
    client: httpx.AsyncClient, task_id: UUID, expected: str, timeout: int = 360
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = await _snapshot(client, task_id)
        if latest.get("state") == expected:
            return latest
        if latest.get("state") in {"FAILED", "BLOCKED_RULE", "REJECTED", "COMPLETED"}:
            break
        await asyncio.sleep(1)
    raise AssertionError(
        f"任务未到达 {expected}，当前为 {latest.get('state')}，稳定错误={latest.get('last_error')!r}"
    )


async def create_input_wait() -> None:
    """创建真实模型待补充任务，用于双模式和检查点恢复验收。"""
    _require_live_environment()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30) as client:
        await _login(client, "demo-operator-north")
        response = await client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": f"issue035-input-{uuid4()}"},
            json={"order_type": "SALES", "input": {"text": "我想买一批螺纹钢。"}},
        )
        assert response.status_code == 202, (
            f"任务 API 未受理：HTTP {response.status_code}"
        )
        task_id = UUID(response.json()["data"]["task_id"])
        try:
            data = await _wait_state(client, task_id, "WAITING_INPUT")
        except AssertionError:
            async with async_session_factory() as session:
                spans = list(
                    (
                        await session.scalars(
                            select(TraceSpan)
                            .where(TraceSpan.task_id == task_id)
                            .order_by(TraceSpan.start_at, TraceSpan.id)
                        )
                    ).all()
                )
                task = await session.get(AgentTask, task_id)
                effective_url = settings.mcp_url
                if task is not None and task.config_snapshot_id is not None:
                    effective = await config_for_snapshot(
                        session, task.config_snapshot_id
                    )
                    effective_url = effective.mcp_url
                print(
                    "ISSUE035_FAILURE_SPANS="
                    + repr(
                        [
                            {
                                "name": span.name,
                                "status": span.status,
                                "error": span.error,
                            }
                            for span in spans
                        ]
                    )
                )
                print(
                    f"ISSUE035_EFFECTIVE_MCP_URL={effective_url}; "
                    f"ENV_MCP_URL={settings.mcp_url}"
                )
                try:
                    direct_tools = await MCPToolClient(effective_url).list_tools()
                    print(
                        "ISSUE035_DIRECT_MCP_AFTER_FAILURE="
                        + ",".join(sorted(tool.name for tool in direct_tools))
                    )
                except (MCPCallError, MCPUnavailable) as exc:
                    causes: list[str] = []
                    current: BaseException | None = exc
                    while current is not None and len(causes) < 5:
                        causes.append(f"{type(current).__name__}: {current}")
                        current = current.__cause__
                    print("ISSUE035_DIRECT_MCP_ERROR=" + " <- ".join(causes))
            raise
        wait = data.get("active_wait")
        assert isinstance(wait, dict) and wait.get("kind") == "INPUT"
        assert data.get("order_ref") is None
    print(f"ISSUE035_INPUT_TASK_ID={task_id}")
    print("ISSUE035_INPUT_WAIT=verified")


async def assert_input_wait(task_id: UUID) -> None:
    _require_live_environment()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30) as client:
        await _login(client, "demo-operator-north")
        data = await _snapshot(client, task_id)
        trace_response = await client.get(f"/api/v1/tasks/{task_id}/trace")
        assert trace_response.status_code == 200, trace_response.text
        trace = trace_response.json()["data"]
    wait = data.get("active_wait")
    assert data.get("state") == "WAITING_INPUT"
    assert isinstance(wait, dict) and wait.get("kind") == "INPUT"
    assert wait.get("checkpoint_id") and wait.get("interrupt_id")
    assert data.get("order_ref") is None
    assert trace["task_id"] == str(task_id)
    assert any(
        item["state"] == "WAITING_INPUT" and item["active"]
        for item in trace["metrics"]["manual_waits"]
    )
    print("ISSUE035_INPUT_WAIT_RECOVERY=verified")


async def create_batch() -> None:
    _require_live_environment()
    valid = _row("有效订单", "螺纹钢", 2)
    valid[8] = "HRB400 12mm"
    valid[11] = 3500
    over_quota = _row("超额订单", "螺纹钢", 101)
    over_quota[8] = "HRB400 12mm"
    over_quota[11] = 3500
    missing = _row("缺字段订单", "螺纹钢", 1)
    missing[8] = None

    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30) as client:
        await _login(client, "demo-operator-north")
        artifact_id = await _upload(client, _workbook([valid, over_quota, missing]))
        idempotency_key = f"issue035-live-{uuid4()}"
        created = await _create_batch(client, artifact_id, idempotency_key)
        assert created.status_code == 202, created.text
        response = created.json()
        batch = response["data"]
        batch_id = UUID(batch["batch_id"])
        groups = {group["group_id"]: group for group in batch["groups"]}
        assert batch["summary"]["task_count"] == 1
        assert groups["有效订单"]["task_id"]
        assert groups["超额订单"]["preview_status"] == "OVER_QUOTA_REVIEW"
        assert groups["超额订单"]["task_id"] is None
        assert groups["缺字段订单"]["preview_status"] == "NEEDS_CORRECTION"
        assert groups["缺字段订单"]["task_id"] is None
        replay = await _create_batch(client, artifact_id, idempotency_key)
        assert replay.status_code == 202, replay.text
        assert replay.json().get("replayed") is True
        task_id = UUID(groups["有效订单"]["task_id"])
        await _wait_state(client, task_id, "WAITING_APPROVAL")

        async with async_session_factory() as session:
            spans = list(
                (
                    await session.scalars(
                        select(TraceSpan).where(TraceSpan.task_id == task_id)
                    )
                ).all()
            )
            models = [span for span in spans if span.kind.casefold() == "model"]
            tools = {
                span.name
                for span in spans
                if span.kind.casefold() == "tool" and span.status == "OK"
            }
            assert any(
                span.execution_mode == "LIVE" and span.status == "OK" for span in models
            )
            required_tools = {
                "get_contract",
                "match_material",
                "save_order_draft",
                "check_contract_balance",
            }
            assert required_tools <= tools, (
                f"真实 MCP 成功工具轨迹不完整；缺少={sorted(required_tools - tools)}，"
                f"实际={sorted(tools)}"
            )
            task = await session.get(AgentTask, task_id)
            start_command = await session.scalar(
                select(TaskCommand)
                .where(
                    TaskCommand.task_id == task_id,
                    TaskCommand.kind == "START",
                )
                .order_by(TaskCommand.created_at)
            )
            assert task is not None and start_command is not None
            assert start_command.payload.get("input_type") == "EXCEL_GROUP"
            bad_items = list(
                (
                    await session.scalars(
                        select(ImportBatchItem).where(
                            ImportBatchItem.batch_id == batch_id,
                            ImportBatchItem.group_id.in_(["超额订单", "缺字段订单"]),
                        )
                    )
                ).all()
            )
            assert len(bad_items) == 2 and all(
                item.task_id is None for item in bad_items
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.task_id == task_id)
                )
                == 0
            )

    print(f"ISSUE035_BATCH_ID={batch_id}")
    print(f"ISSUE035_TASK_ID={task_id}")
    print("ISSUE035_LIVE_MODEL_MCP=verified")
    print("ISSUE035_WAIT_STATE=WAITING_APPROVAL")


async def assert_approval_wait(task_id: UUID) -> None:
    _require_live_environment()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30) as client:
        await _login(client, "demo-operator-north")
        data = await _snapshot(client, task_id)
        trace_response = await client.get(f"/api/v1/tasks/{task_id}/trace")
        assert trace_response.status_code == 200, trace_response.text
        trace = trace_response.json()["data"]
    wait = data.get("active_wait")
    assert data.get("state") == "WAITING_APPROVAL"
    assert isinstance(wait, dict) and wait.get("kind") == "APPROVAL"
    assert wait.get("checkpoint_id") and wait.get("interrupt_id")
    assert data.get("order_ref") is None
    assert any(
        item["state"] == "WAITING_APPROVAL" and item["active"]
        for item in trace["metrics"]["manual_waits"]
    )
    async with async_session_factory() as session:
        task = await session.get(AgentTask, task_id)
        assert task is not None and task.state == "WAITING_APPROVAL"
        assert (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.task_id == task_id)
            )
            == 0
        )
    print("ISSUE035_APPROVAL_WAIT_RECOVERY=verified")


async def approve_batch(batch_id: UUID, task_id: UUID) -> None:
    _require_live_environment()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30) as operator:
        await _login(operator, "demo-operator-north")
        before = await _snapshot(operator, task_id)
    wait = before.get("active_wait")
    draft = before.get("draft")
    assert before.get("state") == "WAITING_APPROVAL"
    assert isinstance(wait, dict) and isinstance(draft, dict)

    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30) as approver:
        await _login(approver, "demo-approver-north")
        approved = await approver.post(
            f"/api/v1/tasks/{task_id}/review",
            headers={"Idempotency-Key": f"issue035-approve-{task_id}"},
            json={
                "decision": "APPROVE",
                "draft_version": draft["version"],
                "content_hash": draft["content_hash"],
                "checkpoint_id": wait["checkpoint_id"],
                "interrupt_id": wait["interrupt_id"],
                "reason": "M5-07 隔离批次在线验收，批准当前版本",
            },
        )
        assert approved.status_code == 202, approved.text
        completed = await _wait_state(approver, task_id, "COMPLETED")
        order_ref = completed.get("order_ref")
        assert isinstance(order_ref, dict)
        order_id = UUID(order_ref["order_id"])

        detail_response = await approver.get(f"/api/v1/orders/{order_id}")
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()["data"]
        assert detail["task"]["id"] == str(task_id)
        assert detail["approval"]["decision"] == "APPROVE"
        assert detail["approval"]["version"] == draft["version"]
        assert detail["approval"]["content_hash"] == draft["content_hash"]
        assert any(event["action"] == "order.committed" for event in detail["audit"])

        trace_response = await approver.get(f"/api/v1/tasks/{task_id}/trace")
        assert trace_response.status_code == 200, trace_response.text
        trace = trace_response.json()["data"]
        assert trace["task_id"] == str(task_id)
        assert trace["state"] == "COMPLETED"
        assert trace["metrics"]["model_calls"] >= 1
        assert trace["metrics"]["usage_status"] in {"available", "unavailable"}
        if trace["metrics"]["usage_status"] == "unavailable":
            assert trace["metrics"]["tokens"] is None
        assert any(
            item["state"] == "WAITING_APPROVAL" and not item["active"]
            for item in trace["metrics"]["manual_waits"]
        )
        assert any(
            span["kind"] == "MODEL"
            and span["execution_mode"] == "LIVE"
            and span["status"] == "OK"
            and span["duration_ms"] is not None
            for span in trace["spans"]
        )
        assert any(
            span["kind"] == "TOOL"
            and span["name"] == "save_order_draft"
            and span["status"] == "OK"
            and span["duration_ms"] is not None
            for span in trace["spans"]
        )
        assert any(
            item["decision"] == "APPROVE"
            and item["content_hash"] == draft["content_hash"]
            for item in trace["approvals"]
        )
        assert any(
            item["action"] == "order.committed" for item in trace["audit_events"]
        )

        async with httpx.AsyncClient(
            base_url="http://api:8000", timeout=30
        ) as batch_operator:
            await _login(batch_operator, "demo-operator-north")
            batch_response = await batch_operator.get(f"/api/v1/imports/{batch_id}")
            assert batch_response.status_code == 200, batch_response.text
            batch = batch_response.json()["data"]
            by_group = {group["group_id"]: group for group in batch["groups"]}
            assert batch["state"] == "PARTIAL"
            assert by_group["有效订单"]["state"] == "COMPLETED"
            assert by_group["有效订单"]["order_id"] == str(order_id)
            assert by_group["超额订单"]["task_id"] is None
            assert by_group["缺字段订单"]["task_id"] is None

    async with async_session_factory() as session:
        order_count = await session.scalar(
            select(func.count()).select_from(Order).where(Order.task_id == task_id)
        )
        occupation_count = await session.scalar(
            select(func.count())
            .select_from(QuotaOccupation)
            .where(QuotaOccupation.order_id == order_id)
        )
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.task_id == task_id, AuditEvent.action == "order.committed"
            )
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(
                TaskEvent.task_id == task_id,
                TaskEvent.event_type == "task.order_committed",
            )
        )
        approval = await session.scalar(
            select(TaskApproval).where(
                TaskApproval.task_id == task_id,
                TaskApproval.decision == "APPROVE",
            )
        )
        approver = await session.scalar(
            select(User).where(User.login == "demo-approver-north")
        )
        batch = await session.get(ImportBatch, batch_id)
        invalid_order_count = await session.scalar(
            select(func.count())
            .select_from(Order)
            .join(ImportBatchItem, ImportBatchItem.task_id == Order.task_id)
            .where(
                ImportBatchItem.batch_id == batch_id,
                ImportBatchItem.group_id.in_(["超额订单", "缺字段订单"]),
            )
        )
        assert order_count == occupation_count == audit_count == event_count == 1
        assert invalid_order_count == 0
        assert (
            approval is not None
            and approver is not None
            and approval.actor_id == approver.id
        )
        assert approver is not None and any(
            item["actor_id"] == str(approver.id)
            for item in trace["approvals"]
            if item["decision"] == "APPROVE"
        )
        assert batch is not None and batch.state == "PARTIAL"

    print(f"ISSUE035_ORDER_ID={order_id}")
    print("ISSUE035_BATCH_ORDER_APPROVAL_QUOTA_AUDIT=verified")


async def run(args: argparse.Namespace) -> None:
    if args.phase == "mcp-smoke":
        await mcp_smoke()
    elif args.phase == "prepare-live":
        await prepare_live_queue()
    elif args.phase == "create-input":
        await create_input_wait()
    elif args.phase == "assert-input-wait":
        await assert_input_wait(UUID(args.task_id))
    elif args.phase == "create-batch":
        await create_batch()
    elif args.phase == "assert-approval-wait":
        await assert_approval_wait(UUID(args.task_id))
    else:
        await approve_batch(UUID(args.batch_id), UUID(args.task_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="M5-07 批量双模式和轨迹在线联调")
    parser.add_argument(
        "phase",
        choices=(
            "mcp-smoke",
            "prepare-live",
            "create-input",
            "assert-input-wait",
            "create-batch",
            "assert-approval-wait",
            "approve-batch",
        ),
    )
    parser.add_argument("--batch-id")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    if args.phase in {"assert-input-wait", "assert-approval-wait"} and not args.task_id:
        parser.error("等待态回查阶段需要 --task-id")
    if args.phase == "approve-batch" and (not args.batch_id or not args.task_id):
        parser.error("approve-batch 阶段需要 --batch-id 和 --task-id")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
