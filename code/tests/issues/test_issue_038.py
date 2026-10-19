from __future__ import annotations

import json
from pathlib import Path

from evals.service import CaseResult, summarize_results, write_report


def test_metrics_reduce_field_coverage_and_exact_match_for_missing_values() -> None:
    result = CaseResult(
        case_id="field-omission",
        category="quota",
        status="FAIL",
        expected={
            "fields": {"quantity": "2.000", "unit": "吨"},
            "required_tools": ["get_contract"],
            "allowed_tools": ["get_contract"],
            "task_state": "BLOCKED_RULE",
        },
        input={"kind": "file", "file_type": "application/pdf"},
        actual={
            "draft": {
                "fields": {"quantity": "2.000"},
                "field_evidence": [
                    {"field_path": "items[0].quantity", "source_type": "USER_TEXT"}
                ],
            },
            "trace": {
                "model_tool_names": ["get_contract"],
                "spans": [
                    {"kind": "MODEL", "duration_ms": 10, "usage": None},
                    {
                        "kind": "TOOL",
                        "name": "get_contract",
                        "status": "OK",
                        "duration_ms": 4,
                    },
                ],
            },
        },
        duration_ms=17,
        tags=["recovery"],
    )

    summary = summarize_results([result])
    metrics = summary["metrics"]

    assert metrics["field_exact_match"] == {"correct": 1, "required": 2, "rate": 0.5}
    assert metrics["field_coverage"] == {"present": 1, "required": 2, "rate": 0.5}
    assert metrics["field_by_source"]["USER_TEXT"]["required"] == 1
    assert metrics["field_by_file_type"]["application/pdf"]["coverage_rate"] == 0.5
    assert metrics["tool_selection"]["rate"] == 1.0
    assert metrics["tool_execution"]["rate"] == 1.0
    assert metrics["rule_blocking"]["rate"] == 0.0
    assert metrics["recovery"]["rate"] == 0.0
    assert metrics["tokens"]["total"] is None
    assert metrics["tokens"]["coverage"] == 0.0
    assert metrics["duration_ms"]["model_span_total"] == 10
    assert metrics["duration_ms"]["tool_span_total"] == 4
    assert summary["category_counts"]["quota"]["applicable"] == 1
    assert summary["safe_invariants_passed"] is False
    assert summary["exit_code"] == 1


def test_reports_match_and_redact_secrets(tmp_path: Path) -> None:
    result = CaseResult(
        case_id="secret-safe",
        category="unauthorized",
        status="PASS",
        expected={"fields": {}, "assertions": []},
        input={
            "kind": "text",
            "value": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789 Bearer auth-token-value",
        },
        actual={"api_key": "should-not-leak", "task": {"state": "BLOCKED_RULE"}},
        task_id="task-ref",
        trace_id="trace-ref",
        duration_ms=8,
    )
    summary = write_report(
        tmp_path,
        metadata={
            "mode": "online",
            "dataset_version": "1.0.0",
            "code_version": "abc123",
            "execution_metadata": {"model": "test", "authorization": "Bearer secret"},
        },
        results=[result],
    )

    json_summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    case_row = json.loads((tmp_path / "cases.jsonl").read_text(encoding="utf-8"))
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert json_summary["metrics"] == summary["metrics"]
    assert json_summary["status_counts"]["PASS"] == 1
    assert case_row["task_id"] == "task-ref"
    assert "trace-ref" in markdown
    assert "预期：" in markdown and "实际摘要：" in markdown
    assert "safe_invariants_passed" in json_summary
    for text in (json.dumps(json_summary), json.dumps(case_row), markdown):
        assert "should-not-leak" not in text
        assert "abcdefghijklmnopqrstuvwxyz0123456789" not in text
        assert "auth-token-value" not in text
        assert "Bearer secret" not in text
    assert json_summary["execution_metadata"]["authorization"] == "[已脱敏]"
