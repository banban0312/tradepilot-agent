"""数据集读取、结果断言与运行汇总；执行器必须返回真实观测值。"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


class CaseSkipped(RuntimeError):
    """执行依赖不可用或场景未支持；不能按通过计数。"""


class CaseExecutor(Protocol):
    async def execute_case(
        self, case: dict[str, Any], *, mode: str
    ) -> dict[str, Any]: ...


_EXTRACTED_FIELD_NAMES = frozenset(
    {
        "order_type",
        "contract_number",
        "customer_id",
        "customer_name",
        "project_id",
        "project_name",
        "delivery_address",
        "contact",
        "delivery_date",
        "material_id",
        "spec",
        "quantity",
        "unit",
        "unit_price",
        "amount",
        "price_source",
        "line_material_ids",
        "line_details",
    }
)


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    status: str
    expected: dict[str, Any]
    input: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    failed_assertions: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    duration_ms: int | None = None
    tags: list[str] = field(default_factory=list)


def load_dataset(dataset_root: Path, dataset_version: str) -> tuple[dict, list[dict]]:
    """只接受明确版本目录及 JSON Lines 案例文件。"""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", dataset_version):
        raise ValueError("数据集版本只能包含小写字母、数字、点、横线和下划线")
    version_root = dataset_root / "datasets" / dataset_version
    if not version_root.resolve().is_relative_to((dataset_root / "datasets").resolve()):
        raise ValueError("数据集路径必须位于 datasets 目录内")
    manifest = json.loads((version_root / "manifest.json").read_text(encoding="utf-8"))
    cases = [
        json.loads(line)
        for line in (version_root / "cases.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    declared_version = manifest.get("dataset_version")
    directory_alias_matches = (
        dataset_version.startswith("v")
        and dataset_version[1:].isdigit()
        and isinstance(declared_version, str)
        and declared_version.split(".", 1)[0] == dataset_version[1:]
    )
    if declared_version != dataset_version and not directory_alias_matches:
        raise ValueError("清单中的数据版本与请求版本不一致")
    if manifest.get("case_count") != len(cases):
        raise ValueError("清单案例数与 JSONL 实际行数不一致")
    identifiers = [case.get("id") for case in cases]
    if len(set(identifiers)) != len(identifiers) or not all(identifiers):
        raise ValueError("案例 ID 缺失或重复")
    return manifest, cases


def select_cases(
    cases: list[dict], *, case_ids: set[str] | None, categories: set[str] | None
) -> list[dict]:
    selected = [
        case
        for case in cases
        if (case_ids is None or case["id"] in case_ids)
        and (categories is None or case["category"] in categories)
    ]
    if case_ids and {case["id"] for case in selected} != case_ids:
        missing = sorted(case_ids - {case["id"] for case in selected})
        raise ValueError(f"找不到指定案例：{', '.join(missing)}")
    if categories and not selected:
        raise ValueError("指定分类没有可运行案例")
    if not selected:
        raise ValueError("筛选结果为空")
    return selected


def evaluate_assertions(expected: dict, actual: dict) -> list[dict[str, Any]]:
    failed = []
    for assertion in expected["assertions"]:
        source = assertion["source"]
        observed = _resolve_source(actual, source)
        target = assertion["expected"]
        operator = assertion["operator"]
        try:
            passed = (
                observed == target
                if operator == "eq"
                else isinstance(observed, list | tuple | set)
                and isinstance(target, list | tuple | set)
                and set(target).issubset(set(observed))
                if operator == "contains_all"
                else False
            )
        except TypeError:
            # 不可哈希的损坏断言值应作为失败定位，不能中断整批报告生成。
            passed = False
        if not passed:
            failed.append(
                {
                    "source": source,
                    "operator": operator,
                    "expected": target,
                    "actual": observed,
                }
            )
    return failed


def _resolve_source(actual: dict, source: str) -> Any:
    value: Any = actual
    for key in source.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


class EvaluationService:
    """串行运行固定案例并严格区分通过、失败、跳过和执行错误。"""

    def __init__(self, executor: CaseExecutor, *, mode: str) -> None:
        if mode not in {"offline", "online"}:
            raise ValueError("评测模式必须是 offline 或 online")
        self.executor = executor
        self.mode = mode

    async def run(
        self,
        cases: list[dict],
        *,
        on_case_completed: Callable[[int, int, CaseResult], None] | None = None,
    ) -> list[CaseResult]:
        results = []

        def record(result: CaseResult) -> None:
            results.append(result)
            if on_case_completed is not None:
                on_case_completed(len(results), len(cases), result)

        for case in cases:
            started = time.perf_counter()
            expected = case["expected"]
            try:
                actual = await self.executor.execute_case(case, mode=self.mode)
            except CaseSkipped as exc:
                record(
                    CaseResult(
                        case_id=case["id"],
                        category=case["category"],
                        status="SKIPPED",
                        expected=expected,
                        input=case.get("input", {}),
                        error_code=str(exc) or "EVAL_DEPENDENCY_UNAVAILABLE",
                        duration_ms=_elapsed_ms(started),
                        tags=case.get("tags", []),
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - each case must leave a report row.
                actual = {"exception_type": type(exc).__name__}
                diagnostic = getattr(exc, "evaluation_stage", None)
                if isinstance(diagnostic, str) and len(diagnostic) <= 2000:
                    actual["diagnostic"] = diagnostic
                record(
                    CaseResult(
                        case_id=case["id"],
                        category=case["category"],
                        status="ERROR",
                        expected=expected,
                        input=case.get("input", {}),
                        error_code=str(getattr(exc, "code", "EVAL_EXECUTION_ERROR")),
                        actual=actual,
                        duration_ms=_elapsed_ms(started),
                        tags=case.get("tags", []),
                    )
                )
                continue
            try:
                failed = evaluate_assertions(expected, actual)
            except Exception as exc:  # noqa: BLE001 - malformed assertions are case errors.
                record(
                    CaseResult(
                        case_id=case["id"],
                        category=case["category"],
                        status="ERROR",
                        expected=expected,
                        input=case.get("input", {}),
                        actual={"exception_type": type(exc).__name__},
                        error_code="EVAL_ASSERTION_ERROR",
                        duration_ms=_elapsed_ms(started),
                        tags=case.get("tags", []),
                    )
                )
                continue
            record(
                CaseResult(
                    case_id=case["id"],
                    category=case["category"],
                    status="FAIL" if failed else "PASS",
                    expected=expected,
                    input=case.get("input", {}),
                    actual=actual,
                    failed_assertions=failed,
                    task_id=actual.get("task_id"),
                    trace_id=actual.get("trace_id"),
                    duration_ms=_elapsed_ms(started),
                    tags=case.get("tags", []),
                )
            )
        return results


def summarize_results(results: list[CaseResult]) -> dict[str, Any]:
    statuses = Counter(result.status for result in results)
    categories: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = categories.setdefault(result.category, Counter())
        bucket[result.status] += 1
    invariant_cases = [
        result
        for result in results
        if result.category in {"unauthorized", "quota", "duplicate_submission"}
    ]
    invariants_passed = all(result.status == "PASS" for result in invariant_cases)
    return {
        "case_count": len(results),
        "status_counts": {
            status: statuses.get(status, 0)
            for status in ("PASS", "FAIL", "SKIPPED", "ERROR")
        },
        "category_counts": {
            category: {
                **{
                    status: counts.get(status, 0)
                    for status in ("PASS", "FAIL", "SKIPPED", "ERROR")
                },
                "applicable": sum(counts.values()),
                "pass_rate": _ratio(counts.get("PASS", 0), sum(counts.values())),
            }
            for category, counts in categories.items()
        },
        "metrics": _quality_metrics(results),
        "safe_invariants": {
            "passed": sum(result.status == "PASS" for result in invariant_cases),
            "applicable": len(invariant_cases),
            "pass_rate": _ratio(
                sum(result.status == "PASS" for result in invariant_cases),
                len(invariant_cases),
            ),
            "status": invariants_passed if invariant_cases else None,
        },
        "safe_invariants_passed": invariants_passed if invariant_cases else None,
        "exit_code": 0
        if results
        and all(result.status == "PASS" for result in results)
        and (not invariant_cases or invariants_passed)
        else 1,
    }


def result_rows(results: list[CaseResult]) -> list[dict[str, Any]]:
    return [asdict(result) for result in results]


def write_report(
    report_dir: Path, *, metadata: dict, results: list[CaseResult]
) -> dict:
    """原子写入便于机器读取及人工复核的三种报告。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    summary = {**_redact(metadata), **summarize_results(results)}
    rows = _redact(result_rows(results))
    _atomic_write(
        report_dir / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(
        report_dir / "cases.jsonl",
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
    )
    lines = [
        "# TradePilot 自动评测报告",
        "",
        f"- 模式：{summary['mode']}",
        f"- 数据集：{summary['dataset_version']}",
        f"- 代码版本：{summary.get('code_version', 'unavailable')}",
        f"- 案例数：{summary['case_count']}",
        "- 结果："
        + "，".join(
            f"{key} {value}" for key, value in summary["status_counts"].items()
        ),
        "",
        "## 指标汇总",
        "",
        *_metrics_markdown(summary["metrics"], summary.get("safe_invariants")),
        "",
        "## 分类结果",
        "",
        "| 分类 | 案例数 | 通过 | 失败 | 跳过 | 错误 | 通过率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        *[
            f"| {category} | {values['applicable']} | {values['PASS']} | {values['FAIL']} | {values['SKIPPED']} | {values['ERROR']} | {_format_ratio(values['pass_rate'])} |"
            for category, values in summary["category_counts"].items()
        ],
        "",
        "## 案例明细",
        "",
    ]
    for row in rows:
        lines.extend([f"### {row['case_id']} — {row['status']}", ""])
        lines.append(f"- 分类：{row['category']}；用时：{row.get('duration_ms')} ms")
        if row.get("task_id") or row.get("trace_id"):
            lines.append(
                f"- 任务：`{row.get('task_id')}`；轨迹：`{row.get('trace_id')}`"
            )
        actual = row.get("actual", {})
        trace = actual.get("trace", {}) if isinstance(actual, dict) else {}
        spans = trace.get("spans", []) if isinstance(trace, dict) else []
        span_ids = [
            span.get("span_id")
            for span in spans
            if isinstance(span, dict) and span.get("span_id")
        ]
        if span_ids:
            lines.append(f"- Span：{', '.join(f'`{span_id}`' for span_id in span_ids)}")
        lines.extend(
            [
                "",
                "输入：",
                "",
                "```json",
                json.dumps(row.get("input", {}), ensure_ascii=False, indent=2),
                "```",
                "",
                "预期：",
                "",
                "```json",
                json.dumps(row.get("expected", {}), ensure_ascii=False, indent=2),
                "```",
                "",
                "实际摘要：",
                "",
                "```json",
                json.dumps(
                    _redact(_actual_summary(actual)), ensure_ascii=False, indent=2
                ),
                "```",
                "",
            ]
        )
        if row["error_code"]:
            lines.extend([f"错误：`{row['error_code']}`", ""])
        if row["failed_assertions"]:
            lines.extend(["失败断言：", ""])
            lines.extend(
                f"- `{item['source']}` 预期 `{item['expected']}`，实际 `{item['actual']}`"
                for item in row["failed_assertions"]
            )
            lines.append("")
    _atomic_write(report_dir / "report.md", "\n".join(lines))
    return summary


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _leaf_values(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        return {
            key: leaf
            for name, item in value.items()
            for key, leaf in _leaf_values(
                item, f"{prefix}.{name}" if prefix else name
            ).items()
        }
    if isinstance(value, list) and value:
        return {
            key: leaf
            for index, item in enumerate(value)
            for key, leaf in _leaf_values(item, f"{prefix}[{index}]").items()
        }
    return {prefix: value} if prefix else {}


def _lookup_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in re.findall(r"[^.\[\]]+", path):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _quality_metrics(results: list[CaseResult]) -> dict[str, Any]:
    evaluated = [result for result in results if result.status in {"PASS", "FAIL"}]
    field_total = field_present = field_correct = 0
    field_details: dict[str, Counter] = {}
    source_details: dict[str, Counter] = {}
    input_kind_details: dict[str, Counter] = {}
    tool_cases = tool_passed = 0
    rule_cases = rule_passed = 0
    idempotency_cases = recovery_cases = 0
    idempotency_passed = recovery_passed = 0
    model_spans = tool_spans = tool_successes = 0
    argument_validation = Counter()
    model_ms = tool_ms = 0
    tokens: dict[str, int] = {"input": 0, "output": 0, "total": 0}
    token_observations = token_unavailable = 0
    for result in results:
        expected = result.expected
        fields = _required_field_leaves(expected)
        if result.status in {"PASS", "FAIL"}:
            actual_fields = result.actual.get("draft", {}).get("fields", {})
            evidence = result.actual.get("draft", {}).get("field_evidence", [])
            evidence_sources: dict[str, set[str]] = {}
            for row in evidence:
                if isinstance(row, dict) and isinstance(row.get("field_path"), str):
                    evidence_sources.setdefault(row["field_path"], set()).add(
                        str(row.get("source_type", "unavailable"))
                    )
            for path, wanted in fields.items():
                field_total += 1
                present, observed = _lookup_path(actual_fields, path)
                correct = present and observed == wanted
                if present:
                    field_present += 1
                field_correct += correct
                path_count = field_details.setdefault(path, Counter())
                path_count["required"] += 1
                path_count["present"] += present
                path_count["correct"] += correct
                source_path = _evidence_path(path)
                sources = evidence_sources.get(source_path, set())
                source = (
                    next(iter(sources))
                    if len(sources) == 1
                    else "multiple"
                    if sources
                    else "unavailable"
                )
                source_count = source_details.setdefault(source, Counter())
                source_count["required"] += 1
                source_count["present"] += present
                source_count["correct"] += correct
                input_kind = str(
                    result.input.get("file_type", result.input.get("kind", "unknown"))
                )
                kind_count = input_kind_details.setdefault(input_kind, Counter())
                kind_count["required"] += 1
                kind_count["present"] += present
                kind_count["correct"] += correct
            required_tools = expected.get("required_tools", [])
            if required_tools:
                tool_cases += 1
                selected = result.actual.get("trace", {}).get("model_tool_names", [])
                allowed = expected.get("allowed_tools", [])
                tool_passed += set(required_tools).issubset(selected) and set(
                    selected
                ).issubset(allowed)
            if expected.get("task_state") == "BLOCKED_RULE":
                rule_cases += 1
                rule_passed += result.status == "PASS"
            if result.category == "duplicate_submission":
                idempotency_cases += 1
                idempotency_passed += result.status == "PASS"
            if "recovery" in result.tags:
                recovery_cases += 1
                recovery_passed += result.status == "PASS"
        trace = (
            result.actual.get("trace", {}) if isinstance(result.actual, dict) else {}
        )
        spans = trace.get("spans", []) if isinstance(trace, dict) else []
        for span in spans:
            if not isinstance(span, dict):
                continue
            kind = span.get("kind")
            duration = span.get("duration_ms")
            if kind == "MODEL":
                model_spans += 1
                if isinstance(duration, int) and not isinstance(duration, bool):
                    model_ms += max(0, duration)
                usage = span.get("usage")
                if isinstance(usage, dict):
                    aliases = {
                        "input": ("input_tokens", "prompt_tokens"),
                        "output": ("output_tokens", "completion_tokens"),
                        "total": ("total_tokens",),
                    }
                    observed = {}
                    for key, names in aliases.items():
                        observed[key] = next(
                            (
                                usage[name]
                                for name in names
                                if isinstance(usage.get(name), int)
                                and not isinstance(usage.get(name), bool)
                            ),
                            None,
                        )
                    if (
                        observed["total"] is None
                        and observed["input"] is not None
                        and observed["output"] is not None
                    ):
                        observed["total"] = observed["input"] + observed["output"]
                    if any(value is not None for value in observed.values()):
                        token_observations += 1
                        for key, value in observed.items():
                            if value is not None:
                                tokens[key] += value
                    else:
                        token_unavailable += 1
                else:
                    token_unavailable += 1
            elif kind == "TOOL" and span.get("name") != "mcp.list_tools":
                tool_spans += 1
                tool_successes += span.get("status") == "OK"
                argument_validation["accepted"] += 1
                if isinstance(duration, int) and not isinstance(duration, bool):
                    tool_ms += max(0, duration)
            elif (
                kind == "RULE"
                and span.get("name") == "function_calling.tool_argument_validation"
            ):
                if span.get("status") != "OK":
                    argument_validation["rejected"] += 1
    field_coverage = _ratio(field_present, field_total)
    return {
        "field_exact_match": {
            "correct": field_correct,
            "required": field_total,
            "rate": _ratio(field_correct, field_total),
        },
        "field_coverage": {
            "present": field_present,
            "required": field_total,
            "rate": field_coverage,
        },
        "field_by_path": _counter_metrics(field_details),
        "field_by_source": _counter_metrics(source_details),
        "field_by_file_type": _counter_metrics(input_kind_details),
        "field_metric_scope": {
            "evaluated_cases": len(evaluated),
            "not_evaluated_cases": sum(
                result.status in {"SKIPPED", "ERROR"} for result in results
            ),
            "not_evaluated_required_fields": sum(
                len(_required_field_leaves(result.expected))
                for result in results
                if result.status in {"SKIPPED", "ERROR"}
            ),
        },
        "tool_selection": {
            "passed": tool_passed,
            "applicable": tool_cases,
            "rate": _ratio(tool_passed, tool_cases),
        },
        "tool_execution": {
            "succeeded": tool_successes,
            "calls": tool_spans,
            "rate": _ratio(tool_successes, tool_spans),
        },
        "tool_parameter_validation": {
            "accepted": argument_validation["accepted"],
            "rejected": argument_validation["rejected"],
            "total": sum(argument_validation.values()),
        },
        "rule_blocking": {
            "passed": rule_passed,
            "applicable": rule_cases,
            "rate": _ratio(rule_passed, rule_cases),
        },
        "idempotency": {
            "passed": idempotency_passed,
            "applicable": idempotency_cases,
            "rate": _ratio(idempotency_passed, idempotency_cases),
        },
        "recovery": {
            "passed": recovery_passed,
            "applicable": recovery_cases,
            "rate": _ratio(recovery_passed, recovery_cases),
        },
        "failed_assertions": dict(
            Counter(
                failure["source"]
                for result in results
                for failure in result.failed_assertions
            )
        ),
        "tokens": {
            "input": tokens["input"] if token_observations else None,
            "output": tokens["output"] if token_observations else None,
            "total": tokens["total"] if token_observations else None,
            "observed_model_spans": token_observations,
            "unavailable_model_spans": token_unavailable,
            "coverage": _ratio(token_observations, model_spans),
        },
        "duration_ms": {
            "model_span_total": model_ms,
            "model_spans": model_spans,
            "tool_span_total": tool_ms,
            "tool_spans": tool_spans,
            "evaluation_case_total": sum(result.duration_ms or 0 for result in results),
            "human_wait": None,
            "human_wait_status": "unavailable_not_measured_by_cli",
        },
    }


def _format_ratio(value: float | None) -> str:
    return "不可用" if value is None else f"{value * 100:.2f}%"


def _counter_metrics(counts: dict[str, Counter]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "correct": values["correct"],
            "present": values["present"],
            "required": values["required"],
            "exact_match_rate": _ratio(values["correct"], values["required"]),
            "coverage_rate": _ratio(values["present"], values["required"]),
        }
        for key, values in sorted(counts.items())
    }


