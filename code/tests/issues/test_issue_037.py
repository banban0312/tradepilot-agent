"""M6-02 评测服务契约、筛选、安全路径与 CLI 报告。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.worker.workflow import DatabaseWorkflowServices

from evals.runner import main
from evals.service import (
    CaseResult,
    CaseSkipped,
    EvaluationService,
    evaluate_assertions,
    load_dataset,
    select_cases,
    summarize_results,
    write_report,
)


class Executor:
    def __init__(self, outcomes: dict[str, dict | Exception]):
        self.outcomes = outcomes

    async def execute_case(self, case: dict, *, mode: str) -> dict:
        result = self.outcomes[case["id"]]
        if isinstance(result, Exception):
            raise result
        return result


def _case(case_id: str, *, category: str = "normal") -> dict:
    return {
        "id": case_id,
        "category": category,
        "expected": {
            "assertions": [
                {"source": "task.state", "operator": "eq", "expected": "WAITING_INPUT"}
            ]
        },
    }


@pytest.mark.asyncio
async def test_service_separates_pass_fail_skip_and_error() -> None:
    cases = [
        _case("pass"),
        _case("fail"),
        _case("skip"),
        _case("error"),
    ]
    results = await EvaluationService(
        Executor(
            {
                "pass": {"task": {"state": "WAITING_INPUT"}},
                "fail": {"task": {"state": "DONE"}},
                "skip": CaseSkipped("MCP_UNAVAILABLE"),
                "error": RuntimeError("broken fixture"),
            }
        ),
        mode="offline",
    ).run(cases)

    assert [result.status for result in results] == ["PASS", "FAIL", "SKIPPED", "ERROR"]
    assert results[1].failed_assertions[0]["actual"] == "DONE"
    assert results[2].error_code == "MCP_UNAVAILABLE"
    assert results[3].actual == {"exception_type": "RuntimeError"}
    assert summarize_results(results)["exit_code"] == 1


@pytest.mark.asyncio
async def test_offline_fixture_replays_a_real_model_timeout_error() -> None:
    from app.agent.model_client import ModelProviderError

    from evals.backend_executor import FixtureModelClient

    client = FixtureModelClient(
        case_id="abnormal-model-timeout",
        fixture={
            "error": {
                "code": "MODEL_TIMEOUT",
                "retryable": True,
                "attempts": 3,
            }
        },
    )

    with pytest.raises(ModelProviderError) as captured:
        await client.create_response(
            instructions="指令", input_items=[{"type": "message"}], tools=[]
        )

    assert captured.value.code == "MODEL_TIMEOUT"
    assert captured.value.retryable is True
    assert captured.value.attempts == 3


def test_dataset_loading_rejects_traversal_and_selection_rejects_missing_case() -> None:
    root = Path(__file__).resolve().parents[2] / "evals"
    manifest, cases = load_dataset(root, "v1")
    assert manifest["case_count"] == 41
    first_case = next(case for case in cases if case["id"] == "normal-complete-sales")
    assert first_case["contract_fixture"] == "sales_north_default"
    assert "TP-DEMO-SALES-N1" in first_case["input"]["value"]
    multi_line_case = next(
        case for case in cases if case["id"] == "normal-multiple-lines"
    )
    assert multi_line_case["contract_fixture"] == "sales_north_multi_line"
    assert multi_line_case["expected"]["fields"]["line_count"] == 2
    assert len(multi_line_case["expected"]["fields"]["line_details"]) == 2
    fixtures = json.loads(
        (root / "fixtures/v1/master_data.json").read_text(encoding="utf-8")
    )
    assert len(fixtures["contracts"]["sales_north_multi_line"]["lines"]) == 2
    similar_customer_case = next(
        case for case in cases if case["id"] == "ambiguity-similar-customer"
    )
    assert similar_customer_case["customer_fixtures"] == ["customer_north_similar"]
    assert fixtures["customers"]["customer_north_similar"]["department"] == "north"
    mcp_failure_case = next(
        case for case in cases if case["id"] == "abnormal-mcp-unavailable"
    )
    assert mcp_failure_case["simulate_mcp_unavailable"] is True
    assert len(select_cases(cases, case_ids={cases[0]["id"]}, categories=None)) == 1
    with pytest.raises(ValueError, match="版本"):
        load_dataset(root, "..")
    with pytest.raises(ValueError, match="找不到"):
        select_cases(cases, case_ids={"missing-case"}, categories=None)


def test_backend_rule_date_uses_fixed_task_time_instead_of_wall_clock() -> None:
    services = DatabaseWorkflowServices.__new__(DatabaseWorkflowServices)
    services.config = SimpleNamespace(business_timezone="Asia/Shanghai")

    assert services._business_date({"input_at": "2026-10-16T09:00:00+08:00"}) == (
        date(2026, 10, 16)
    )


def test_assertions_require_exact_values_and_report_atomic_artifacts(
    tmp_path: Path,
) -> None:
    expected = {
        "assertions": [
            {"source": "database.orders", "operator": "eq", "expected": 0},
            {
                "source": "trace.tools",
                "operator": "contains_all",
                "expected": ["get_contract"],
            },
        ]
    }
    assert (
        evaluate_assertions(
            expected, {"database": {"orders": 0}, "trace": {"tools": ["get_contract"]}}
        )
        == []
    )
    failures = evaluate_assertions(
        expected, {"database": {"orders": 1}, "trace": {"tools": []}}
    )
    assert len(failures) == 2
    malformed = {
        "assertions": [
            {
                "source": "trace.tools",
                "operator": "contains_all",
                "expected": [["not", "hashable"]],
            }
        ]
    }
    assert evaluate_assertions(malformed, {"trace": {"tools": ["anything"]}})

    results = [
        # 安全类别被标为失败时，汇总必须失败。
        CaseResult(
            case_id="unauthorized-1",
            category="unauthorized",
            status="FAIL",
            expected=expected,
            failed_assertions=failures,
        )
    ]
    summary = write_report(
        tmp_path,
        metadata={"mode": "offline", "dataset_version": "1.0.0"},
        results=results,
    )
    assert summary["safe_invariants_passed"] is False
    assert summary["exit_code"] == 1
    assert {path.name for path in tmp_path.iterdir()} == {
        "summary.json",
        "cases.jsonl",
        "report.md",
    }
    assert (
        json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))[
            "status_counts"
        ]["FAIL"]
        == 1
    )
    assert "unauthorized-1" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_cli_writes_report_and_returns_nonzero_for_failed_assertion(
    tmp_path: Path, monkeypatch
) -> None:
    evals_root = tmp_path / "evals"
    dataset = evals_root / "datasets" / "v1"
    dataset.mkdir(parents=True)
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_version": "1.0.0",
                "master_data_version": "1.0.0",
                "input_fixture_version": "1.0.0",
                "case_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (dataset / "cases.jsonl").write_text(
        json.dumps({**_case("single"), "execution_modes": ["offline"]}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "evals.runner._load_factory",
        lambda _: lambda **kwargs: Executor({"single": {"task": {"state": "DONE"}}}),
    )
    report_dir = tmp_path / "reports" / "run-1"
    exit_code = main(
        [
            "--mode",
            "offline",
            "--dataset",
            "v1",
            "--dataset-root",
            str(evals_root),
            "--executor-factory",
            "test:factory",
            "--report-dir",
            str(report_dir),
            "--case",
            "single",
        ]
    )
    assert exit_code == 1
    report = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    assert report["selected_case_ids"] == ["single"]
    assert report["status_counts"]["FAIL"] == 1


def test_cli_reports_missing_executor_as_error_without_marking_cases_pass(
    tmp_path: Path,
) -> None:
    evals_root = tmp_path / "evals"
    dataset = evals_root / "datasets" / "v1"
    dataset.mkdir(parents=True)
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_version": "1.0.0",
                "master_data_version": "1.0.0",
                "input_fixture_version": "1.0.0",
                "case_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (dataset / "cases.jsonl").write_text(
        json.dumps({**_case("single"), "execution_modes": ["online"]}) + "\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports" / "missing-adapter"
    exit_code = main(
        [
            "--mode",
            "online",
            "--dataset",
            "v1",
            "--dataset-root",
            str(evals_root),
            "--executor-factory",
            "evals.module_that_does_not_exist:create_executor",
            "--report-dir",
            str(report_dir),
            "--case",
            "single",
        ]
    )
    assert exit_code == 1
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    row = json.loads((report_dir / "cases.jsonl").read_text(encoding="utf-8"))
    assert summary["status_counts"]["ERROR"] == 1
    assert summary["status_counts"]["PASS"] == 0
    assert row["error_code"] == "EVAL_EXECUTOR_CONFIGURATION_ERROR"


def test_backend_executor_refuses_to_touch_non_sandbox_environment(monkeypatch) -> None:
    from evals.backend_executor import BackendCaseExecutor

    monkeypatch.delenv("TRADEPILOT_EVAL_SANDBOX", raising=False)
    with pytest.raises(ValueError, match="隔离"):
        BackendCaseExecutor(mode="offline", manifest={}, seed=1)


@pytest.mark.asyncio
async def test_offline_model_fixture_uses_real_tool_outputs_without_expected_values() -> (
    None
):
    from evals.backend_executor import FixtureModelClient

    fixture = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "evals/fixtures/v1/model_responses.json"
        ).read_text(encoding="utf-8")
    )
    client = FixtureModelClient(
        case_id="normal-complete-sales",
        fixture=fixture["cases"]["normal-complete-sales"],
    )
    first = await client.create_response(
        instructions="fixture",
        input_items=[{"role": "user", "content": "case input"}],
        tools=[],
    )
    assert [call["name"] for call in first.output] == [
        "get_contract",
        "match_material",
    ]

    history = [
        *first.output,
        {
            "type": "function_call_output",
            "call_id": first.output[0]["call_id"],
            "output": json.dumps(
                {
                    "data": {
                        "id": "00000000-0000-4000-8000-000000000050",
                        "customer_id": "00000000-0000-4000-8000-000000000010",
                        "project_id": "00000000-0000-4000-8000-000000000020",
                        "delivery_address": "隔离示例地址",
                        "lines": [{"unit_price": "3500.000000"}],
                    }
                }
            ),
        },
        {
            "type": "function_call_output",
            "call_id": first.output[1]["call_id"],
            "output": json.dumps(
                {
                    "data": {
                        "candidates": [
                            {"material_id": "00000000-0000-4000-8000-000000000030"}
                        ]
                    }
                }
            ),
        },
    ]
    second = await client.create_response(
        instructions="fixture", input_items=history, tools=[]
    )
    save_arguments = json.loads(second.output[0]["arguments"])
    assert save_arguments["candidate_order"]["contract_id"].endswith("000000000050")
    assert save_arguments["candidate_order"]["items"][0]["material_id"].endswith(
        "000000000030"
    )
    assert (
        save_arguments["candidate_order"]["items"][0]["specification"] == "HRB400 12mm"
    )
    assert save_arguments["candidate_order"]["items"][0]["unit_price"] == "3500.000000"


@pytest.mark.asyncio
async def test_contract_price_fallback_is_server_sourced_and_auditable() -> None:
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import UUID

    from evals.backend_executor import _BACKEND_ROOT

    assert _BACKEND_ROOT.is_dir()
    from app.drafts.service import _fill_contract_prices
    from app.drafts.types import (
        CandidateOrder,
        CandidateOrderItem,
        FieldEvidenceInput,
        FieldSource,
    )

    contract_id = UUID("00000000-0000-4000-8000-000000000050")
    material_id = UUID("00000000-0000-4000-8000-000000000030")
    line_id = UUID("00000000-0000-4000-8000-000000000060")
    session = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(id=line_id, unit_price=Decimal("3500.000000"))
        )
    )
    candidate = CandidateOrder(
        contract_id=contract_id,
        items=(
            CandidateOrderItem(
                material_id=material_id,
                specification="HRB400 12mm",
                quantity=Decimal(2),
                unit="吨",
                unit_price=None,
            ),
        ),
        evidence=(
            FieldEvidenceInput(
                field_path="items[0].unit_price",
                source_type=FieldSource.MASTER_DATA,
                accepted_value="9999.000000",
            ),
        ),
    )

    saved_candidate = await _fill_contract_prices(session, candidate)

    assert saved_candidate.items[0].unit_price == Decimal("3500.000000")
    evidence = next(
        item
        for item in saved_candidate.evidence
        if item.field_path == "items[0].unit_price"
    )
    assert evidence.source_type is FieldSource.MASTER_DATA
    assert evidence.locator == {"contract_line_id": str(line_id)}
    assert evidence.accepted_value == "3500.000000"
    assert (
        sum(
            item.field_path == "items[0].unit_price"
            for item in saved_candidate.evidence
        )
        == 1
    )


def test_selected_tool_trace_includes_schema_rejected_model_calls() -> None:
    from types import SimpleNamespace

    from evals.backend_executor import _model_selected_tools

    rows = [
        SimpleNamespace(kind="TOOL", name="mcp.list_tools", output_ref={}),
        SimpleNamespace(
            kind="MODEL",
            name="model.function_calling",
            output_ref={"tool_calls": [{"name": "get_contract"}]},
        ),
        SimpleNamespace(kind="RULE", name="tool_argument_validation", output_ref={}),
    ]

    assert _model_selected_tools(rows) == ["get_contract"]
