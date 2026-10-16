"""让 LangGraph 检查点写入与任务 epoch 校验处于同一 PostgreSQL 事务。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres import _ainternal
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncCursor
from psycopg.rows import DictRow, dict_row

from app.tasks.service import StaleExecutionEpoch, TaskExecutionLease


class EpochFencedPostgresSaver(AsyncPostgresSaver):
    """绑定单任务专属连接；过期执行器的 checkpoint/writes 在提交前被拒绝。"""

    def __init__(self, lease: TaskExecutionLease) -> None:
        super().__init__(lease.connection, pipe=None)
        self.task_id = lease.task_id
        self.execution_epoch = lease.epoch

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
            if pipeline:
                async with connection.transaction(), connection.cursor(
                    binary=True, row_factory=dict_row
                ) as cursor:
                    row = await cursor.execute(
                        "SELECT execution_epoch FROM agent_tasks WHERE id = %s FOR UPDATE",
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