def _evidence_path(field_path: str) -> str:
    aliases = {
        "contract_number": "contract_id",
        "material_id": "items[0].material_id",
        "quantity": "items[0].quantity",
        "unit": "items[0].unit",
        "unit_price": "items[0].unit_price",
        "amount": "items[0].amount",
        "spec": "items[0].specification",
    }
    if field_path.startswith("line_details["):
        return field_path.replace("line_details[", "items[", 1)
    if field_path.startswith("line_material_ids["):
        return field_path.replace("line_material_ids[", "items[", 1).replace(
            "]", "].material_id", 1
        )
    return aliases.get(field_path, field_path)


def _expected_field_values(expected: dict[str, Any]) -> dict[str, Any]:
    fields = expected.get("fields", {})
    if not isinstance(fields, dict):
        return {}
    labels = expected.get("field_labels")
    if isinstance(labels, list):
        selected = {label for label in labels if isinstance(label, str)}
        return {key: value for key, value in fields.items() if key in selected}
    return {
        key: value for key, value in fields.items() if key in _EXTRACTED_FIELD_NAMES
    }


def _required_field_leaves(expected: dict[str, Any]) -> dict[str, Any]:
    return {
        path: value
        for path, value in _leaf_values(_expected_field_values(expected)).items()
        if value is not None
    }


