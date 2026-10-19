"""任务入队与执行租约；事务与持久队列是唯一恢复依据。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models import AgentTask, ConfigSnapshot, TaskCommand, TaskJob, TaskSession
from app.observability.service import append_task_event_on_connection
from app.runtime_config import environment_config, new_snapshot

MAX_ACTIVE_TASKS = 5
JOB_KIND_RUN_TASK = "RUN_TASK"


class IdempotencyConflict(ValueError):
    """同一个 request_id 被用于不同命令内容。"""


class StaleExecutionEpoch(RuntimeError):
    """执行者持有的 epoch 已失效，不得继续写业务状态。"""


class TaskLockUnavailable(RuntimeError):
    """其他执行者正持有该任务的 PostgreSQL 会话锁。"""


def _payload_hash(
    *,
    kind: str,
    payload: dict[str, object],
    expected_version: int | None,
    target_interrupt_id: str | None,
) -> str:
    semantic_request = {
        "kind": kind,
        "payload": payload,
        "expected_version": expected_version,
        "target_interrupt_id": target_interrupt_id,
    }
    canonical = json.dumps(
        semantic_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EnqueuedCommand:
    task_id: UUID
    command_id: UUID
    job_id: UUID
    replayed: bool


async def command_job(session: AsyncSession, command: TaskCommand) -> TaskJob | None:
    """读取命令对应作业；恢复命令保留原作业 ID，即使后续恢复再次复用作业。"""
    job = await session.scalar(
        select(TaskJob).where(
            TaskJob.command_id == command.id, TaskJob.kind == JOB_KIND_RUN_TASK
        )
    )
    if job is not None or command.kind != "RETRY":
        return job
    result = command.result_ref if isinstance(command.result_ref, dict) else {}
    job_id = result.get("job_id")
    if not isinstance(job_id, str):
        return None
    try:
        parsed_job_id = UUID(job_id)
    except ValueError:
        return None
    return await session.scalar(
        select(TaskJob).where(
            TaskJob.id == parsed_job_id,
            TaskJob.task_id == command.task_id,
            TaskJob.kind == JOB_KIND_RUN_TASK,
        )
    )


async def create_task(
    session: AsyncSession,
    *,
    owner_id: UUID,
    department_id: UUID,
    task_type: str,
    title: str | None = None,
    task_id: UUID | None = None,
    session_id: UUID | None = None,
) -> AgentTask:
    """新建采销任务；数据库事务由调用方控制，task_id 同时作为图 thread_id。"""
    if task_type not in {"SALES", "PURCHASE"}:
        raise ValueError("任务类型必须为 SALES 或 PURCHASE")
    if session_id is None:
        task_session = TaskSession(id=uuid4(), owner_id=owner_id, title=title)
        session.add(task_session)
        await session.flush()
    else:
        task_session = await session.scalar(
            select(TaskSession).where(TaskSession.id == session_id).with_for_update()
        )
        if task_session is None or task_session.owner_id != owner_id:
            raise LookupError("任务会话不存在")
    await session.execute(text("SELECT pg_advisory_xact_lock(74192832)"))
    config_snapshot_id = await session.scalar(
        select(ConfigSnapshot.id)
        .where(ConfigSnapshot.is_current.is_(True))
        .order_by(ConfigSnapshot.version.desc())
        .limit(1)
    )
    if config_snapshot_id is None:
        baseline = new_snapshot(content=environment_config(), version=1, actor_id=None)
        session.add(baseline)
        await session.flush()
        config_snapshot_id = baseline.id
    task = AgentTask(
        id=task_id or uuid4(),
        session_id=task_session.id,
        owner_id=owner_id,
        department_id=department_id,
        task_type=task_type,
        state="QUEUED",
        config_snapshot_id=config_snapshot_id,
    )
    session.add(task)
    await session.flush()
    task_session.last_task_id = task.id
    await session.flush()
    return task


async def enqueue_command(
    session: AsyncSession,
    *,
    task_id: UUID,
    actor_id: UUID,
    kind: str,
    payload: dict[str, object],
    request_id: str,
    expected_version: int | None = None,
    target_interrupt_id: str | None = None,
    reuse_job_id: UUID | None = None,
) -> EnqueuedCommand:
    """在一个事务内写命令与作业；request_id 重放不会创建第二项副作用。"""
    if not request_id.strip() or len(request_id) > 128:
        raise ValueError("request_id 必须为 1 至 128 个字符")
    task = await session.scalar(
        select(AgentTask).where(AgentTask.id == task_id).with_for_update()
    )
    if task is None:
        raise LookupError("任务不存在")
    digest = _payload_hash(
        kind=kind,
        payload=payload,
        expected_version=expected_version,
        target_interrupt_id=target_interrupt_id,
    )
    existing = await session.scalar(
        select(TaskCommand).where(
            TaskCommand.task_id == task_id, TaskCommand.request_id == request_id
        )
    )
    if existing is not None:
        if existing.payload_hash != digest:
            raise IdempotencyConflict("request_id 已用于不同命令")
        job = await command_job(session, existing)
        if job is None:
            raise RuntimeError("已保存命令缺少对应作业")
        return EnqueuedCommand(task_id, existing.id, job.id, True)

    command = TaskCommand(
        id=uuid4(),
        task_id=task_id,
        actor_id=actor_id,
        kind=kind,
        payload=payload,
        expected_version=expected_version,
        target_interrupt_id=target_interrupt_id,
        request_id=request_id,
        payload_hash=digest,
    )
    session.add(command)
    if reuse_job_id is None:
        job = TaskJob(
            id=uuid4(),
            task_id=task_id,
            command_id=command.id,
            kind=JOB_KIND_RUN_TASK,
            state="READY",
            attempts=0,
            available_at=datetime.now(UTC),
        )
        session.add(job)
    else:
        job = await session.scalar(
            select(TaskJob)
            .where(
                TaskJob.id == reuse_job_id,
                TaskJob.task_id == task_id,
                TaskJob.kind == JOB_KIND_RUN_TASK,
                TaskJob.state == "FAILED",
            )
            .with_for_update()
        )
        if job is None:
            raise ValueError("只有已失败的原作业可以安全重试")
        # 保留原 job ID，Function Calling 由它派生的 MCP operation_id 才能继续幂等。
        job.command_id = command.id
        job.state = "READY"
        job.available_at = datetime.now(UTC)
        job.worker_id = None
        job.heartbeat_at = None
        job.execution_epoch = None
    if kind == "RETRY":
        # 固定这条幂等命令的作业身份；后续恢复可重用 job.command_id。
        command.result_ref = {"job_id": str(job.id)}
    if task.state not in {"COMPLETED", "REJECTED"}:
        task.state = "QUEUED"
        task.projection_version += 1
    await session.flush()
    return EnqueuedCommand(task_id, command.id, job.id, False)


async def count_active_tasks(session: AsyncSession) -> int:
    """统计全局执行中任务；调用方领取时锁任务并重复检查。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(AgentTask)
            .where(AgentTask.state == "RUNNING")
        )
        or 0
    )


