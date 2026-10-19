"""Excel 模板下载与受权限保护的订单分组预览。"""

from __future__ import annotations

import hashlib
import io
from typing import Annotated
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.worksheet.datavalidation import DataValidation
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.routes import _readable_artifact
from app.auth.context import BUSINESS_OPERATOR, AuthContext, require_role
from app.auth.dependencies import get_current_identity, request_id, require_csrf
from app.config import settings
from app.database import get_db_session
from app.imports.service import TEMPLATE_HEADERS, preview_workbook
from app.models import AuditEvent
from app.storage.files import FileUploadError, storage_path

router = APIRouter(prefix="/api/v1/imports", tags=["imports"])


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID


def _api_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _template() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单明细"
    sheet.append(TEMPLATE_HEADERS)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = "A1:L1"
    widths = (18, 14, 20, 24, 32, 16, 16, 24, 22, 14, 12, 16)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + index)].width = width
    order_type = DataValidation(type="list", formula1='"销售,采购"', allow_blank=False)
    sheet.add_data_validation(order_type)
    order_type.add("B2:B501")

    guide = workbook.create_sheet("填报说明")
    guide.append(("说明", "请按以下规则填写订单明细"))
    guide.append(
        ("订单分组", "同一订单的多条物料明细填写相同分组编号；不同订单使用不同编号。")
    )
    guide.append(("订单类型", "填写销售或采购，且必须与有效合同类型一致。"))
    guide.append(
        ("合同编号", "填写当前部门有效合同的完整编号；项目需与合同关联项目一致。")
    )
    guide.append(
        ("必填字段", "所有模板列均为必填；数量和含税单价必须大于 0，最多 6 位小数。")
    )
    guide.append(
        (
            "单元格",
            "业务字段请填写固定值，不要使用公式；本次导入只预览和校验，不会创建任务或订单。",
        )
    )
    guide.column_dimensions["A"].width = 20
    guide.column_dimensions["B"].width = 96
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@router.get("/template")
async def download_template(
    context: Annotated[AuthContext, Depends(get_current_identity)],
) -> StreamingResponse:
    require_role(context, BUSINESS_OPERATOR)
    content = _template()
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''TradePilot-%E8%AE%A2%E5%8D%95%E6%A8%A1%E6%9D%BF.xlsx",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/preview")
async def preview_import(
    body: PreviewRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    require_role(context, BUSINESS_OPERATOR)
    artifact = await _readable_artifact(session, body.artifact_id, context)
    if artifact.metadata_json.get("parse_intent") != "order_xlsx":
        raise _api_error(415, "FILE_UNSUPPORTED", "请选择已上传的 XLSX 订单文件")
    if artifact.state != "STORED":
        raise _api_error(409, "ARTIFACT_NOT_READY", "附件当前不可解析，请重新上传")
    try:
        path = storage_path(settings.attachments_root, artifact.storage_key)
    except FileUploadError as exc:
        raise _api_error(exc.status_code or 503, exc.code, exc.message) from exc
    if path.is_symlink() or not path.is_file():
        raise _api_error(503, "FILE_UNREADABLE", "附件存储暂不可用")
    digest = await anyio.to_thread.run_sync(
        lambda: hashlib.sha256(path.read_bytes()).hexdigest()
    )
    if digest != artifact.sha256:
        raise _api_error(503, "FILE_INTEGRITY_FAILED", "附件内容校验失败")
    try:
        data = await preview_workbook(
            path,
            artifact_id=artifact.id,
            session=session,
            department_ids=context.department_ids,
        )
    except ValueError as exc:
        raise _api_error(413, "FILE_TOO_LARGE", str(exc)) from exc
    session.add(
        AuditEvent(
            id=uuid4(),
            task_id=None,
            actor_id=context.user_id,
            action="import.previewed",
            object_ref={"artifact_id": str(artifact.id)},
            after_ref={
                "detail_rows": data["summary"]["detail_rows"],
                "group_count": data["summary"]["group_count"],
                "needs_correction_groups": data["summary"]["needs_correction_groups"],
            },
            result="SUCCESS",
            input_hash=artifact.sha256,
        )
    )
    await session.commit()
    return {"data": data, "request_id": request_id(request)}
