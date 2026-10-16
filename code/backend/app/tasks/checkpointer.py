"""让 LangGraph 检查点写入与任务 epoch 校验处于同一 PostgreSQL 事务。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres import _ainternal
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncCursor
from psycopg.rows import DictRow, dict_row

from app.config import settings
from app.tasks.service import StaleExecutionEpoch, TaskExecutionLease

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class EpochFencedPostgresSaver(AsyncPostgresSaver):
    """绑定单任务专属连接；过期执行器的 checkpoint/writes 在提交前被拒绝。"""

    def __init__(
        self, lease: TaskExecutionLease, *, checkpoint_schema: str | None = None
    ) -> None:
        # LangGraph 仅接受 psycopg 连接；取 SQLAlchemy 专属连接内部的 driver
        # connection，确保 advisory lock 与 checkpoint 写入使用同一个 PG session。
        driver_connection = lease.connection.sync_connection.connection.driver_connection
        super().__init__(driver_connection, pipe=None)
        self.task_id = lease.task_id
        self.execution_epoch = lease.epoch
        self.checkpoint_schema = (
            checkpoint_schema if checkpoint_schema is not None else settings.checkpoint_schema
        )
        if not _SCHEMA_NAME.fullmatch(self.checkpoint_schema):
            raise ValueError("检查点 schema 名称无效")
        self._setup_mode = False

    async def setup(self) -> None:
        """建立独立命名空间，再由官方 saver 创建其维护的内部表。"""
        connection = self.conn
        previous_autocommit = connection.autocommit
        self._setup_mode = True
        lock_key = f"tradepilot:checkpoint-setup:{self.checkpoint_schema}"
        setup_lock_acquired = False
        try:
            # 官方迁移含 CREATE INDEX CONCURRENTLY，须在 autocommit 下运行。
            await connection.set_autocommit(True)
            await connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,)
            )
            setup_lock_acquired = True
            async with connection.cursor(
                binary=True, row_factory=dict_row
            ) as cursor:
                await cursor.execute(
                    f"CREATE SCHEMA IF NOT EXISTS {self.checkpoint_schema}"
                )
            await super().setup()
        finally:
            if setup_lock_acquired:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,)
                )
            self._setup_mode = False
            await connection.set_autocommit(previous_autocommit)

    def _verify_thread(self, config: RunnableConfig) -> None:
        thread_id = config.get("configurable", {}).get("thread_id")
        if thread_id != str(self.task_id):
            raise ValueError("LangGraph thread_id 必须与 task_id 相同")

    @asynccontextmanager
    async def _cursor(
        self, *, pipeline: bool = False
    ) -> AsyncIterator[AsyncCursor[DictRow]]:
        # 上游 saver 的 pipeline 路径不提供可与业务 epoch 锁同事务的扩展点。
        # 对写入路径改用显式事务，并在同一连接、同一事务先锁定任务行核验 epoch。
        async with self.lock, _ainternal.get_connection(self.conn) as connection:
            if self._setup_mode:
                async with connection.cursor(
                    binary=True, row_factory=dict_row
                ) as cursor:
                    await cursor.execute(
                        f"SET search_path TO {self.checkpoint_schema}, public"
                    )
                    try:
                        yield cursor
                    finally:
                        await cursor.execute("RESET search_path")
                return
            if pipeline:
                async with connection.transaction(), connection.cursor(
                    binary=True, row_factory=dict_row
                ) as cursor:
                    await cursor.execute(
                        "SELECT set_config('search_path', %s, true)",
                        (f"{self.checkpoint_schema}, public",),
                    )
                    row = await cursor.execute(
                        "SELECT execution_epoch FROM public.agent_tasks WHERE id = %s FOR UPDATE",
                        (self.task_id,),
                    )
                    current = await row.fetchone()
                    if current is None or current["execution_epoch"] != self.execution_epoch:
                        raise StaleExecutionEpoch("检查点写入 epoch 已过期")
                    yield cursor
            else:
                # 读取也结束自己的短事务，避免一次 aget_state 留下外层事务，
                # 让后续 fencing 写入只落在 savepoint 而未提交。
                async with connection.transaction(), connection.cursor(
                    binary=True, row_factory=dict_row
                ) as cursor:
                    await cursor.execute(
                        "SELECT set_config('search_path', %s, true)",
                        (f"{self.checkpoint_schema}, public",),
                    )
                    yield cursor

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._verify_thread(config)
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._verify_thread(config)
        await super().aput_writes(config, writes, task_id, task_path)