async def try_claim_job(
    session: AsyncSession,
    *,
    worker_id: str,
    supported_kinds: frozenset[str],
    max_active_tasks: int = MAX_ACTIVE_TASKS,
) -> TaskJob | None:
    """使用 SKIP LOCKED 做短事务领取，不在模型/OCR 期间持有行锁。"""
    if not supported_kinds:
        return None
    # 全局短事务锁避免两个 Worker 同时看到 4 个活跃任务并各自领取第 5 个。
    await session.execute(text("SELECT pg_advisory_xact_lock(74192831)"))
    active = int(
        await session.scalar(
            select(func.count(func.distinct(TaskJob.task_id))).where(
                TaskJob.state == "RUNNING"
            )
        )
        or 0
    )
    if active >= max(1, min(MAX_ACTIVE_TASKS, max_active_tasks)):
        return None
    job = await session.scalar(
        select(TaskJob)
        .where(
            TaskJob.state == "READY",
            TaskJob.available_at <= func.now(),
            TaskJob.kind.in_(supported_kinds),
        )
        .order_by(TaskJob.available_at, TaskJob.id)
        .with_for_update(skip_locked=True, of=TaskJob)
        .limit(1)
    )
    if job is None:
        return None
    task = await session.scalar(
        select(AgentTask).where(AgentTask.id == job.task_id).with_for_update()
    )
    if task is None:
        job.state = "FAILED"
        return None
    if task.state in {"COMPLETED", "REJECTED"}:
        job.state = "DONE"
        return None
    job.state = "RUNNING"
    job.worker_id = worker_id
    job.heartbeat_at = datetime.now(UTC)
    job.attempts += 1
    await session.flush()
    return job


