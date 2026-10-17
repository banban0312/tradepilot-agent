"""通过真实模型、MCP、Worker 和任务 HTTP API 的可选在线冒烟验收。"""

from __future__ import annotations

import asyncio
import os
import time
from uuid import UUID, uuid4

import httpx
import pytest
from app.config import settings
from app.database import async_session_factory
from app.models import AgentTask, Draft, Order, TaskEvent, TraceSpan
from sqlalchemy import func, select

_ORIGIN = settings.app_origin or "http://api:8000"


def _demo_password() -> str:
    password = os.environ.get("DEMO_PASSWORD", "")
    if os.environ.get("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("在线冒烟验收只能在隔离 Compose 环境运行")
    if os.environ.get("DEMO_MODE") != "true" or len(password) < 12:
        raise RuntimeError("隔离在线验收缺少临时演示账号")
    return password


@pytest.mark.asyncio
async def test_real_task_api_worker_model_and_mcp_flow_creates_only_a_draft() -> None:
    """从 API 入队开始，证明在线模型与 MCP 在 Worker 中完成真实任务。"""
    assert os.getenv("TRADEPILOT_ONLINE_MODEL") == "1", "必须显式开启在线模型验收"
    assert settings.model_provider == "deepseek", "本次在线验收限定使用 DeepSeek"
    assert settings.model_configuration_state == "configured_unverified", (
        "在线验收必须配置模型名称和本机密钥"
    )

    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30) as client:
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={"login": "demo-operator-north", "password": _demo_password()},
        )
        assert login.status_code == 200, (
            f"隔离演示账号登录失败：HTTP {login.status_code}"
        )
        identity = login.json()["data"]
        before_orders = await _order_count()
        created = await client.post(
            "/api/v1/tasks",
            headers={
                "Origin": _ORIGIN,
                "X-CSRF-Token": identity["csrf_token"],
                "Idempotency-Key": f"issue014-live-{uuid4()}",
            },
            json={
                "order_type": "SALES",
                "input": {
                    "text": (
                        "请查询并使用客户 TP-DEMO-CUST-N1（华北示例客户）、销售合同 "
                        "TP-DEMO-SALES-N1 和项目 TP-DEMO-PROJ-N1。商品为螺纹钢 "
                        "HRB400 12mm，数量 2 吨，交期 2026 年 10 月 25 日，送至 "
                        "北京市朝阳区示例工地，联系人张先生。单价必须来自合同主数据。"
                        "请通过可用业务工具查询事实、保存草稿并核验合同余额；只生成草稿，"
                        "不得审批或创建正式订单。"
                    )
                },
            },
        )
        assert created.status_code == 202, (
            f"任务 API 未受理：HTTP {created.status_code}"
        )
        accepted = created.json()["data"]
        assert accepted["state"] == "QUEUED"
        task_id = UUID(accepted["task_id"])

        deadline = time.monotonic() + 240
        task_data: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = await client.get(f"/api/v1/tasks/{task_id}")
            assert response.status_code == 200, "任务状态接口不可用"
            task_data = response.json()["data"]
            state = task_data["state"]
            if state in {"WAITING_APPROVAL", "FAILED", "BLOCKED_RULE", "REJECTED"}:
                break
            await asyncio.sleep(1)

    assert task_data.get("state") == "WAITING_APPROVAL", (
        "真实任务未到达审批等待态；请检查隔离 Worker、模型与 MCP 服务状态"
    )
    draft = task_data.get("draft")
    assert isinstance(draft, dict) and draft.get("validated") is True
    assert task_data.get("order_ref") is None

    async with async_session_factory() as session:
        spans = list(
            (
                await session.scalars(
                    select(TraceSpan).where(TraceSpan.task_id == task_id)
                )
            ).all()
        )
        event_types = set(
            (
                await session.scalars(
                    select(TaskEvent.event_type).where(TaskEvent.task_id == task_id)
                )
            ).all()
        )
        persisted_task = await session.get(AgentTask, task_id)
        persisted_draft = await session.scalar(
            select(Draft).where(Draft.task_id == task_id)
        )
        persisted_order_count = int(
            await session.scalar(select(func.count()).select_from(Order)) or 0
        )

    assert persisted_task is not None and persisted_task.state == "WAITING_APPROVAL"
    assert persisted_draft is not None and persisted_draft.current_version >= 1
    assert any(
        span.kind.casefold() == "model" and span.status == "OK" for span in spans
    )
    successful_tools = {
        span.name
        for span in spans
        if span.kind.casefold() == "tool" and span.status == "OK"
    }
    assert {
        "query_customer",
        "get_contract",
        "match_material",
        "save_order_draft",
        "check_contract_balance",
    } <= successful_tools
    assert "tool.completed" in event_types
    assert persisted_order_count == before_orders, "任务流程不得创建正式订单"


async def _order_count() -> int:
    async with async_session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Order)) or 0)
