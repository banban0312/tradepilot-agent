"""M2-05 离线回路、预算、重试与真实模型配置状态验证。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from app.agent.function_calling import (
    FunctionCallingFailure,
    FunctionCallingRunner,
    _strict_model_schema,
    create_configured_model_client,
)
from app.agent.model_client import (
    ModelProviderError,
    ModelResponse,
    OpenAIResponsesClient,
)
from app.auth.context import BUSINESS_OPERATOR, AuthContext
from app.config import Settings, settings
from app.mcp_server.client import INPUT_SCHEMAS
from app.mcp_server.contracts import MCPUnavailable, StrictModel

DEPARTMENT_ID = UUID("00000000-0000-4000-8000-000000000001")
ACTOR_ID = UUID("00000000-0000-4000-8000-000000000100")
MATERIAL_ID = UUID("00000000-0000-4000-8000-000000000030")
TASK_ID = UUID("00000000-0000-4000-8000-000000000500")
JOB_ID = UUID("00000000-0000-4000-8000-000000000501")


class ToolResult(StrictModel):
    data: dict[str, object]


class MemoryTraceSink:
    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.finished: list[dict[str, object]] = []

    async def start(self, **values):
        span_id = uuid4()
        self.started.append({**values, "span_id": span_id})
        return span_id

    async def finish(self, **values):
        self.finished.append(values)


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    async def create_response(self, **request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("测试模型响应已耗尽")
        return self.responses.pop(0)


class ScriptedMCP:
    def __init__(
        self, *, fail_first: bool = False, fail_list_first: bool = False
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_first = fail_first
        self.fail_list_first = fail_list_first
        self.list_attempts = 0

    async def list_tools(self):
        self.list_attempts += 1
        if self.fail_list_first and self.list_attempts == 1:
            raise MCPUnavailable("offline registry retry fixture")
        return [
            {
                "name": name,
                "inputSchema": schema.model_json_schema(),
            }
            for name, schema in INPUT_SCHEMAS.items()
        ]

    async def call_tool(self, name, arguments, *, context_token):
        self.calls.append(
            {"name": name, "arguments": arguments, "context_token": context_token}
        )
        if self.fail_first and len(self.calls) == 1:
            raise MCPUnavailable("offline retry fixture")
        return ToolResult(data={"source": "isolated-mcp-fixture", "found": True})


def _settings(**overrides) -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+psycopg://user:password@localhost/db"),
        model_provider="openai",
        model_name="test-model",
        model_api_key=SecretStr("test-key-not-real"),
        **overrides,
    )


def _tool_response(
    name: str = "query_customer", arguments: str = '{"name":"建联公司"}'
) -> ModelResponse:
    return ModelResponse(
        response_id="resp-tool",
        model="test-model-version",
        output=(
            {
                "type": "function_call",
                "id": "fc_001",
                "call_id": "call_001",
                "name": name,
                "arguments": arguments,
            },
        ),
        output_text="",
        usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
        attempts=1,
    )


def _final_response(*, usage: dict[str, int] | None = None) -> ModelResponse:
    return ModelResponse(
        response_id="resp-final",
        model="test-model-version",
        output=(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "已查询客户并保存事实结果。"}
                ],
            },
        ),
        output_text="已查询客户并保存事实结果。",
        usage=usage,
        attempts=1,
    )


def _actor() -> AuthContext:
    return AuthContext(
        user_id=ACTOR_ID,
        login="issue-12-operator",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({DEPARTMENT_ID}),
    )


def _call() -> dict[str, object]:
    return {
        "actor": _actor(),
        "department_id": DEPARTMENT_ID,
        "input_at": datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
        "job_id": JOB_ID,
        "lease": SimpleNamespace(task_id=TASK_ID, epoch=1),
        "task_id": TASK_ID,
        "task_type": "SALES",
        "user_text": "给建联公司项目订 20 吨 HRB400E，后天交付；没有提供价格。",
    }


def _runner(model, mcp, trace, *, config=None, issued=None) -> FunctionCallingRunner:
    def issue_context(**values):
        if issued is not None:
            issued.append(values)
        return "test-signed-context"

    return FunctionCallingRunner(
        model_client=model,
        mcp_client=mcp,  # type: ignore[arg-type]
        trace_sink=trace,
        config=config or _settings(),
        context_issuer=issue_context,
    )


def _assert_strict_schema(schema: object) -> None:
    if isinstance(schema, list):
        for item in schema:
            _assert_strict_schema(item)
        return
    if not isinstance(schema, dict):
        return
    assert "default" not in schema
    properties = schema.get("properties")
    if isinstance(properties, dict):
        assert schema.get("additionalProperties") is False
        assert schema.get("required") == list(properties)
    for value in schema.values():
        _assert_strict_schema(value)


def test_strict_query_customer_schema_moves_root_validation_to_mcp() -> None:
    """根级 anyOf 不兼容 Responses strict；MCP 保留 name/code 业务校验。"""
    source = INPUT_SCHEMAS["query_customer"].model_json_schema()
    assert "anyOf" in source["properties"]["name"]
    source["anyOf"] = [
        {"required": ["name"]},
        {"required": ["code"]},
    ]

    projected = _strict_model_schema(source)

    assert projected["type"] == "object"
    assert "anyOf" not in projected
    _assert_strict_schema(projected)
    with pytest.raises(ValueError):
        INPUT_SCHEMAS["query_customer"].model_validate({}, strict=True)


@pytest.mark.asyncio
async def test_model_tool_choice_runs_mcp_and_returns_real_usage_shape() -> None:
    model = ScriptedModel(
        [
            _tool_response(),
            _final_response(
                usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}
            ),
        ]
    )
    mcp = ScriptedMCP()
    trace = MemoryTraceSink()
    issued: list[dict[str, object]] = []

    result = await _runner(model, mcp, trace, issued=issued).run(**_call())

    assert result.final_text == "已查询客户并保存事实结果。"
    assert result.model_rounds == 2
    assert result.tool_calls == 1
    assert result.response_model == "test-model-version"
    assert result.usage == {
        "input_tokens": 24,
        "output_tokens": 8,
        "total_tokens": 32,
    }
    assert mcp.calls[0]["name"] == "query_customer"
    assert mcp.calls[0]["arguments"] == {"name": "建联公司", "limit": 5}
    assert mcp.calls[0]["context_token"] == "test-signed-context"
    assert issued[0]["actor"] == _actor()
    assert issued[0]["department_id"] == DEPARTMENT_ID
    assert issued[0]["allowed_tools"] == frozenset({"query_customer"})
    assert issued[0]["operation_id"] == uuid5(
        NAMESPACE_URL,
        f"tradepilot/tool-effect/{JOB_ID}/1/1",
    )
    assert len(model.requests[0]["tools"]) == 5
    assert model.requests[0]["tools"][0]["type"] == "function"
    definitions = model.requests[0]["tools"]
    assert all(definition["strict"] is True for definition in definitions)
    for definition in definitions:
        _assert_strict_schema(definition["parameters"])
    draft_schema = next(
        definition["parameters"]
        for definition in definitions
        if definition["name"] == "save_order_draft"
    )
    assert "business_fields" not in str(draft_schema)
    assert "raw_value" not in str(draft_schema)
    assert "accepted_value" not in str(draft_schema)
    assert model.requests[0]["input_items"][0]["role"] == "user"
    assert "2026-10-16" in model.requests[0]["input_items"][0]["content"]
    assert "Asia/Hong_Kong" in model.requests[0]["input_items"][0]["content"]
    assert "后天" in model.requests[0]["input_items"][0]["content"]
    assert model.requests[1]["input_items"][-1]["type"] == "function_call_output"
    assert len(trace.started) == 4  # MCP 工具注册、模型两轮与实际工具调用
    actual_tool_span = next(
        item
        for item in trace.finished
        if item.get("output_ref", {}).get("tool") == "query_customer"
    )
    summary = actual_tool_span["output_ref"]["result_summary"]
    assert "source" in summary["data_fields"]
    assert "found" in summary["data_fields"]
    assert "isolated-mcp-fixture" not in json.dumps(trace.finished, default=str)
    assert "建联公司" not in json.dumps(trace.started, ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_retry_keeps_logical_operation_identity(monkeypatch) -> None:
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.agent.function_calling.asyncio.sleep", no_wait)
    model = ScriptedModel([_tool_response(), _final_response()])
    mcp = ScriptedMCP(fail_first=True)
    trace = MemoryTraceSink()
    issued: list[dict[str, object]] = []

    result = await _runner(model, mcp, trace, issued=issued).run(**_call())

    assert result.tool_calls == 1
    assert len(mcp.calls) == 2
    assert len(issued) == 2
    assert issued[0]["operation_id"] == issued[1]["operation_id"]
    assert issued[0]["tool_call_id"] == issued[1]["tool_call_id"]
    assert [
        entry["attempt"]
        for entry in trace.started
        if entry.get("name") == "query_customer"
    ] == [1, 2]


@pytest.mark.asyncio
async def test_tool_registry_retry_records_each_external_attempt(monkeypatch) -> None:
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.agent.function_calling.asyncio.sleep", no_wait)
    model = ScriptedModel([_final_response()])
    mcp = ScriptedMCP(fail_list_first=True)
    trace = MemoryTraceSink()

    result = await _runner(model, mcp, trace).run(**_call())

    assert result.final_text == "已查询客户并保存事实结果。"
    assert mcp.list_attempts == 2
    registry_spans = [
        entry for entry in trace.started if entry["name"] == "mcp.list_tools"
    ]
    assert [entry["attempt"] for entry in registry_spans] == [1, 2]
    registry_ids = {entry["span_id"] for entry in registry_spans}
    registry_results = [
        entry for entry in trace.finished if entry["span_id"] in registry_ids
    ]
    assert [entry["status"] for entry in registry_results] == ["ERROR", "OK"]


@pytest.mark.asyncio
async def test_tool_budget_blocks_entire_over_budget_model_round() -> None:
    over_budget = ModelResponse(
        response_id="resp-over-budget",
        model="test-model-version",
        output=tuple(
            {
                "type": "function_call",
                "id": f"fc_{index}",
                "call_id": f"call_{index}",
                "name": "query_customer",
                "arguments": json.dumps({"name": "客户"}),
            }
            for index in range(2)
        ),
        output_text="",
        usage=None,
        attempts=1,
    )
    model = ScriptedModel([over_budget])
    mcp = ScriptedMCP()
    trace = MemoryTraceSink()
    runner = _runner(
        model,
        mcp,
        trace,
        config=_settings(model_max_tool_calls=1),
    )

    with pytest.raises(FunctionCallingFailure) as error:
        await runner.run(**_call())

    assert error.value.code == "MCP_TOOL_BUDGET_EXCEEDED"
    assert mcp.calls == []
    assert any(
        entry.get("error", {}).get("code") == "MCP_TOOL_BUDGET_EXCEEDED"
        for entry in trace.finished
    )


@pytest.mark.asyncio
async def test_invalid_or_unavailable_model_tools_fail_without_mcp_side_effect() -> (
    None
):
    bad_argument = ScriptedModel([_tool_response(arguments='{"name":NaN}')])
    mcp = ScriptedMCP()
    with pytest.raises(FunctionCallingFailure) as error:
        await _runner(bad_argument, mcp, MemoryTraceSink()).run(**_call())
    assert error.value.code == "TOOL_INPUT_INVALID"
    assert mcp.calls == []

    unavailable_schema = ScriptedMCP()
    unavailable_schema.list_tools = lambda: _async_empty_tools()
    with pytest.raises(FunctionCallingFailure) as error:
        await _runner(ScriptedModel([]), unavailable_schema, MemoryTraceSink()).run(
            **_call()
        )
    assert error.value.code == "MCP_TOOLS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_empty_model_answer_is_a_traced_failure() -> None:
    empty_response = ModelResponse(
        response_id="resp-empty",
        model="test-model-version",
        output=(),
        output_text="",
        usage=None,
        attempts=1,
    )
    trace = MemoryTraceSink()

    with pytest.raises(FunctionCallingFailure) as error:
        await _runner(ScriptedModel([empty_response]), ScriptedMCP(), trace).run(
            **_call()
        )

    assert error.value.code == "MODEL_OUTPUT_EMPTY"
    assert any(
        entry.get("error", {}).get("code") == "MODEL_OUTPUT_EMPTY"
        for entry in trace.finished
    )


async def _async_empty_tools():
    return []


async def _create_persisted_task():
    from app.database import async_session_factory
    from app.models import User
    from app.tasks.service import create_task

    task_id = uuid4()
    async with async_session_factory() as session, session.begin():
        user = await session.get(User, ACTOR_ID)
        assert user is not None and user.active
        task = await create_task(
            session,
            owner_id=ACTOR_ID,
            department_id=DEPARTMENT_ID,
            task_type="SALES",
            title="Issue 12 MCP 集成隔离验收",
            task_id=task_id,
        )
        task.state = "RUNNING"
        task.execution_epoch = 1
    return task_id


@pytest.mark.asyncio
async def test_function_calling_loop_uses_real_mcp_server_when_enabled() -> None:
    if os.getenv("TRADEPILOT_MCP_INTEGRATION") != "1":
        pytest.skip("真实 MCP 联调由 verify_issue_012.ps1 启动隔离 Compose")

    from sqlalchemy import func, select

    from app.agent.function_calling import DatabaseTraceSink
    from app.database import async_session_factory
    from app.mcp_server.client import MCPToolClient
    from app.models import Draft, Order, TraceSpan

    task_id = await _create_persisted_task()
    model = ScriptedModel(
        [
            _tool_response(arguments='{"name":"华北示例客户"}'),
            _final_response(),
        ]
    )
    client = MCPToolClient(
        settings.mcp_url, timeout_seconds=settings.model_timeout_seconds
    )
    trace = DatabaseTraceSink(async_session_factory)
    runner = FunctionCallingRunner(
        model_client=model,
        mcp_client=client,
        trace_sink=trace,
        config=settings,
    )
    actor = AuthContext(
        user_id=ACTOR_ID,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({DEPARTMENT_ID}),
    )
    result = await runner.run(
        task_id=task_id,
        job_id=uuid4(),
        task_type="SALES",
        actor=actor,
        department_id=DEPARTMENT_ID,
        lease=SimpleNamespace(task_id=task_id, epoch=1),  # type: ignore[arg-type]
        input_at=datetime(2026, 10, 16, 9, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
        user_text="查询华北示例客户并简要回复真实查询结果。",
    )

    async with async_session_factory() as session:
        spans = list(
            (
                await session.scalars(
                    select(TraceSpan).where(TraceSpan.trace_id == result.trace_id)
                )
            ).all()
        )
        drafts = int(
            await session.scalar(
                select(func.count()).select_from(Draft).where(Draft.task_id == task_id)
            )
            or 0
        )
        orders = int(await session.scalar(select(func.count()).select_from(Order)) or 0)

    assert result.tool_calls == 1
    assert any(
        span.name == "query_customer"
        and span.output_ref
        and span.output_ref.get("result_summary", {}).get("candidates_count", 0) > 0
        for span in spans
    ), "MCP 查询必须返回隔离数据库中的真实客户候选"
    trace_evidence = json.dumps(
        [
            {"input_ref": span.input_ref, "output_ref": span.output_ref}
            for span in spans
        ],
        ensure_ascii=False,
    )
    assert "华北示例客户" not in trace_evidence
    assert all(span.status == "OK" for span in spans)
    assert drafts == 0
    assert orders == 0


@pytest.mark.asyncio
async def test_responses_client_retries_rate_limit_and_preserves_usage() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"error": {"message": "private detail"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_live_shape",
                "model": "configured-model-2026-10",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    async def no_wait(_seconds):
        return None

    import app.agent.model_client as client_module

    original_sleep = client_module.asyncio.sleep
    client_module.asyncio.sleep = no_wait
    try:
        client = OpenAIResponsesClient(
            model="configured-model",
            api_key=SecretStr("not-a-real-key"),
            max_retries=1,
            transport=httpx.MockTransport(handler),
        )
        response = await client.create_response(
            instructions="只用于离线测试",
            input_items=[{"role": "user", "content": "测试"}],
            tools=[],
        )
    finally:
        client_module.asyncio.sleep = original_sleep

    assert len(requests) == 2
    assert requests[0].url.path == "/v1/responses"
    assert requests[0].headers["authorization"] == "Bearer not-a-real-key"
    body = json.loads(requests[0].content)
    assert body["store"] is False
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False
    assert response.response_id == "resp_live_shape"
    assert response.model == "configured-model-2026-10"
    assert response.output_text == "ok"
    assert response.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    assert response.attempts == 2


@pytest.mark.asyncio
async def test_responses_client_does_not_retry_exhausted_model_quota() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            429,
            json={
                "error": {
                    "type": "insufficient_quota",
                    "code": "credit_balance_exhausted",
                    "message": "private provider detail",
                }
            },
        )

    client = OpenAIResponsesClient(
        model="configured-model",
        api_key=SecretStr("not-a-real-key"),
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelProviderError) as error:
        await client.create_response(
            instructions="测试额度错误分类",
            input_items=[{"role": "user", "content": "测试"}],
            tools=[],
        )

    assert error.value.code == "MODEL_QUOTA_EXHAUSTED"
    assert error.value.retryable is False
    assert error.value.attempts == 1
    assert len(requests) == 1
    assert "private provider detail" not in str(error.value)


@pytest.mark.asyncio
async def test_incomplete_model_response_fails_without_a_success_payload() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_incomplete",
                "model": "configured-model",
                "status": "incomplete",
                "output": [],
            },
        )

    client = OpenAIResponsesClient(
        model="configured-model",
        api_key=SecretStr("not-a-real-key"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelProviderError) as error:
        await client.create_response(
            instructions="测试不完整结果",
            input_items=[{"role": "user", "content": "测试"}],
            tools=[],
        )

    assert error.value.code == "MODEL_RESPONSE_INCOMPLETE"
    assert error.value.attempts == 1

    async def refusal_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_refusal",
                "model": "configured-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "不处理此请求"}],
                    }
                ],
            },
        )

    refusal_client = OpenAIResponsesClient(
        model="configured-model",
        api_key=SecretStr("not-a-real-key"),
        transport=httpx.MockTransport(refusal_handler),
    )
    with pytest.raises(ModelProviderError) as refusal:
        await refusal_client.create_response(
            instructions="测试拒绝结果",
            input_items=[{"role": "user", "content": "测试"}],
            tools=[],
        )
    assert refusal.value.code == "MODEL_REFUSED"


def test_model_service_requires_tls_outside_local_development() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAIResponsesClient(
            model="configured-model",
            api_key=SecretStr("not-a-real-key"),
            base_url="http://model-gateway.example/v1",
        )


def test_missing_live_credentials_are_reported_as_unconfigured() -> None:
    config = Settings(
        database_url=SecretStr("postgresql+psycopg://user:password@localhost/db"),
        model_provider=None,
        model_name=None,
        model_api_key=None,
    )

    with pytest.raises(FunctionCallingFailure) as error:
        create_configured_model_client(config)

    assert error.value.code == "MODEL_PROVIDER_UNSUPPORTED"


@pytest.mark.asyncio
async def test_online_model_selects_real_mcp_tools_and_creates_no_order() -> None:
    """仅在显式开启且配有真凭据时在线运行，必须使用隔离 Compose 数据库。"""
    if os.getenv("TRADEPILOT_ONLINE_MODEL") != "1":
        pytest.skip("未显式开启在线模型验证；离线替身不能代表真实模型验收")
    if settings.model_configuration_state != "configured_unverified":
        pytest.skip("MODEL_PROVIDER、MODEL_NAME 或 MODEL_API_KEY 尚未完整配置")

    from sqlalchemy import func, select

    from app.agent.function_calling import create_database_runner
    from app.database import async_session_factory
    from app.models import Draft, DraftItem, DraftRevision, Order, TraceSpan, User
    from app.tasks.service import create_task

    task_id, job_id = uuid4(), uuid4()
    async with async_session_factory() as session, session.begin():
        user = await session.get(User, ACTOR_ID)
        assert user is not None and user.active
        task = await create_task(
            session,
            owner_id=ACTOR_ID,
            department_id=DEPARTMENT_ID,
            task_type="SALES",
            title="Issue 12 在线模型隔离验收",
            task_id=task_id,
        )
        task.state = "RUNNING"
        task.execution_epoch = 1

    actor = AuthContext(
        user_id=ACTOR_ID,
        login="demo-operator-north",
        roles=frozenset({BUSINESS_OPERATOR}),
        department_ids=frozenset({DEPARTMENT_ID}),
    )
    runner = create_database_runner(
        session_factory=async_session_factory, config=settings
    )
    result = await runner.run(
        task_id=task_id,
        job_id=job_id,
        task_type="SALES",
        actor=actor,
        department_id=DEPARTMENT_ID,
        lease=SimpleNamespace(task_id=task_id, epoch=1),  # type: ignore[arg-type]
        input_at=datetime(2026, 10, 16, 9, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
        user_text=(
            "请用全部相关业务工具逐步完成：查询客户 TP-DEMO-CUST-N1（华北示例客户），"
            "查询销售合同 TP-DEMO-SALES-N1 和项目 TP-DEMO-PROJ-N1，匹配 HRB400 12mm 螺纹钢，"
            "数量 10 吨，交期后天，单价只能使用合同主数据中的价格。"
            "保存草稿后，再用真实草稿 ID、版本和金额核验合同额度。"
            "仅生成草稿，绝不审批或创建正式订单。"
        ),
    )

    async with async_session_factory() as session:
        spans = list(
            (
                await session.scalars(
                    select(TraceSpan).where(TraceSpan.trace_id == result.trace_id)
                )
            ).all()
        )
        draft_count = int(
            await session.scalar(
                select(func.count()).select_from(Draft).where(Draft.task_id == task_id)
            )
            or 0
        )
        order_count = int(
            await session.scalar(select(func.count()).select_from(Order)) or 0
        )
        latest_revision = await session.scalar(
            select(DraftRevision)
            .join(Draft, DraftRevision.draft_id == Draft.id)
            .where(Draft.task_id == task_id)
            .order_by(DraftRevision.version.desc())
            .limit(1)
        )
        latest_items = (
            list(
                (
                    await session.scalars(
                        select(DraftItem).where(
                            DraftItem.draft_id == latest_revision.draft_id,
                            DraftItem.version == latest_revision.version,
                        )
                    )
                ).all()
            )
            if latest_revision is not None
            else []
        )

    tool_names = {span.name for span in spans if span.kind.casefold() == "tool"}
    required_names = {
        "query_customer",
        "get_contract",
        "match_material",
        "save_order_draft",
        "check_contract_balance",
    }
    assert required_names <= tool_names, (
        f"真实模型未完成要求的 MCP 工具链：{sorted(tool_names)}"
    )
    assert result.model_rounds <= settings.model_max_rounds
    assert result.tool_calls <= settings.model_max_tool_calls
    assert draft_count == 1
    assert order_count == 0
    assert latest_revision is not None
    assert latest_revision.delivery_date is not None
    assert latest_revision.delivery_date.isoformat() == "2026-10-18"
    assert latest_revision.total_amount == Decimal("35000.00")
    assert len(latest_items) == 1
    assert latest_items[0].material_id == MATERIAL_ID
    assert latest_items[0].quantity == Decimal("10.000000")
    assert latest_items[0].unit == "吨"
    assert latest_items[0].unit_price == Decimal("3500.000000")
    assert latest_items[0].amount == Decimal("35000.00")
    assert all(span.status in {"OK", "ERROR", "INTERRUPTED"} for span in spans)
