"""FastAPI 进程入口；提供健康检查、配置状态和身份认证端点。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth.origin import is_same_origin
from app.auth.routes import router as auth_router
from app.config import settings
from app.database import check_database_connection, engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """进程退出时释放数据库连接池。"""
    yield
    await engine.dispose()


app = FastAPI(title="TradePilot API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, error: HTTPException) -> JSONResponse:
    detail = error.detail
    if isinstance(detail, dict) and "code" in detail:
        body = {
            "error": {
                "code": detail["code"],
                "message": detail.get("message", "请求未能处理"),
                "retryable": False,
                "details": {},
            },
            "request_id": getattr(request.state, "request_id", ""),
        }
        return JSONResponse(
            status_code=error.status_code, content=body, headers=error.headers
        )
    return JSONResponse(
        status_code=error.status_code, content={"detail": detail}, headers=error.headers
    )


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    """所有 API 写入口都先校验 Origin；会话写入口再校验 CSRF 令牌。"""
    supplied_request_id = request.headers.get("X-Request-ID", "")
    try:
        request.state.request_id = str(UUID(supplied_request_id))
    except (ValueError, TypeError):
        request.state.request_id = str(uuid4())

    unsafe_method = request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
    if (
        request.url.path.startswith("/api/v1/")
        and unsafe_method
        and not is_same_origin(request)
    ):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "ORIGIN_INVALID",
                    "message": "请求来源与当前应用不一致",
                    "retryable": False,
                    "details": {},
                },
                "request_id": request.state.request_id,
            },
        )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.get("/health/live", tags=["health"])
async def live() -> dict[str, str]:
    """确认 API 进程可响应，不代表业务功能已交付。"""
    return {"status": "live"}


@app.get("/health/ready", tags=["health"])
async def ready() -> dict[str, str]:
    """只有 PostgreSQL 实际响应只读查询后才报告就绪。"""
    try:
        await check_database_connection()
    except Exception as error:
        raise HTTPException(status_code=503, detail="PostgreSQL 尚未就绪") from error
    return {"status": "ready", "database": "connected"}


@app.get("/health/config", tags=["health"])
async def configuration_status() -> dict[str, dict[str, str]]:
    """公开非敏感的配置状态；不返回凭据，也不声称外部服务已验证。"""
    return {
        "model": {
            "status": settings.model_configuration_state,
            "provider": settings.model_provider or "",
            "model": settings.model_name or "",
        },
        "ocr": {
            "status": settings.ocr_configuration_state,
            "provider": settings.ocr_provider or "",
        },
    }
