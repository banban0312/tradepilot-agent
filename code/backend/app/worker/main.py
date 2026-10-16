"""Worker 入口基线；作业领取和执行由 M2 的任务队列 issue 实现。"""

import asyncio
import logging
import signal

from app.config import settings
from app.database import check_database_connection, engine

logger = logging.getLogger("tradepilot.worker")


async def run() -> None:
    """验证数据库连接并保持独立进程存活，等待后续队列实现。"""
    await check_database_connection()
    logger.info("Worker 已启动；作业队列将在后续 issue 实现")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(handled_signal, stop.set)
    try:
        await stop.wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("附件目录配置为 %s", settings.attachments_root)
    asyncio.run(run())
