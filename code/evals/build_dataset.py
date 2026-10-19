"""Rebuild deterministic M6-01 binary fixtures and their versioned indexes."""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "datasets" / "v1"
FIXTURES = ROOT / "fixtures" / "v1"
FIXED_TIME = "2026-10-16T09:00:00+08:00"
REQUIRED_CATEGORIES = (
    "normal",
    "abnormal",
    "ambiguity",
    "unauthorized",
    "quota",
    "duplicate_submission",
)


def _write_files() -> dict:
    files = FIXTURES / "files"
    files.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    workbook.properties.created = workbook.properties.modified = datetime(2020, 1, 1)
    sheet = workbook.active
    sheet.title = "订单明细"
    sheet.append(
        [
            "订单分组",
            "订单类型",
            "合同编号",
            "项目",
            "地址",
            "联系人",
            "交期",
            "物料名称",
            "规格",
            "数量",
            "单位",
            "含税单价",
        ]
    )
    sheet.append(
        [
            "正常组",
            "销售",
            "TP-DEMO-SALES-N1",
            "华北示例工程",
            "北京市朝阳区示例路1号",
            "张先生",
            "2026-11-01",
            "HRB400",
            "12mm",
            2,
            "吨",
            3500,
        ]
    )
    sheet.append(
        [
            "歧义组",
            "销售",
            "TP-DEMO-SALES-N1",
            "华北示例工程",
            "北京市朝阳区示例路1号",
            "李女士",
            "2026-11-02",
            "螺纹钢",
            "",
            1,
            "吨",
            3500,
        ]
    )
    sheet.append(
        [
            "超额组",
            "销售",
            "TP-DEMO-SALES-N1",
            "华北示例工程",
            "北京市朝阳区示例路1号",
            "王先生",
            "2026-11-03",
            "HRB400",
            "12mm",
            100,
            "吨",
            3500,
        ]
    )

    raw_workbook = io.BytesIO()
    workbook.save(raw_workbook)
    workbook.close()
    normalized_workbook = io.BytesIO()
    with (
        ZipFile(raw_workbook, "r") as source_zip,
        ZipFile(
            normalized_workbook,
            "w",
            compression=ZIP_DEFLATED,
            compresslevel=9,
        ) as target_zip,
    ):
        for name in sorted(source_zip.namelist()):
            entry = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = source_zip.getinfo(name).external_attr
            entry.create_system = 0
            content = source_zip.read(name)
            if name == "docProps/core.xml":
                content = re.sub(
                    rb"(<dcterms:modified\b[^>]*>).*?(</dcterms:modified>)",
                    rb"\g<1>2020-01-01T00:00:00Z\g<2>",
                    content,
                )
            target_zip.writestr(entry, content)
    (files / "mixed_batch.xlsx").write_bytes(normalized_workbook.getvalue())
    (files / "damaged.pdf").write_bytes(
        b"%PDF-1.4\nSynthetic deliberately truncated fixture; no customer data.\n"
    )

    definitions = {}
    for fixture_id, filename, media_type in [
        (
            "mixed_batch",
            "mixed_batch.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("damaged_pdf", "damaged.pdf", "application/pdf"),
    ]:
        path = files / filename
        definitions[fixture_id] = {
            "path": f"files/{filename}",
            "media_type": media_type,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "description": "合成评测输入，不含真实业务或个人信息。",
        }
    return definitions


def main() -> None:
    master = json.loads((FIXTURES / "master_data.json").read_text(encoding="utf-8"))
    cases = [
        json.loads(line)
        for line in (DATASET / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    files = _write_files()
    index = {
        "version": "1.0.0",
        "actors": master["actors"],
        "contracts": master["contracts"],
        "files": files,
    }
    (FIXTURES / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    counts = {
        category: sum(case["category"] == category for case in cases)
        for category in REQUIRED_CATEGORIES
    }
    manifest = {
        "dataset_version": "1.0.0",
        "master_data_version": master["version"],
        "input_fixture_version": "1.0.0",
        "timezone": master["timezone"],
        "fixed_clock": FIXED_TIME,
        "case_count": len(cases),
        "required_categories": list(REQUIRED_CATEGORIES),
        "minimum_cases_per_category": 6,
        "category_counts": counts,
        "special_case_count": sum(case["category"] == "special" for case in cases),
        "special_requirements": {
            "recovery": 2,
            "concurrency": 2,
            "unknown_commit_result": 1,
        },
        "files": files,
        "execution_modes": {
            "offline": "固定输入夹具；结果不得标记为在线模型成绩",
            "online": "真实模型及外部服务执行，须单独记录配置与失败/跳过状态",
        },
    }
    (DATASET / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已更新夹具清单：共 {len(cases)} 例，六类数量 {counts}。")


if __name__ == "__main__":
    main()
