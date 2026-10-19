"""运行固定评测集并生成机器可读与人工可读报告。"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.service import (
    CaseSkipped,
    EvaluationService,
    load_dataset,
    select_cases,
    write_report,
)


def _load_factory(spec: str):
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("执行器工厂格式应为 package.module:factory")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError("执行器工厂不可调用")
    return factory


async def _run(args: argparse.Namespace) -> int:
    dataset_root = Path(args.dataset_root).resolve()
    manifest, all_cases = load_dataset(dataset_root, args.dataset)
    selected = select_cases(
        all_cases,
        case_ids=set(args.case) if args.case else None,
        categories=set(args.category) if args.category else None,
    )
    unsupported = [
        case["id"] for case in selected if args.mode not in case["execution_modes"]
    ]
    if unsupported:
        raise ValueError(f"所选案例不支持 {args.mode} 模式：{', '.join(unsupported)}")

    # 固定伪随机源便于复现；报告仍记录真实运行时间，不能冒充固定时钟成绩。
    random.seed(args.random_seed)
    factory = _load_factory(args.executor_factory)
    executor = factory(mode=args.mode, manifest=manifest, seed=args.random_seed)
    results = await EvaluationService(executor, mode=args.mode).run(selected)
    metadata = {
        "mode": args.mode,
        "dataset_version": manifest["dataset_version"],
        "master_data_version": manifest["master_data_version"],
        "input_fixture_version": manifest["input_fixture_version"],
        "random_seed": args.random_seed,
        "started_at": args.started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "selected_case_ids": [case["id"] for case in selected],
        "executor": args.executor_factory,
        "online_model_configuration": (
            "configured_unverified"
            if args.mode == "online" and os.getenv("MODEL_API_KEY")
            else "not_detected"
            if args.mode == "online"
            else "not_applicable"
        ),
    }
    summary = write_report(
        Path(args.report_dir).resolve(), metadata=metadata, results=results
    )
    print(f"评测报告：{Path(args.report_dir).resolve()}")
    print(f"案例 {summary['case_count']}：{summary['status_counts']}")
    return summary["exit_code"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradePilot 隔离数据域自动评测 CLI")
    parser.add_argument("--mode", required=True, choices=("offline", "online"))
    parser.add_argument("--dataset", default="v1", help="数据集目录版本，例如 v1")
    parser.add_argument("--case", action="append", help="仅运行指定案例；可重复指定")
    parser.add_argument(
        "--category", action="append", help="仅运行指定分类；可重复指定"
    )
    parser.add_argument("--report-dir", required=True)
    parser.add_argument(
        "--dataset-root",
        default=str(Path(__file__).resolve().parent),
        help="评测数据目录，默认使用仓库内 code/evals",
    )
    parser.add_argument(
        "--executor-factory",
        default=os.getenv(
            "EVAL_EXECUTOR_FACTORY", "evals.backend_executor:create_executor"
        ),
        help="评测执行器工厂 module:callable",
    )
    parser.add_argument("--random-seed", type=int, default=20261016)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.started_at = datetime.now(UTC).isoformat()
    try:
        return asyncio.run(_run(args))
    except (ValueError, OSError, ImportError, AttributeError) as exc:
        print(f"评测配置或数据错误：{exc}", file=sys.stderr)
        return 2
    except CaseSkipped as exc:
        print(f"评测依赖未就绪：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