@dataclass(slots=True)
class TaskExecutionLease:
    """独占数据库连接持有 session advisory lock，直到任务执行完全结束。"""

    connection: AsyncConnection
    task_id: UUID
    job_id: UUID
    worker_id: str
    epoch: int
    attempt_started_at: datetime

    @classmethod
    async def acquire(
        cls, connection: AsyncConnection, *, task_id: UUID, job_id: UUID, worker_id: str
    ) -> TaskExecutionLease:
        acquired = bool(
            await connection.scalar(
                text(
                    "SELECT pg_try_advisory_lock(hashtextextended(CAST(:task_id AS text), 0))"
                ),
                {"task_id": str(task_id)},
            )
        )
        await connection.commit()
        if not acquired:
            raise TaskLockUnavailable("同一任务已有执行者")
        try:
            async with connection.begin():
                row = (
                    await connection.execute(
                        text("""UPDATE agent_tasks SET execution_epoch = execution_epoch + 1,
                              state = 'RUNNING', projection_version = projection_version + 1,
                              updated_at = now()
                           WHERE id = :task_id
                           RETURNING execution_epoch, projection_version, now()"""),
                        {"task_id": task_id},
                    )
                ).first()
                if row is None:
                    raise LookupError("任务不存在")
                epoch = int(row[0])
                result = await connection.execute(
                    text("""UPDATE jobs SET execution_epoch = :epoch, heartbeat_at = now()
                           WHERE id = :job_id AND task_id = :task_id
                             AND state = 'RUNNING' AND worker_id = :worker_id"""),
                    {
                        "epoch": epoch,
                        "job_id": job_id,
                        "task_id": task_id,
                        "worker_id": worker_id,
                    },
                )
                if result.rowcount != 1:
                    raise StaleExecutionEpoch("作业领取已失效")
                await append_task_event_on_connection(
                    connection,
                    task_id=task_id,
                    event_type="task.status_changed",
                    payload={"state": "RUNNING", "projection_version": int(row[1])},
                )
            return cls(connection, task_id, job_id, worker_id, epoch, row[2])
        except BaseException:
            await connection.execute(
                text(
                    "SELECT pg_advisory_unlock(hashtextextended(CAST(:task_id AS text), 0))"
                ),
                {"task_id": str(task_id)},
            )
            await connection.commit()
            raise

    async def assert_current(self, connection: AsyncConnection | None = None) -> None:
        """在每次业务写入事务内锁定任务行，拒绝旧 epoch。"""
        target = connection or self.connection
        value = await target.scalar(
            text(
                "SELECT execution_epoch FROM agent_tasks WHERE id=:task_id FOR UPDATE"
            ),
            {"task_id": self.task_id},
        )
        if value != self.epoch:
            raise StaleExecutionEpoch("执行 epoch 已被后续恢复接管")

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[AsyncConnection]:
        """业务副作用必须使用此事务，epoch 行锁持有到所有写入一起提交。"""
        async with self.connection.begin():
            await self.assert_current()
            yield self.connection

    @asynccontextmanager
    async def write_session(self) -> AsyncIterator[AsyncSession]:
        """用同一专属连接承载 ORM 写入，epoch 行锁覆盖 ORM 提交。"""
        async with self.connection.begin():
            await self.assert_current()
            session = AsyncSession(
                bind=self.connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
                await session.commit()  # 只释放 ORM savepoint，外层事务随后统一提交。
            except BaseException:
                await session.rollback()
                raise
            finally:
                await session.close()

    async def heartbeat(self, connection: AsyncConnection | None = None) -> None:
        """使用独立短连接续租，避免与图/checkpointer 并发操作专属锁连接。"""

        async def update_heartbeat(target: AsyncConnection) -> None:
            result = await target.execute(
                text("""UPDATE jobs SET heartbeat_at=now()
                       WHERE id=:job_id AND worker_id=:worker_id AND execution_epoch=:epoch
                         AND state='RUNNING'"""),
                {
                    "job_id": self.job_id,
                    "worker_id": self.worker_id,
                    "epoch": self.epoch,
                },
            )
            if result.rowcount != 1:
                raise StaleExecutionEpoch("作业租约已被撤销")

        if connection is None:
            async with self.write_transaction() as target:
                await update_heartbeat(target)
        else:
            async with connection.begin():
                await self.assert_current(connection)
                await update_heartbeat(connection)

    async def project_state(
        self,
        state: str,
        *,
        checkpoint_ref: str | None = None,
        current_step: str | None = None,
    ) -> None:
        """按 epoch 更新投影；仅接受已定义任务状态。"""
        allowed = {
            "QUEUED",
            "RUNNING",
            "WAITING_INPUT",
            "WAITING_APPROVAL",
            "SUBMITTING",
            "BLOCKED_RULE",
            "FAILED",
            "COMPLETED",
            "REJECTED",
        }
        if state not in allowed:
            raise ValueError("任务状态无效")
        async with self.write_transaction() as connection:
            result = await connection.execute(
                text("""UPDATE agent_tasks SET state=:state,
                          checkpoint_ref=COALESCE(:checkpoint_ref, checkpoint_ref),
                          current_step=:current_step,
                          projection_version=projection_version+1, updated_at=now()
                       WHERE id=:task_id AND execution_epoch=:epoch
                       RETURNING projection_version"""),
                {
                    "state": state,
                    "checkpoint_ref": checkpoint_ref,
                    "current_step": current_step,
                    "task_id": self.task_id,
                    "epoch": self.epoch,
                },
            )
            if result.rowcount != 1:
                raise StaleExecutionEpoch("任务状态投影已失效")
            await append_task_event_on_connection(
                connection,
                task_id=self.task_id,
                event_type="task.status_changed",
                payload={
                    "state": state,
                    "projection_version": result.scalar_one(),
                },
            )

    async def finish(self, *, error: dict[str, object] | None = None) -> None:
        async with self.write_transaction() as connection:
            if error is None:
                task_state_row = await connection.execute(
                    text("SELECT state, last_error FROM agent_tasks WHERE id=:task_id"),
                    {"task_id": self.task_id},
                )
                task_state, task_error = task_state_row.one()
                if task_state == "RUNNING":
                    raise ValueError("完成作业前必须投影等待、失败或终态")
                result = await connection.execute(
                    text("""UPDATE jobs SET state='DONE', heartbeat_at=now()
                           WHERE id=:job_id AND task_id=:task_id AND worker_id=:worker_id
                             AND execution_epoch=:epoch AND state='RUNNING'"""),
                    {
                        "job_id": self.job_id,
                        "task_id": self.task_id,
                        "worker_id": self.worker_id,
                        "epoch": self.epoch,
                    },
                )
                if result.rowcount != 1:
                    raise StaleExecutionEpoch("作业完成投影已失效")
                await connection.execute(
                    text(
                        "UPDATE task_commands SET state='APPLIED' WHERE id=(SELECT command_id FROM jobs WHERE id=:job_id) AND state='READY'"
                    ),
                    {"job_id": self.job_id},
                )
                if task_state != "FAILED":
                    await connection.execute(
                        text(
                            "UPDATE agent_tasks SET last_error=NULL WHERE id=:task_id"
                        ),
                        {"task_id": self.task_id},
                    )
                if task_state == "FAILED":
                    await append_task_event_on_connection(
                        connection,
                        task_id=self.task_id,
                        event_type="task.failed",
                        payload={"error": task_error or {}},
                    )
            else:
                safe_error = dict(error)
                failure_span = (
                    await connection.execute(
                        text("""SELECT id, name, kind, status, error
                               FROM trace_spans
                               WHERE task_id=:task_id
                                 AND status IN ('ERROR', 'INTERRUPTED', 'RUNNING')
                                 AND start_at >= :attempt_started_at
                               ORDER BY start_at DESC, id DESC
                               LIMIT 1 FOR UPDATE"""),
                        {
                            "task_id": self.task_id,
                            "attempt_started_at": self.attempt_started_at,
                        },
                    )
                ).first()
                if failure_span is None:
                    synthetic_span_id = uuid4()
                    synthetic_trace_id = uuid4()
                    synthetic_name = str(safe_error.get("stage") or "worker.execution")
                    synthetic_error = {
                        "code": str(
                            safe_error.get("code") or "WORKER_EXECUTION_FAILED"
                        ),
                        "retryable": bool(safe_error.get("retryable", False)),
                    }
                    await connection.execute(
                        text("""INSERT INTO trace_spans
                               (id, trace_id, task_id, kind, name, execution_mode, protocol,
                                attempt, start_at, end_at, duration_ms, status, error)
                               VALUES (:id, :trace_id, :task_id, 'WORKER', :name, 'LIVE',
                                       'internal', 1, now(), now(), 0, 'ERROR', CAST(:error AS jsonb))"""),
                        {
                            "id": synthetic_span_id,
                            "trace_id": synthetic_trace_id,
                            "task_id": self.task_id,
                            "name": synthetic_name,
                            "error": json.dumps(synthetic_error),
                        },
                    )
                    await append_task_event_on_connection(
                        connection,
                        task_id=self.task_id,
                        event_type="span.failed",
                        span_id=synthetic_span_id,
                        payload={
                            "span_id": str(synthetic_span_id),
                            "status": "ERROR",
                            "duration_ms": 0,
                            "error": synthetic_error,
                        },
                    )
                    failure_span = (
                        synthetic_span_id,
                        synthetic_name,
                        "WORKER",
                        "ERROR",
                        synthetic_error,
                    )
                if failure_span is not None:
                    span_id, span_name, span_kind, span_status, span_error = (
                        failure_span
                    )
                    span_status = str(span_status)
                    span_error = span_error if isinstance(span_error, dict) else {}
                    if span_status == "RUNNING":
                        span_error = {
                            "code": str(
                                safe_error.get("code") or "EXECUTION_INTERRUPTED"
                            ),
                            "retryable": bool(safe_error.get("retryable", False)),
                        }
                        await connection.execute(
                            text("""UPDATE trace_spans
                                   SET status='INTERRUPTED', end_at=now(), error=CAST(:error AS jsonb)
                                   WHERE id=:span_id AND status='RUNNING'"""),
                            {
                                "span_id": span_id,
                                "error": json.dumps(span_error),
                            },
                        )
                        span_status = "INTERRUPTED"
                    retryable = bool(safe_error.get("retryable"))
                    if (
                        span_status == "INTERRUPTED"
                        and span_error.get("code") == "EXECUTION_INTERRUPTED"
                    ):
                        retryable = True
                    safe_error["retryable"] = retryable
                    safe_error["failed_span_id"] = str(span_id)
                    safe_error["failure"] = {
                        "span_id": str(span_id),
                        "name": str(span_name),
                        "kind": str(span_kind),
                        "status": span_status,
                        "code": span_error.get("code") or safe_error.get("code"),
                        "retryable": retryable,
                    }
                result = await connection.execute(
                    text("""UPDATE jobs SET state='FAILED', heartbeat_at=now()
                           WHERE id=:job_id AND task_id=:task_id AND worker_id=:worker_id
                             AND execution_epoch=:epoch AND state='RUNNING'"""),
                    {
                        "job_id": self.job_id,
                        "task_id": self.task_id,
                        "worker_id": self.worker_id,
                        "epoch": self.epoch,
                    },
                )
                if result.rowcount != 1:
                    raise StaleExecutionEpoch("失败作业投影已失效")
                task_update = await connection.execute(
                    text("""UPDATE agent_tasks SET state='FAILED', last_error=CAST(:error AS jsonb),
                              projection_version=projection_version+1, updated_at=now()
                           WHERE id=:task_id AND execution_epoch=:epoch
                           RETURNING projection_version"""),
                    {
                        "task_id": self.task_id,
                        "epoch": self.epoch,
                        "error": json.dumps(safe_error, ensure_ascii=False),
                    },
                )
                await connection.execute(
                    text(
                        "UPDATE task_commands SET state='REJECTED' WHERE id=(SELECT command_id FROM jobs WHERE id=:job_id) AND state='READY'"
                    ),
                    {"job_id": self.job_id},
                )
                await append_task_event_on_connection(
                    connection,
                    task_id=self.task_id,
                    event_type="task.status_changed",
                    payload={
                        "state": "FAILED",
                        "projection_version": task_update.scalar_one(),
                    },
                )
                await append_task_event_on_connection(
                    connection,
                    task_id=self.task_id,
                    event_type="task.failed",
                    payload={"error": safe_error},
                )

    async def release(self) -> None:
        await self.connection.execute(
            text(
                "SELECT pg_advisory_unlock(hashtextextended(CAST(:task_id AS text), 0))"
            ),
            {"task_id": str(self.task_id)},
        )
        await self.connection.commit()


async def recover_abandoned_jobs(
    connection: AsyncConnection, *, stale_before: datetime
) -> int:
    """心跳仅发现候选；只有成功取得相同任务锁的协调器才能重新排队。"""
    rows = (
        await connection.execute(
            text("""SELECT id, task_id FROM jobs
               WHERE state='RUNNING' AND heartbeat_at < :stale_before
               ORDER BY heartbeat_at FOR UPDATE SKIP LOCKED"""),
            {"stale_before": stale_before},
        )
    ).all()
    recovered = 0
    for job_id, task_id in rows:
        acquired = bool(
            await connection.scalar(
                text(
                    "SELECT pg_try_advisory_lock(hashtextextended(CAST(:task_id AS text), 0))"
                ),
                {"task_id": str(task_id)},
            )
        )
        if not acquired:
            continue
        try:
            async with connection.begin_nested():
                await connection.execute(
                    text("""UPDATE jobs SET state='READY', worker_id=NULL,
                              heartbeat_at=NULL, execution_epoch=NULL, available_at=now()
                           WHERE id=:job_id AND state='RUNNING'"""),
                    {"job_id": job_id},
                )
                interrupted_spans = (
                    await connection.execute(
                        text("""SELECT id, trace_id, name, kind
                               FROM trace_spans
                               WHERE task_id=:task_id AND status='RUNNING'
                               FOR UPDATE"""),
                        {"task_id": task_id},
                    )
                ).all()
                for span_id, trace_id, span_name, span_kind in interrupted_spans:
                    span_error = {
                        "code": "EXECUTION_INTERRUPTED",
                        "retryable": True,
                    }
                    await connection.execute(
                        text("""UPDATE trace_spans
                               SET status='INTERRUPTED', end_at=now(),
                                   error=CAST(:error AS jsonb)
                               WHERE id=:span_id AND status='RUNNING'"""),
                        {
                            "span_id": span_id,
                            "error": json.dumps(span_error),
                        },
                    )
                    await append_task_event_on_connection(
                        connection,
                        task_id=task_id,
                        event_type="span.failed",
                        span_id=span_id,
                        payload={
                            "span_id": str(span_id),
                            "trace_id": str(trace_id),
                            "name": span_name,
                            "kind": span_kind,
                            "status": "INTERRUPTED",
                            "error": span_error,
                        },
                    )
                task_update = await connection.execute(
                    text("""UPDATE agent_tasks SET execution_epoch=execution_epoch+1,
                              state=CASE WHEN state='RUNNING' THEN 'QUEUED' ELSE state END,
                              projection_version=projection_version+1, updated_at=now()
                           WHERE id=:task_id RETURNING state, projection_version"""),
                    {"task_id": task_id},
                )
                task_state, projection_version = task_update.one()
                await append_task_event_on_connection(
                    connection,
                    task_id=task_id,
                    event_type="task.status_changed",
                    payload={
                        "state": task_state,
                        "projection_version": projection_version,
                        "recovered": True,
                    },
                )
            recovered += 1
        finally:
            await connection.execute(
                text(
                    "SELECT pg_advisory_unlock(hashtextextended(CAST(:task_id AS text), 0))"
                ),
                {"task_id": str(task_id)},
            )
    return recovered
