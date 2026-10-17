"""持久任务 Worker 入口；业务图执行器由后续工作流 issue 注册。"""

import asyncio
import logging
import signal

from app.config import settings
from app.database import async_session_factory, check_database_connection, engine
from app.worker.queue import PersistentTaskWorker
from app.worker.workflow import create_run_task_handler

logger = logging.getLogger("tradepilot.worker")


async def run() -> None:
    """启动恢复协调器与持久队列轮询器。"""
    await check_database_connection()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(handled_signal, stop.set)
    handler = await create_run_task_handler(session_factory=async_session_factory)
    worker = PersistentTaskWorker(
        engine=engine,
        session_factory=async_session_factory,
        handlers={"RUN_TASK": handler},
    )
    runner = asyncio.create_task(worker.run())
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        if runner in done:
            await runner
        else:
            worker.stop()
            await runner
    finally:
        stopper.cancel()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("附件目录配置为 %s", settings.attachments_root)
    asyncio.run(run())
