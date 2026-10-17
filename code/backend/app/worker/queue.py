"""单进程 Worker 的持久队列轮询、任务锁和崩溃恢复。"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models import TaskJob
from app.tasks.service import (
    MAX_ACTIVE_TASKS,
    StaleExecutionEpoch,
    TaskExecutionLease,
    TaskLockUnavailable,
    recover_abandoned_jobs,
    try_claim_job,
)

logger = logging.getLogger("tradepilot.worker")
JobHandler = Callable[[TaskExecutionLease, TaskJob], Awaitable[None]]


class LeaseHeartbeatLost(RuntimeError):
    """Worker 无法证明作业租约仍有效，必须取消当前执行并交由恢复器处理。"""


class PersistentTaskWorker:
    """仅领取已注册执行器支持的作业，避免把未实现的图流程标成成功。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: async_sessionmaker,
        handlers: dict[str, JobHandler] | None = None,
        worker_id: str | None = None,
        poll_seconds: float = 0.5,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.handlers = handlers or {}
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self.poll_seconds = poll_seconds
        self._slots = asyncio.Semaphore(MAX_ACTIVE_TASKS)
        self._stop = asyncio.Event()

    async def recover(self, *, stale_after: timedelta = timedelta(minutes=2)) -> int:
        """回收时先检查心跳，再尝试 session advisory lock。"""
        async with self.engine.connect() as connection, connection.begin():
            queue_ready = await connection.scalar(
                text("SELECT to_regclass('public.jobs') IS NOT NULL")
            )
            if not queue_ready:
                # 首次启动时 Compose 可能先于 Alembic；迁移完成后轮询会自动恢复。
                return 0
            count = await recover_abandoned_jobs(
                connection, stale_before=datetime.now(UTC) - stale_after
            )
        if count:
            logger.info("已重新排队 %d 个确认失去锁的作业", count)
        return count

    async def run_once(self) -> bool:
        """领取并运行一项已注册作业；返回是否消费了候选作业。"""
        if not self.handlers:
            return False
        await self._slots.acquire()
        job: TaskJob | None = None
        try:
            async with self.session_factory() as session, session.begin():
                job = await try_claim_job(
                    session,
                    worker_id=self.worker_id,
                    supported_kinds=frozenset(self.handlers),
                )
            if job is None:
                return False
            async with self.engine.connect() as connection:
                # session 级 advisory lock 必须跨事务并由此专属连接持有。
                async with connection.begin():
                    pass
                try:
                    lease = await TaskExecutionLease.acquire(
                        connection,
                        task_id=job.task_id,
                        job_id=job.id,
                        worker_id=self.worker_id,
                    )
                except TaskLockUnavailable:
                    async with connection.begin():
                        await connection.execute(
                            text("""UPDATE jobs SET state='READY', worker_id=NULL,
                                      heartbeat_at=NULL, available_at=now() + interval '2 seconds'
                                   WHERE id=:job_id AND state='RUNNING' AND worker_id=:worker_id"""),
                            {"job_id": job.id, "worker_id": self.worker_id},
                        )
                    return True
                try:
                    handler = self.handlers[job.kind]
                    await self._run_with_heartbeat(lease, job, handler)
                    await lease.finish()
                except asyncio.CancelledError:
                    # 不伪报成功；释放数据库锁后由心跳恢复器重新排队原 job/task。
                    raise
                except (StaleExecutionEpoch, LeaseHeartbeatLost):
                    logger.warning(
                        "任务执行租约失效，等待持久恢复协调器处理 job_id=%s", job.id
                    )
                except Exception as exc:
                    logger.exception("作业执行失败，job_id=%s", job.id)
                    failure = {
                        "code": str(getattr(exc, "code", "WORKER_EXECUTION_FAILED")),
                        "type": type(exc).__name__,
                        "retryable": bool(getattr(exc, "retryable", False)),
                    }
                    stage = getattr(exc, "stage", None)
                    if isinstance(stage, str) and stage:
                        failure["stage"] = stage
                    try:
                        await lease.finish(error=failure)
                    except StaleExecutionEpoch:
                        logger.warning(
                            "任务 epoch 已更新，忽略旧 Worker 失败投影 job_id=%s",
                            job.id,
                        )
                finally:
                    try:
                        await lease.release()
                    except Exception:
                        logger.warning(
                            "释放任务锁时连接已断开，job_id=%s", job.id, exc_info=True
                        )
            return True
        finally:
            self._slots.release()

    async def _run_with_heartbeat(
        self, lease: TaskExecutionLease, job: TaskJob, handler: JobHandler
    ) -> None:
        """续租使用独立连接；epoch 失效时立即取消图执行协程。"""
        work = asyncio.create_task(handler(lease, job))
        heartbeat = asyncio.create_task(self._heartbeat_loop(lease))
        try:
            done, _ = await asyncio.wait(
                {work, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                error = heartbeat.exception()
                if error is not None:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise error
            await work
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat_loop(self, lease: TaskExecutionLease) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                async with self.engine.connect() as connection:
                    await lease.heartbeat(connection)
            except Exception as exc:
                raise LeaseHeartbeatLost("Worker 无法续租，已取消当前任务执行") from exc

    async def run(self) -> None:
        """循环恢复与领取；关闭信号会让当前连接释放任务锁。"""
        await self.recover()
        logger.info("持久任务 Worker 已启动，执行器数量=%d", len(self.handlers))
        next_recovery = datetime.now(UTC) + timedelta(seconds=30)
        running: set[asyncio.Task[bool]] = set()
        while not self._stop.is_set():
            if datetime.now(UTC) >= next_recovery:
                await self.recover()
                next_recovery = datetime.now(UTC) + timedelta(seconds=30)
            for task in tuple(running):
                if task.done():
                    running.remove(task)
                    try:
                        task.result()
                    except Exception:
                        logger.exception("Worker 队列循环发生未处理错误")
            if self.handlers and len(running) < MAX_ACTIVE_TASKS:
                running.add(asyncio.create_task(self.run_once()))
                await asyncio.sleep(self.poll_seconds)
            elif running:
                try:
                    await asyncio.wait(
                        running,
                        timeout=self.poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except TimeoutError:
                    pass
            else:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

        if running:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