def _metrics_markdown(
    metrics: dict[str, Any], safe_invariants: dict[str, Any] | None = None
) -> list[str]:
    lines = [
        "| 指标 | 通过/观测 | 适用/总数 | 比率或数值 |",
        "|---|---:|---:|---:|",
    ]
    for key, label in (
        ("field_exact_match", "字段精确匹配"),
        ("field_coverage", "字段覆盖率"),
        ("tool_selection", "工具选择"),
        ("tool_execution", "工具执行"),
        ("rule_blocking", "规则阻断"),
        ("idempotency", "幂等"),
        ("recovery", "恢复"),
    ):
        item = metrics[key]
        numerator = item.get("correct", item.get("passed", item.get("present", 0)))
        denominator = item.get("required", item.get("applicable", 0))
        lines.append(
            f"| {label} | {numerator} | {denominator} | {_format_ratio(item['rate'])} |"
        )
    token = metrics["tokens"]
    validation = metrics["tool_parameter_validation"]
    lines.append(
        f"| 工具参数校验 | 接受 {validation['accepted']} / 拒绝 {validation['rejected']} | {validation['total']} 次 | 按实际规则结果记录 |"
    )
    lines.extend(
        [
            f"| Token 覆盖率 | {token['observed_model_spans']} | {token['observed_model_spans'] + token['unavailable_model_spans']} | {_format_ratio(token['coverage'])} |",
            f"| 模型耗时合计 | {metrics['duration_ms']['model_span_total']} ms | {metrics['duration_ms']['model_spans']} 次 | — |",
            f"| 工具耗时合计 | {metrics['duration_ms']['tool_span_total']} ms | {metrics['duration_ms']['tool_spans']} 次 | — |",
            f"| 人工等待 | — | — | {metrics['duration_ms']['human_wait_status']} |",
        ]
    )
    lines.append("")
    lines.append(
        f"Token 合计：输入 {token['input'] if token['input'] is not None else 'unavailable'}，输出 {token['output'] if token['output'] is not None else 'unavailable'}，总计 {token['total'] if token['total'] is not None else 'unavailable'}。"
    )
    lines.append(
        f"评测单例执行耗时合计：{metrics['duration_ms']['evaluation_case_total']} ms（不含人工等待）。"
    )
    lines.append(f"安全不变量：{_safe_invariants_text(safe_invariants)}")
    lines.append("")
    lines.append(
        "字段细分（`field_by_path`、`field_by_source`、`field_by_file_type` 同时保存在 JSON）："
    )
    for grouping, label in (
        ("field_by_path", "字段"),
        ("field_by_source", "来源"),
        ("field_by_file_type", "文件/输入类型"),
    ):
        detail = metrics[grouping]
        lines.append(
            f"- {label}："
            + (
                "；".join(
                    f"{key} {value['correct']}/{value['required']} 精确、覆盖 {value['present']}/{value['required']}"
                    for key, value in detail.items()
                )
                if detail
                else "unavailable"
            )
        )
    failures = metrics["failed_assertions"]
    lines.append(
        "- 失败断言："
        + (
            "；".join(
                f"{source} ×{count}" for source, count in sorted(failures.items())
            )
            if failures
            else "无"
        )
    )
    return lines


