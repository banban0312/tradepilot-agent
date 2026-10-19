"""数据集读取、结果断言与运行汇总；执行器必须返回真实观测值。"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


class CaseSkipped(RuntimeError):
    """执行依赖不可用或场景未支持；不能按通过计数。"""


class CaseExecutor(Protocol):
    async def execute_case(
        self, case: dict[str, Any], *, mode: str
    ) -> dict[str, Any]: ...


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
        passed = (
            observed == target
            if operator == "eq"
            else isinstance(observed, list | tuple | set)
            and set(target).issubset(set(observed))
            if operator == "contains_all"
            else False
        )
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

    async def run(self, cases: list[dict]) -> list[CaseResult]:
        results = []
        for case in cases:
            expected = case["expected"]
            try:
                actual = await self.executor.execute_case(case, mode=self.mode)
            except CaseSkipped as exc:
                results.append(
                    CaseResult(
                        case_id=case["id"],
                        category=case["category"],
                        status="SKIPPED",
                        expected=expected,
                        input=case.get("input", {}),
                        error_code=str(exc) or "EVAL_DEPENDENCY_UNAVAILABLE",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - each case must leave a report row.
                results.append(
                    CaseResult(
                        case_id=case["id"],
                        category=case["category"],
                        status="ERROR",
                        expected=expected,
                        input=case.get("input", {}),
                        error_code=str(getattr(exc, "code", "EVAL_EXECUTION_ERROR")),
                        actual={"exception_type": type(exc).__name__},
                    )
                )
                continue
            failed = evaluate_assertions(expected, actual)
            results.append(
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
                )
            )
        return results


def summarize_results(results: list[CaseResult]) -> dict[str, Any]:
    statuses = Counter(result.status for result in results)
    categories: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = categories.setdefault(result.category, Counter())
        bucket[result.status] += 1
    return {
        "case_count": len(results),
        "status_counts": {
            status: statuses.get(status, 0)
            for status in ("PASS", "FAIL", "SKIPPED", "ERROR")
        },
        "category_counts": {
            category: dict(counts) for category, counts in categories.items()
        },
        "safe_invariants_passed": all(
            result.status == "PASS"
            for result in results
            if result.category in {"unauthorized", "quota", "duplicate_submission"}
        ),
        "exit_code": 0
        if results
        and all(result.status == "PASS" for result in results)
        and all(
            result.status == "PASS"
            for result in results
            if result.category in {"unauthorized", "quota", "duplicate_submission"}
        )
        else 1,
    }


def result_rows(results: list[CaseResult]) -> list[dict[str, Any]]:
    return [asdict(result) for result in results]


def write_report(
    report_dir: Path, *, metadata: dict, results: list[CaseResult]
) -> dict:
    """原子写入便于机器读取及人工复核的三种报告。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    summary = {**metadata, **summarize_results(results)}
    rows = result_rows(results)
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
        f"- 模式：{metadata['mode']}",
        f"- 数据集：{metadata['dataset_version']}",
        f"- 案例数：{summary['case_count']}",
        "- 结果："
        + "，".join(
            f"{key} {value}" for key, value in summary["status_counts"].items()
        ),
        "",
        "## 案例明细",
        "",
    ]
    for row in rows:
        lines.extend([f"### {row['case_id']} — {row['status']}", ""])
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


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)
