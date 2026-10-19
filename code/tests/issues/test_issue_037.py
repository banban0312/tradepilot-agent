"""M6-02 评测服务契约、筛选、安全路径与 CLI 报告。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_dataset_loading_rejects_traversal_and_selection_rejects_missing_case() -> None:
    root = Path(__file__).resolve().parents[2] / "evals"
    manifest, cases = load_dataset(root, "v1")
    assert manifest["case_count"] == 41
    assert len(select_cases(cases, case_ids={cases[0]["id"]}, categories=None)) == 1
    with pytest.raises(ValueError, match="版本"):
        load_dataset(root, "..")
    with pytest.raises(ValueError, match="找不到"):
        select_cases(cases, case_ids={"missing-case"}, categories=None)


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
