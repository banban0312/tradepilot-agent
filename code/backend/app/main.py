"""FastAPI 进程入口；当前仅提供启动、就绪及配置状态端点。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import settings
from app.database import check_database_connection, engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """进程退出时释放数据库连接池。"""
    yield
    await engine.dispose()


app = FastAPI(title="TradePilot API", version="0.1.0", lifespan=lifespan)


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
