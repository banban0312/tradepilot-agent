"""通过真实 API、Worker、MCP 与隔离 PostgreSQL 执行单案例评测。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

_BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if not _BACKEND_ROOT.is_dir():
    _BACKEND_ROOT = Path("/app")
if _BACKEND_ROOT.is_dir() and str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from evals.service import CaseSkipped


class BackendCaseExecutor:
    """每例走正式任务 API 与持久 Worker，仅在线模式调用已配置模型。"""

    def __init__(self, *, mode: str, manifest: dict, seed: int) -> None:
        del seed  # 固定时钟取自每条数据集案例；模型调用本身不使用随机数。
        self.mode = mode
        self.manifest = manifest
        self.api_url = os.getenv("EVAL_API_URL", "http://api:8000").rstrip("/")
        self.project_name = os.getenv("EVAL_PROJECT_NAME", "")
        if os.getenv("TRADEPILOT_EVAL_SANDBOX") != "1":
            raise ValueError("真实评测必须在显式隔离的 Docker 数据域中运行")
        if not self.project_name.startswith("tp-issue037-"):
            raise ValueError("隔离 Compose 项目名必须以 tp-issue037- 开头")
        parsed_api = urlsplit(self.api_url)
        if parsed_api.hostname != "api":
            raise ValueError("评测 API 必须使用隔离 Compose 网络中的 api 服务")

        from app.config import settings

        self.settings = settings
        database_url = make_url(settings.database_url.get_secret_value())
        if database_url.host != "db":
            raise ValueError("评测数据库必须是隔离 Compose 网络中的 db 服务")
        if (
            mode == "online"
            and settings.model_configuration_state != "configured_unverified"
        ):
            raise ValueError("在线评测缺少模型供应商、模型名或密钥配置")
        self.fixtures = json.loads(
            (
                Path(__file__).resolve().parent / "fixtures/v1/master_data.json"
            ).read_text(encoding="utf-8")
        )
        model_fixture_path = (
            Path(__file__).resolve().parent / "fixtures/v1/model_responses.json"
        )
        self.model_fixtures = (
            json.loads(model_fixture_path.read_text(encoding="utf-8"))
            if model_fixture_path.is_file()
            else {"version": None, "cases": {}}
        )
        from app.agent.function_calling import (
            REQUIRED_BUSINESS_TOOLS,
            SYSTEM_INSTRUCTIONS,
        )
        from app.mcp_server.client import INPUT_SCHEMAS

        schema_payload = {
            name: INPUT_SCHEMAS[name].model_json_schema()
            for name in sorted(REQUIRED_BUSINESS_TOOLS)
        }
        self.metadata = {
            "provider": settings.model_provider
            if mode == "online"
            else "offline_fixture",
            "model": settings.model_name
            if mode == "online"
            else "offline-response-fixtures",
            "system_prompt_sha256": hashlib.sha256(
                SYSTEM_INSTRUCTIONS.encode("utf-8")
            ).hexdigest(),
            "tool_schema_sha256": hashlib.sha256(
                json.dumps(schema_payload, sort_keys=True, ensure_ascii=False).encode(
                    "utf-8"
                )
            ).hexdigest(),
            "tool_names": sorted(REQUIRED_BUSINESS_TOOLS),
            "model_configuration": (
                settings.model_configuration_state
                if mode == "online"
                else "not_used_offline"
            ),
            "offline_response_fixture_version": self.model_fixtures.get("version"),
        }

    async def execute_case(self, case: dict, *, mode: str) -> dict:
        if mode != self.mode:
            raise ValueError("评测执行器模式与服务模式不一致")
        actions = [action.get("kind") for action in case.get("actions", [])]
        if actions != ["create_task"]:
            raise CaseSkipped("EVAL_ACTION_NOT_SUPPORTED")
        if case.get("input", {}).get("kind") != "text":
            raise CaseSkipped("EVAL_FILE_INPUT_NOT_SUPPORTED")
        if mode == "offline":
            fixture = self.model_fixtures.get("cases", {}).get(case["id"])
            if fixture is None:
                raise CaseSkipped("EVAL_OFFLINE_MODEL_FIXTURE_UNAVAILABLE")
            if self.model_fixtures.get("version") != self.manifest.get(
                "input_fixture_version"
            ):
                raise ValueError("离线模型响应夹具版本与数据集输入版本不一致")
            model_client_factory = lambda _config: FixtureModelClient(
                case_id=case["id"], fixture=fixture
            )
            return await self._execute_task(
                case,
                model_client_factory=model_client_factory,
                model_execution_mode="OFFLINE",
            )
        return await self._execute_task(
            case, model_client_factory=None, model_execution_mode="LIVE"
        )

    async def _execute_task(
        self, case: dict, *, model_client_factory, model_execution_mode: str
    ) -> dict:
        from app.database import async_session_factory, engine
        from app.models import (
            Contract,
            ContractBalance,
            Draft,
            DraftItem,
            FieldEvidence,
            Material,
            Order,
            QuotaOccupation,
            TaskApproval,
            TaskCommand,
            TraceSpan,
        )
        from app.worker.queue import PersistentTaskWorker
        from app.worker.workflow import create_run_task_handler

        password = self.settings.demo_password
        if password is None or not password.get_secret_value():
            raise ValueError("隔离演示账号密码未配置")
        actor = self.fixtures["actors"].get(case["actor_fixture"])
        contract_fixture = self.fixtures["contracts"].get(case["contract_fixture"])
        if not isinstance(actor, dict) or not isinstance(contract_fixture, dict):
            raise TypeError("案例引用的账号或合同夹具不存在")
        if actor.get("role") not in {"operator", "admin"}:
            raise CaseSkipped("EVAL_ACTION_REQUIRES_HUMAN_APPROVER")
        order_type = contract_fixture["direction"].upper()
        input_text = case["input"].get("value")
        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("在线案例缺少文本输入")

        headers = {"Origin": self.settings.app_origin or "http://localhost"}
        async with httpx.AsyncClient(
            base_url=self.api_url,
            timeout=httpx.Timeout(self.settings.model_timeout_seconds + 30),
            headers=headers,
        ) as client:
            health = await client.get("/health/ready")
            health.raise_for_status()
            login_response = await client.post(
                "/api/v1/auth/login",
                json={"login": actor["login"], "password": password.get_secret_value()},
            )
            login_response.raise_for_status()
            login_data = login_response.json()["data"]
            client.headers["X-CSRF-Token"] = login_data["csrf_token"]

            contract_number = contract_fixture["number"]
            async with async_session_factory() as session:
                contract = await session.scalar(
                    select(Contract).where(Contract.number == contract_number)
                )
                if contract is None:
                    raise ValueError(f"隔离数据库缺少合同夹具 {contract_number}")
                balance = await session.get(ContractBalance, contract.id)
                occupied_before = (
                    balance.occupied_amount if balance is not None else Decimal("0.00")
                )

            create_response = await client.post(
                "/api/v1/tasks",
                headers={"Idempotency-Key": f"eval-{uuid4().hex}"},
                json={"order_type": order_type, "input": {"text": input_text}},
            )
            create_response.raise_for_status()
            task_id = create_response.json()["data"]["task_id"]
            command_id = create_response.json()["data"]["command_id"]

            # API 使用当前时钟建任务；仅在隔离库内把 START 命令固定到清单时钟。
            fixed_time = datetime.fromisoformat(case["fixed_time"])
            from uuid import UUID

            task_uuid = UUID(task_id)
            async with async_session_factory() as session, session.begin():
                command = await session.get(TaskCommand, UUID(command_id))
                if command is None or command.task_id != task_uuid:
                    raise RuntimeError("API 已接受任务，但找不到对应 START 命令")
                command.payload = {
                    **command.payload,
                    "input_at": fixed_time.isoformat(),
                }

            handler = await create_run_task_handler(
                session_factory=async_session_factory,
                config=self.settings,
                model_client_factory=model_client_factory,
                model_execution_mode=model_execution_mode,
            )
            worker = PersistentTaskWorker(
                engine=engine,
                session_factory=async_session_factory,
                handlers={"RUN_TASK": handler},
                worker_id=f"eval-{uuid4().hex[:20]}",
                poll_seconds=0.01,
            )
            if not await worker.run_once():
                raise RuntimeError("隔离评测 Worker 未领取新建任务")

            task_response = await client.get(f"/api/v1/tasks/{task_id}")
            task_response.raise_for_status()
            task_data = task_response.json()["data"]
            trace_response = await client.get(
                f"/api/v1/tasks/{task_id}/trace", params={"limit": 100}
            )
            trace_response.raise_for_status()
            trace_data = trace_response.json()["data"]

        async with async_session_factory() as session:
            orders = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.task_id == task_uuid)
                )
                or 0
            )
            approvals = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskApproval)
                    .where(TaskApproval.task_id == task_uuid)
                )
                or 0
            )
            occupations = int(
                await session.scalar(
                    select(func.count())
                    .select_from(QuotaOccupation)
                    .where(QuotaOccupation.task_id == task_uuid)
                )
                or 0
            )
            balance = await session.get(ContractBalance, contract.id)
            occupied_after = (
                balance.occupied_amount if balance is not None else Decimal("0.00")
            )
            trace_rows = list(
                (
                    await session.scalars(
                        select(TraceSpan)
                        .where(TraceSpan.task_id == task_uuid)
                        .order_by(TraceSpan.start_at, TraceSpan.id)
                    )
                ).all()
            )
            candidate = (task_data.get("draft") or {}).get("candidate") or {}
            draft = await session.scalar(
                select(Draft).where(Draft.task_id == task_uuid)
            )
            persisted_items = []
            evidence_rows = []
            if draft is not None and draft.current_version > 0:
                persisted_items = list(
                    (
                        await session.scalars(
                            select(DraftItem)
                            .where(
                                DraftItem.draft_id == draft.id,
                                DraftItem.version == draft.current_version,
                            )
                            .order_by(DraftItem.line_no)
                        )
                    ).all()
                )
                evidence_rows = list(
                    (
                        await session.scalars(
                            select(FieldEvidence).where(
                                FieldEvidence.draft_id == draft.id,
                                FieldEvidence.version == draft.current_version,
                            )
                        )
                    ).all()
                )
            draft_fields = await _normalize_draft_fields(
                session,
                candidate,
                Material,
                Contract,
                self.fixtures,
                persisted_items=persisted_items,
                evidence_rows=evidence_rows,
            )

        last_error = task_data.get("last_error")
        return {
            "task": {
                "state": task_data["state"],
                "error_code": last_error.get("code")
                if isinstance(last_error, dict)
                else None,
            },
            "draft": {
                "fields": draft_fields,
                "issues": (task_data.get("draft") or {}).get("issues", []),
                "validated": (task_data.get("draft") or {}).get("validated"),
            },
            "active_wait": task_data.get("active_wait"),
            "trace": {
                "model_tool_names": _model_selected_tools(trace_rows),
                "trace_id": str(trace_rows[0].trace_id) if trace_rows else None,
                "spans": trace_data.get("spans", []),
            },
            "database": {
                "orders": orders,
                "approvals": approvals,
                "quota_occupations": occupations,
                "occupied_amount_delta": f"{occupied_after - occupied_before:.2f}",
            },
            "task_id": task_id,
            "trace_id": str(trace_rows[0].trace_id) if trace_rows else None,
        }


async def _normalize_draft_fields(
    session,
    candidate: dict,
    Material,
    Contract,
    fixtures: dict,
    *,
    persisted_items=(),
    evidence_rows=(),
) -> dict:
    from uuid import UUID

    fields: dict[str, object] = {}
    if candidate.get("order_type"):
        fields["order_type"] = candidate["order_type"].lower()
    contract_id = candidate.get("contract_id")
    if contract_id:
        contract = await session.get(Contract, UUID(contract_id))
        if contract is not None:
            fields["contract_number"] = contract.number
    items = persisted_items or candidate.get("items") or []
    fields["line_count"] = len(items)
    if items:
        item = items[0]
        get_item_value = (
            (lambda key: getattr(item, key))
            if not isinstance(item, dict)
            else (lambda key: item.get(key))
        )
        material_id = get_item_value("material_id")
        if material_id:
            material = await session.get(
                Material,
                UUID(str(material_id))
                if not isinstance(material_id, UUID)
                else material_id,
            )
            if material is not None:
                fixture_material = next(
                    (
                        item
                        for item in fixtures.get("materials", [])
                        if item.get("source_code") == material.code
                    ),
                    None,
                )
                fields["material_id"] = (
                    fixture_material["id"] if fixture_material else material.code
                )
        for source, target in (
            ("specification", "spec"),
            ("quantity", "quantity"),
            ("unit", "unit"),
            ("unit_price", "unit_price"),
        ):
            if get_item_value(source) is not None:
                value = get_item_value(source)
                if target == "spec":
                    fixture_material = next(
                        (
                            material_fixture
                            for material_fixture in fixtures.get("materials", [])
                            if material_fixture.get("id") == fields.get("material_id")
                        ),
                        None,
                    )
                    if (
                        fixture_material is not None
                        and fixture_material.get("source_spec") == value
                    ):
                        value = fixture_material["spec"]
                if target == "quantity":
                    value = format(
                        Decimal(str(value)).quantize(Decimal("0.001")), ".3f"
                    )
                elif target == "unit_price":
                    value = format(Decimal(str(value)).quantize(Decimal("0.01")), ".2f")
                fields[target] = value
        price_evidence = next(
            (row for row in evidence_rows if row.field_path == "items[0].unit_price"),
            None,
        )
        if (
            price_evidence is not None
            and price_evidence.source_type == "MASTER_DATA"
            and isinstance(price_evidence.locator, dict)
            and price_evidence.locator.get("contract_line_id")
        ):
            fields["price_source"] = "CONTRACT"
    if isinstance(candidate.get("business_fields"), dict):
        fields.update(candidate["business_fields"])
    if candidate.get("delivery_date"):
        fields["delivery_date"] = candidate["delivery_date"]
    return fields


def create_executor(*, mode: str, manifest: dict, seed: int) -> BackendCaseExecutor:
    """CLI 默认执行器；所有实际业务副作用都限制在隔离 Compose 数据域。"""
    return BackendCaseExecutor(mode=mode, manifest=manifest, seed=seed)


def _model_selected_tools(trace_rows) -> list[str]:
    allowed = {
        "query_customer",
        "match_material",
        "get_contract",
        "check_contract_balance",
        "save_order_draft",
    }
    selected = []
    for span in trace_rows:
        if span.kind != "MODEL" or not isinstance(span.output_ref, dict):
            continue
        for call in span.output_ref.get("tool_calls", []):
            if isinstance(call, dict) and call.get("name") in allowed:
                selected.append(call["name"])
    return selected


class FixtureModelClient:
    """从独立版本化响应夹具回放工具选择，不从 expected 断言构造模型输出。"""

    def __init__(self, *, case_id: str, fixture: dict) -> None:
        self.case_id = case_id
        self.fixture = fixture
        self.round_index = 0

    async def create_response(self, *, instructions, input_items, tools):
        del instructions, tools
        from app.agent.model_client import ModelResponse

        rounds = self.fixture.get("rounds", [])
        if self.round_index < len(rounds):
            output = tuple(
                {
                    "type": "function_call",
                    "id": f"fc_eval_{self.case_id}_{self.round_index}_{index}",
                    "call_id": f"call_eval_{self.case_id}_{self.round_index}_{index}",
                    "name": call["name"],
                    "arguments": json.dumps(
                        _resolve_template(call["arguments"], input_items),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                }
                for index, call in enumerate(rounds[self.round_index], start=1)
            )
            output_text = ""
        else:
            output_text = self.fixture.get("final_text", "离线评测案例执行完成。")
            output = (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": output_text}],
                },
            )
        self.round_index += 1
        return ModelResponse(
            response_id=f"eval-{self.case_id}-{self.round_index}",
            model="offline-fixture-v1",
            output=output,
            output_text=output_text,
            usage=None,
            attempts=1,
        )


def _resolve_template(value, input_items):
    call_names = {}
    outputs = {}
    for item in input_items:
        if item.get("type") == "function_call":
            call_names[item.get("call_id")] = item.get("name")
        elif item.get("type") == "function_call_output":
            name = call_names.get(item.get("call_id"))
            if name:
                outputs[name] = json.loads(item["output"])

    if isinstance(value, dict) and set(value) == {"$ref"}:
        resolved = outputs
        for part in value["$ref"].split("."):
            resolved = (
                resolved[int(part)] if isinstance(resolved, list) else resolved[part]
            )
        return resolved
    if isinstance(value, dict):
        return {
            key: _resolve_template(item, input_items) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_template(item, input_items) for item in value]
    return value