def _safe_invariants_text(invariant: Any) -> str:
    if not isinstance(invariant, dict) or invariant.get("status") is None:
        return "不适用"
    return f"{invariant['passed']}/{invariant['applicable']}，{_format_ratio(invariant['pass_rate'])}"


def _actual_summary(actual: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(actual, dict):
        return {"value": actual}
    trace = actual.get("trace", {})
    spans = trace.get("spans", []) if isinstance(trace, dict) else []
    return {
        "task": actual.get("task"),
        "draft": actual.get("draft"),
        "database": actual.get("database"),
        "action": actual.get("action"),
        "selected_tools": trace.get("model_tool_names", [])
        if isinstance(trace, dict)
        else [],
        "span_ids": [
            span.get("span_id")
            for span in spans
            if isinstance(span, dict) and span.get("span_id")
        ],
        "trace_id": trace.get("trace_id") if isinstance(trace, dict) else None,
        "error_code": actual.get("error_code"),
    }


def _redact(value: Any, key: str = "") -> Any:
    sensitive = (
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "csrf_token",
        "database_url",
        "signing_key",
        "credential",
    )
    if any(part in key.lower() for part in sensitive):
        return "[已脱敏]"
    if isinstance(value, dict):
        return {name: _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, key) for item in value]
    if isinstance(value, str):
        patterns = (
            r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b",
            r"\bBearer\s+[^\s,;]+",
            r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
            r"\bAKIA[A-Z0-9]{16}\b",
        )
        for pattern in patterns:
            value = re.sub(pattern, "[已脱敏]", value, flags=re.IGNORECASE)
        return value
    return value


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)
