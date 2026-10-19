"""逐页解析 PDF；扫描页使用本地 Tesseract，所有内容都只作为不可信数据。"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from pypdf import PdfReader

MAX_OCR_SECONDS = 60
MAX_RENDER_PIXELS = 40_000_000
MIN_TEXT_CHARS = 12


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    bbox: dict[str, int]
    confidence: float | None


class PdfParseError(Exception):
    """安全的解析错误，不暴露本地路径或文档内容。"""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _ocr_image(image_bytes: bytes, *, timeout: float) -> list[OcrLine]:
    """调用固定可执行文件并解析 TSV；用户内容不会进入命令行参数。"""
    try:
        with tempfile.TemporaryDirectory(prefix="tradepilot-ocr-") as directory:
            image_path = Path(directory) / "page.png"
            image_path.write_bytes(image_bytes)
            result = subprocess.run(
                [
                    "tesseract",
                    str(image_path),
                    "stdout",
                    "-l",
                    "chi_sim+eng",
                    "--psm",
                    "6",
                    "tsv",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.1, timeout),
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
    except FileNotFoundError as exc:
        raise PdfParseError("OCR_UNAVAILABLE", "本地 OCR 引擎未安装", 503) from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfParseError("OCR_TIMEOUT", "扫描页 OCR 超时") from exc
    except OSError as exc:
        raise PdfParseError("OCR_FAILED", "扫描页 OCR 执行失败") from exc
    if result.returncode != 0:
        raise PdfParseError("OCR_FAILED", "扫描页 OCR 执行失败")

    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text or row.get("level") != "5":
            continue
        try:
            float(row["conf"])  # Tesseract 的启发式分值未经校准，不对外当作置信度。
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
            line_key = (
                row["page_num"],
                row["block_num"],
                row["par_num"],
                row["line_num"],
            )
        except (KeyError, TypeError, ValueError):
            continue
        if width < 1 or height < 1:
            continue
        existing = grouped.get(line_key)
        if existing is None:
            grouped[line_key] = {
                "words": [text],
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
            }
        else:
            existing["words"].append(text)
            existing["left"] = min(existing["left"], left)
            existing["top"] = min(existing["top"], top)
            existing["right"] = max(existing["right"], left + width)
            existing["bottom"] = max(existing["bottom"], top + height)
    return [
        OcrLine(
            text=" ".join(line["words"]),
            bbox={
                "x": line["left"],
                "y": line["top"],
                "width": line["right"] - line["left"],
                "height": line["bottom"] - line["top"],
            },
            confidence=None,
        )
        for line in grouped.values()
    ]


def _field_candidates(
    pages: list[dict[str, Any]],
    *,
    include_conflicts: bool = False,
    include_delivery_fields: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    patterns: dict[str, re.Pattern[str]] = {
        "contract_number": re.compile(
            r"(?:合同编号|合同号|contract\s*(?:no|number))\s*[:：#;；]?\s*([A-Za-z0-9][A-Za-z0-9/_-]{2,})",
            re.IGNORECASE,
        ),
        "customer": re.compile(
            r"(?:客户名称|客户|需方|买方|购方|签约单位)\s*[:：]?\s*([^，,；;\n]{2,40})"
        ),
        "material": re.compile(
            r"(?:物料名称|产品名称|商品名称|材料名称|物料|品名)\s*[:：]?\s*([^，,；;\n]{2,60})"
        ),
        "specification": re.compile(
            r"(?:规格型号|规格|型号)\s*[:：]?\s*([^，,；;\n]{1,40})"
        ),
        "quantity": re.compile(
            r"(?:数量|qty|quantity)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*(吨|tons?|tonnes?|t|kg|千克|件|套|米|根)?",
            re.IGNORECASE,
        ),
        "amount": re.compile(
            r"(?:含税总价|合同金额|总金额|金额|合计|total\s*amount)\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
            re.IGNORECASE,
        ),
        "delivery_date": re.compile(
            r"(?:交付日期|交货日期|交期|交货时间|delivery\s*date)\s*[:：]?\s*((?:20\d{2})[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?)",
            re.IGNORECASE,
        ),
    }
    if include_delivery_fields:
        patterns["recipient"] = re.compile(
            r"(?:收货人|联系人|签收人|recipient|receiver|contact(?:\s+person)?)\s*[:：]?\s*([^，,；;\n]{2,30})",
            re.IGNORECASE,
        )
        patterns["delivery_address"] = re.compile(
            r"(?:收货地址|送货地址|卸货地址|交货地点|delivery\s+address|shipping\s+address)\s*[:：]?\s*([^，,；;\n]{4,80})",
            re.IGNORECASE,
        )
    found: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        for line in page["lines"]:
            for field, pattern in patterns.items():
                match = pattern.search(line["text"])
                if not match or (field in found and not include_conflicts):
                    continue
                candidate = {
                    "field": field,
                    "value": match.group(1).strip(),
                    "page_number": page["page_number"],
                    "raw_text": line["text"],
                    "locator": line["locator"],
                    "confidence": line["confidence"],
                    "confirmation_required": True,
                }
                if (
                    include_conflicts
                    and field == "quantity"
                    and match.lastindex
                    and match.lastindex >= 2
                ):
                    unit = match.group(2)
                    if unit:
                        candidate["unit"] = unit
                if include_conflicts:
                    found.setdefault(field, []).append(candidate)
                else:
                    found[field] = [candidate]
    required = list(patterns)
    fields: list[dict[str, Any]] = []
    for candidates in found.values():
        values = {candidate["value"] for candidate in candidates}
        status = "CONFLICT" if len(values) > 1 else "NEEDS_CONFIRMATION"
        fields.extend(
            {**candidate, "status": status} if include_conflicts else candidate
            for candidate in candidates
        )
    return fields, [field for field in required if field not in found]


def parse_pdf(path: Path, input_sha256: str) -> dict[str, Any]:
    """解析已鉴权的附件；返回结果携带输入哈希、页码和原始位置证据。"""
    if not re.fullmatch(r"[a-f0-9]{64}", input_sha256):
        raise PdfParseError("INPUT_HASH_INVALID", "附件哈希无效", 500)
    try:
        reader = PdfReader(str(path), strict=True)
        if reader.is_encrypted:
            raise PdfParseError("FILE_UNREADABLE", "不支持加密 PDF")
        page_count = len(reader.pages)
        document = pdfium.PdfDocument(str(path))
    except PdfParseError:
        raise
    except Exception as exc:
        raise PdfParseError("FILE_UNREADABLE", "PDF 文件损坏或无法读取") from exc

    deadline = time.monotonic() + MAX_OCR_SECONDS
    pages: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    try:
        for index, page in enumerate(reader.pages):
            page_number = index + 1
            try:
                text = page.extract_text() or ""
                lines: list[dict[str, Any]] = []
                if len(re.sub(r"\s", "", text)) >= MIN_TEXT_CHARS:
                    offset = 0
                    for raw_line in text.splitlines():
                        line_text = raw_line.strip()
                        if not line_text:
                            continue
                        start = text.find(raw_line, offset)
                        offset = max(start, 0) + len(raw_line)
                        lines.append(
                            {
                                "text": line_text,
                                "locator": {
                                    "type": "text_range",
                                    "start": max(start, 0),
                                    "end": max(start, 0) + len(raw_line),
                                },
                                "confidence": None,
                            }
                        )
                    mode = "TEXT"
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PdfParseError("OCR_TIMEOUT", "扫描页 OCR 超时")
                    rendered_page = document[index]
                    width, height = rendered_page.get_size()
                    scale = 200 / 72
                    if width * scale * height * scale > MAX_RENDER_PIXELS:
                        raise PdfParseError(
                            "PAGE_TOO_LARGE", "PDF 页面像素尺寸超出安全范围"
                        )
                    rendered = rendered_page.render(scale=scale)
                    image = rendered.to_pil()
                    if image.width * image.height > MAX_RENDER_PIXELS:
                        raise PdfParseError(
                            "PAGE_TOO_LARGE", "PDF 页面像素尺寸超出安全范围"
                        )
                    with io.BytesIO() as output:
                        image.save(output, format="PNG", optimize=True)
                        ocr_lines = _ocr_image(
                            output.getvalue(), timeout=deadline - time.monotonic()
                        )
                    lines = [
                        {
                            "text": line.text,
                            "locator": {"type": "bbox", **line.bbox},
                            "confidence": line.confidence,
                        }
                        for line in ocr_lines
                    ]
                    mode = "OCR" if lines else "UNREADABLE"
                    if not lines:
                        issues.append(
                            {"code": "PAGE_UNREADABLE", "page_number": page_number}
                        )
                pages.append({"page_number": page_number, "mode": mode, "lines": lines})
            except PdfParseError as exc:
                pages.append(
                    {"page_number": page_number, "mode": "UNREADABLE", "lines": []}
                )
                issues.append({"code": exc.code, "page_number": page_number})
            except Exception:  # noqa: BLE001 - renderer/OCR parser errors must mark only this page unreadable.
                pages.append(
                    {"page_number": page_number, "mode": "UNREADABLE", "lines": []}
                )
                issues.append({"code": "PAGE_PARSE_FAILED", "page_number": page_number})
    finally:
        document.close()

    if len(pages) != page_count:
        raise PdfParseError("FILE_UNREADABLE", "PDF 页面解析不完整")
    fields, missing = _field_candidates(
        pages, include_conflicts=True, include_delivery_fields=True
    )
    has_conflict = any(field["status"] == "CONFLICT" for field in fields)
    return {
        "state": "PARTIAL" if issues or missing or has_conflict else "COMPLETE",
        "input_sha256": input_sha256,
        "page_count": page_count,
        "pages": pages,
        "fields": fields,
        "missing_fields": missing,
        "issues": issues,
    }


def sha256_file(path: Path) -> str:
    """用流式读取校验解析输入仍与上传时保持一致。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
