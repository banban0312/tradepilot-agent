"""API 与 Worker 使用的异步 SQLAlchemy 数据库连接。"""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url.get_secret_value(),
    pool_pre_ping=True,
)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """为 API 请求提供独立事务会话，并在响应结束时关闭。"""
    async with async_session_factory() as session:
        yield session


async def check_database_connection() -> None:
    """执行只读往返查询，供健康检查确认数据库真实可用。"""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
