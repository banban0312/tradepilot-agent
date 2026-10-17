"""Issue #1 集成检查：真实 API、PostgreSQL、LangGraph 检查点及 MCP 传输。"""

from __future__ import annotations

import os
from typing import TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from mcp import Client
from psycopg import AsyncConnection, sql

pytestmark = pytest.mark.asyncio


def _test_connection_url() -> tuple[str, str, str]:
    """限制检查点测试仅连到本次 Compose 创建的专用数据库。"""
    if os.environ.get("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("此集成测试只能在隔离 Compose 验证环境中运行")

    database_url = os.environ["DATABASE_URL"]
    parsed = urlsplit(database_url)
    if parsed.hostname != "db" or parsed.path != "/tradepilot":
        raise RuntimeError("测试拒绝连接非隔离的 TradePilot 验证数据库")

    psycopg_url = parsed._replace(scheme="postgresql")
    schema = f"tp_issue001_{uuid4().hex}"
    query = dict(parse_qsl(psycopg_url.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    scoped_url = urlunsplit(psycopg_url._replace(query=urlencode(query)))
    return urlunsplit(psycopg_url), scoped_url, schema


async def test_api_reports_database_readiness_and_configured_local_ocr() -> None:
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=5) as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "database": "connected"}

        configuration = await client.get("/health/config")
        assert configuration.status_code == 200
        body = configuration.json()
        assert body["model"]["status"] == "unconfigured"
        assert body["ocr"] == {
            "status": "configured_unverified",
            "provider": "local_tesseract",
        }
        assert "model_api_key" not in str(body)
        assert "ocr_api_key" not in str(body)


async def test_official_mcp_client_lists_and_calls_real_streamable_http_tool() -> None:
    async with Client(os.environ["MCP_URL"]) as client:
        tools = await client.list_tools()
        health_tool = next((tool for tool in tools.tools if tool.name == "health_check"), None)
        assert health_tool is not None
        assert health_tool.input_schema.get("type") == "object"

        result = await client.call_tool("health_check", {})
        assert result.is_error is False
        assert result.structured_content == {
            "service": "tradepilot-mcp",
            "status": "ok",
        }


class _CheckpointState(TypedDict, total=False):
    count: int
    resumed: bool


def _build_checkpoint_graph(checkpointer: AsyncPostgresSaver):
    def prepare(state: _CheckpointState) -> dict[str, int]:
        return {"count": state["count"] + 1}

    def finish(_: _CheckpointState) -> dict[str, bool]:
        return {"resumed": True}

    builder = StateGraph(_CheckpointState)
    builder.add_node("prepare", prepare)
    builder.add_node("finish", finish)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer, interrupt_before=["finish"])


async def test_langgraph_checkpoint_survives_a_new_postgres_connection() -> None:
    database_url, scoped_url, schema = _test_connection_url()
    thread_id = f"issue-001-{uuid4().hex}"
    graph_config = {"configurable": {"thread_id": thread_id}}

    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    try:
        async with AsyncPostgresSaver.from_conn_string(scoped_url) as first_saver:
            await first_saver.setup()
            first_graph = _build_checkpoint_graph(first_saver)
            result = await first_graph.ainvoke({"count": 0}, graph_config)
            assert result["count"] == 1
            assert "resumed" not in result

        async with AsyncPostgresSaver.from_conn_string(scoped_url) as restarted_saver:
            restarted_graph = _build_checkpoint_graph(restarted_saver)
            saved_state = await restarted_graph.aget_state(graph_config)
            assert saved_state.values == {"count": 1}
            resumed_result = await restarted_graph.ainvoke(None, graph_config)
            assert resumed_result == {"count": 1, "resumed": True}
    finally:
        async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
