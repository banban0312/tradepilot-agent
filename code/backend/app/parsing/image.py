"""单据图片 OCR；沿用 PDF 解析的 OCR 与候选证据格式。"""

from __future__ import annotations

import io
import re
import time
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.parsing.pdf import (
    MAX_OCR_SECONDS,
    PdfParseError,
    _field_candidates,
    _ocr_image,
    sha256_file,
)

MAX_IMAGE_PIXELS = 40_000_000


def parse_image(path: Path, input_sha256: str) -> dict[str, Any]:
    """解析一张已验证图片，保留原图哈希和像素框，不确认任何候选字段。"""
    if not re.fullmatch(r"[a-f0-9]{64}", input_sha256):
        raise PdfParseError("INPUT_HASH_INVALID", "附件哈希无效", 500)
    if sha256_file(path) != input_sha256:
        raise PdfParseError("FILE_INTEGRITY_FAILED", "附件内容校验失败", 503)

    started = time.monotonic()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                media_type = source.format
                image = ImageOps.exif_transpose(source)
                width, height = image.size
                if (
                    width < 1
                    or height < 1
                    or width * height > MAX_IMAGE_PIXELS
                    or width > 16_000
                    or height > 16_000
                ):
                    raise PdfParseError("FILE_TOO_LARGE", "图片尺寸超出安全处理范围")
                image.load()
                image = image.convert("RGB")
                remaining = MAX_OCR_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise PdfParseError("OCR_TIMEOUT", "图片 OCR 超时")
                with io.BytesIO() as output:
                    image.save(output, format="PNG", optimize=True)
                    ocr_lines = _ocr_image(output.getvalue(), timeout=remaining)
    except PdfParseError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise PdfParseError("FILE_UNREADABLE", "图片文件损坏或无法读取") from exc

    lines = [
        {
            "text": line.text,
            "locator": {"type": "bbox", **line.bbox},
            "confidence": line.confidence,
        }
        for line in ocr_lines
    ]
    page = {"page_number": 1, "mode": "OCR" if lines else "UNREADABLE", "lines": lines}
    fields, missing = _field_candidates(
        [page], include_conflicts=True, include_delivery_fields=True
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    has_conflict = any(field["status"] == "CONFLICT" for field in fields)
    return {
        "state": "PARTIAL" if missing or not lines or has_conflict else "COMPLETE",
        "input_sha256": input_sha256,
        "media_type": "image/png" if media_type == "PNG" else "image/jpeg",
        "ocr_engine": "tesseract",
        "ocr_version": _tesseract_version(),
        "elapsed_ms": elapsed_ms,
        "pages": [page],
        "fields": fields,
        "missing_fields": missing,
        "issues": [] if lines else [{"code": "IMAGE_UNREADABLE", "page_number": 1}],
    }


def _tesseract_version() -> str | None:
    """返回本机引擎版本，版本查询失败时不影响已取得的 OCR 结果。"""
    import subprocess

    try:
        result = subprocess.run(
            ["tesseract", "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    return first_line.removeprefix("tesseract ")[:80] or None
