"""M3-07 AC-01～AC-20 验收索引的完整性检查。"""

from __future__ import annotations

import json
from pathlib import Path


def test_core_acceptance_matrix_covers_all_twenty_scenarios() -> None:
    matrix = Path(__file__).with_name("issue_021_acceptance_matrix.json")
    rows = json.loads(matrix.read_text(encoding="utf-8"))
    assert [row["ac"] for row in rows] == [
        f"AC-{number:02d}" for number in range(1, 21)
    ]
    assert all(row["tests"] and row["coverage"] for row in rows)
    assert rows[3]["online"] == "DeepSeek 在线钢材闭环；不覆盖相对日期解析"
    assert rows[19]["result"] == "partial_m4_file_permission"
    assert all("模拟" not in row["coverage"] for row in rows)
