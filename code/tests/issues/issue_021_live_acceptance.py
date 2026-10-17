"""#21 在线验收命令：真实任务等待、恢复、人工批准及正式订单回查。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select

sys.path.insert(0, "/app")

from app.config import settings
from app.database import async_session_factory
from app.models import (
    AgentTask,
    AuditEvent,
    Order,
    QuotaOccupation,
    TaskApproval,
    TaskEvent,
    TraceSpan,
    User,
)

API_URL = os.environ.get("TRADEPILOT_TEST_API_URL", "http://api:8000")
ORIGIN = settings.app_origin or "http://localhost:5173"
ONLINE_REQUEST = (
    "请根据我上一条消息，创建一笔华北示例客户的销售订单：客户编码 "
    "TP-DEMO-CUST-N1，销售合同 TP-DEMO-SALES-N1，项目 TP-DEMO-PROJ-N1；"
    "物料螺纹钢 HRB400 12mm，数量 2 吨，交期 2026-10-25，送到北京市朝阳区示例工地，"
    "联系人张先生。单价必须从合同主数据读取。请调用业务工具核实客户、合同、项目、"
    "物料和额度并保存有效草稿。需要我补充的信息请明确提出；不得批准或直接创建正式订单。"
)


def _require_online_test() -> None:
    if os.getenv("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("在线验收仅可在本脚本创建的隔离 Compose 环境执行")
    if os.getenv("TRADEPILOT_ONLINE_MODEL") != "1":
        raise RuntimeError("在线验收必须显式启用真实模型")
    if (
        settings.model_provider != "deepseek"
        or settings.model_configuration_state != "configured_unverified"
    ):
        raise RuntimeError("本机 DeepSeek 模型配置不完整；密钥值不会显示")
    if (
        not settings.mcp_context_signing_key
        or not settings.mcp_context_signing_key.get_secret_value()
    ):
        raise RuntimeError("在线验收缺少隔离 MCP 上下文签名密钥")


def _password() -> str:
    value = os.getenv("DEMO_PASSWORD", "")
    if len(value) < 12:
        raise RuntimeError("隔离演示账号密码未配置")
    return value


async def _login(client: httpx.AsyncClient, login: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"login": login, "password": _password()},
    )
    assert response.status_code == 200, (
        f"隔离演示账号登录失败：HTTP {response.status_code}"
    )
    identity = response.json()["data"]
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": identity["csrf_token"]})
    return identity


async def _snapshot(client: httpx.AsyncClient, task_id: UUID) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200, f"任务快照读取失败：HTTP {response.status_code}"
    return response.json()["data"]


async def _wait_for_state(
    client: httpx.AsyncClient, task_id: UUID, expected: str, timeout: int = 300
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await _snapshot(client, task_id)
        if latest.get("state") == expected:
            return latest
        if latest.get("state") in {"FAILED", "BLOCKED_RULE", "REJECTED", "COMPLETED"}:
            break
        await asyncio.sleep(1)
    raise AssertionError(
        f"任务未到达预期状态 {expected}；实际状态 {latest.get('state')}，"
        "请检查隔离 Worker、模型与 MCP 日志。"
    )


async def _create_input_wait() -> None:
    _require_online_test()
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        await _login(client, "demo-operator-north")
        response = await client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": f"issue021-start-{uuid4()}"},
            json={"order_type": "SALES", "input": {"text": "我想买一批螺纹钢。"}},
        )
        assert response.status_code == 202, (
            f"任务 API 未受理：HTTP {response.status_code}"
        )
        task_id = UUID(response.json()["data"]["task_id"])
        data = await _wait_for_state(client, task_id, "WAITING_INPUT")
        wait = data.get("active_wait")
        assert isinstance(wait, dict) and wait.get("kind") == "INPUT"
        assert data.get("order_ref") is None
        async with async_session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.task_id == task_id)
                )
                == 0
            )
        print(f"ISSUE021_TASK_ID={task_id}")
        print("ISSUE021_WAIT_STATE=WAITING_INPUT")


async def _assert_wait(task_id: UUID, expected: str) -> None:
    _require_online_test()
    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        await _login(client, "demo-operator-north")
        data = await _snapshot(client, task_id)
    wait = data.get("active_wait")
    expected_kind = "INPUT" if expected == "WAITING_INPUT" else "APPROVAL"
    assert data.get("state") == expected, f"重启后任务状态变成 {data.get('state')}"
    assert isinstance(wait, dict) and wait.get("kind") == expected_kind
    assert wait.get("checkpoint_id") and wait.get("interrupt_id")
    assert data.get("order_ref") is None
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.task_id == task_id)
            )
            == 0
        )
    print(f"ISSUE021_WAIT_STATE={expected}")


async def _continue_to_approval(task_id: UUID) -> None:
    _require_online_test()
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        await _login(client, "demo-operator-north")
        before = await _snapshot(client, task_id)
        wait = before.get("active_wait")
        assert before.get("state") == "WAITING_INPUT" and isinstance(wait, dict)
        response = await client.post(
            f"/api/v1/tasks/{task_id}/inputs",
            headers={"Idempotency-Key": f"issue021-input-{uuid4()}"},
            json={
                "text": ONLINE_REQUEST,
                "checkpoint_id": wait["checkpoint_id"],
                "interrupt_id": wait["interrupt_id"],
                "expected_draft_version": wait.get("draft_version"),
            },
        )
        assert response.status_code == 202, (
            f"补充输入未受理：HTTP {response.status_code}"
        )
        data = await _wait_for_state(client, task_id, "WAITING_APPROVAL")
        draft = data.get("draft")
        wait = data.get("active_wait")
        assert isinstance(draft, dict) and draft.get("validated") is True
        assert isinstance(wait, dict) and wait.get("kind") == "APPROVAL"
        assert data.get("order_ref") is None

    async with async_session_factory() as session:
        spans = list(
            (
                await session.scalars(
                    select(TraceSpan).where(TraceSpan.task_id == task_id)
                )
            ).all()
        )
        model_spans = [span for span in spans if span.kind.casefold() == "model"]
        successful_tools = {
            span.name
            for span in spans
            if span.kind.casefold() == "tool" and span.status == "OK"
        }
        assert any(
            span.status == "OK" and span.execution_mode == "LIVE"
            for span in model_spans
        )
        assert {
            "query_customer",
            "get_contract",
            "match_material",
            "save_order_draft",
            "check_contract_balance",
        } <= successful_tools
        persisted = await session.get(AgentTask, task_id)
        assert persisted is not None and persisted.state == "WAITING_APPROVAL"
    print(f"ISSUE021_TASK_ID={task_id}")
    print("ISSUE021_WAIT_STATE=WAITING_APPROVAL")
    print("ISSUE021_LIVE_MODEL_AND_MCP=verified")


async def _approve_and_verify(task_id: UUID) -> None:
    _require_online_test()
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as operator:
        await _login(operator, "demo-operator-north")
        before = await _snapshot(operator, task_id)
    wait = before.get("active_wait")
    draft = before.get("draft")
    assert before.get("state") == "WAITING_APPROVAL"
    assert isinstance(wait, dict) and isinstance(draft, dict)
    assert wait.get("content_hash") == draft.get("content_hash")

    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as approver:
        await _login(approver, "demo-approver-north")
        response = await approver.post(
            f"/api/v1/tasks/{task_id}/review",
            headers={"Idempotency-Key": f"issue021-approve-{uuid4()}"},
            json={
                "decision": "APPROVE",
                "draft_version": draft["version"],
                "content_hash": draft["content_hash"],
                "checkpoint_id": wait["checkpoint_id"],
                "interrupt_id": wait["interrupt_id"],
                "reason": "M3-07 隔离在线验收批准当前版本",
            },
        )
        assert response.status_code == 202, (
            f"真实审批 API 未受理：HTTP {response.status_code}"
        )
        completed = await _wait_for_state(approver, task_id, "COMPLETED")
        assert isinstance(completed.get("order_ref"), dict)

    order_id = UUID(completed["order_ref"]["order_id"])
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        await _login(client, "demo-operator-north")
        listing = await client.get(
            "/api/v1/orders",
            params={"type": "SALES", "contract": "TP-DEMO-SALES-N1", "limit": 100},
        )
        assert listing.status_code == 200
        listed = [
            row for row in listing.json()["data"]["items"] if row["id"] == str(order_id)
        ]
        assert len(listed) == 1 and listed[0]["task_id"] == str(task_id)
        detail_response = await client.get(f"/api/v1/orders/{order_id}")
        assert detail_response.status_code == 200
        detail = detail_response.json()["data"]
        assert detail["task"]["id"] == str(task_id)
        assert detail["approval"]["decision"] == "APPROVE"
        assert detail["approval"]["version"] == draft["version"]
        assert detail["approval"]["content_hash"] == draft["content_hash"]
        assert detail["total_amount"] == draft["total_amount"]
        assert detail["items"] and detail["audit"]
        assert any(event["action"] == "order.committed" for event in detail["audit"])

    async with async_session_factory() as session:
        order_count = await session.scalar(
            select(func.count()).select_from(Order).where(Order.task_id == task_id)
        )
        occupation_count = await session.scalar(
            select(func.count())
            .select_from(QuotaOccupation)
            .where(QuotaOccupation.order_id == order_id)
        )
        order = await session.get(Order, order_id)
        approval = await session.get(TaskApproval, UUID(detail["approval"]["id"]))
        approver_user = await session.scalar(
            select(User).where(User.login == "demo-approver-north")
        )
        audit_exists = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.task_id == task_id, AuditEvent.action == "order.committed"
            )
        )
        order_event_count = await session.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(
                TaskEvent.task_id == task_id,
                TaskEvent.event_type == "task.order_committed",
            )
        )
        assert order_count == occupation_count == audit_exists == order_event_count == 1
        assert order is not None and approval is not None and approver_user is not None
        assert approval.actor_id == approver_user.id
        assert order.approved_version == approval.version == draft["version"]
        assert (
            order.approved_content_hash
            == approval.content_hash
            == draft["content_hash"]
        )
    print(f"ISSUE021_TASK_ID={task_id}")
    print(f"ISSUE021_ORDER_ID={order_id}")
    print("ISSUE021_ORDER_COMMIT=verified")


async def run(args: argparse.Namespace) -> None:
    if args.phase == "create-input":
        await _create_input_wait()
    elif args.phase == "continue":
        await _continue_to_approval(UUID(args.task_id))
    elif args.phase == "assert-wait":
        await _assert_wait(UUID(args.task_id), args.expected)
    elif args.phase == "approve":
        await _approve_and_verify(UUID(args.task_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="M3-07 在线钢材订单闭环验收")
    parser.add_argument(
        "phase", choices=("create-input", "continue", "assert-wait", "approve")
    )
    parser.add_argument("--task-id")
    parser.add_argument("--expected", choices=("WAITING_INPUT", "WAITING_APPROVAL"))
    args = parser.parse_args()
    if args.phase in {"continue", "approve", "assert-wait"} and not args.task_id:
        parser.error(f"{args.phase} 阶段需要 --task-id")
    if args.phase == "assert-wait" and not args.expected:
        parser.error("assert-wait 阶段需要 --expected")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
