"""Worker 容器健康检查：确认它所依赖的 PostgreSQL 可用。"""

import asyncio

from app.database import check_database_connection, engine


async def main() -> None:
    try:
        await check_database_connection()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
