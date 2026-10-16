"""持久任务、命令、作业及执行 epoch。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8d31a7b2f40"
down_revision: str | None = "b6c40ea57912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("last_task_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name="fk_sessions_owner", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index("ix_sessions_owner_updated", "sessions", ["owner_id", "updated_at"])
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(16), nullable=False),
        sa.Column("graph_version", sa.String(64), server_default="v1", nullable=False),
        sa.Column("state", sa.String(32), server_default="QUEUED", nullable=False),
        sa.Column("execution_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("projection_version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("current_step", sa.Text(), nullable=True),
        sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("config_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("task_type IN ('SALES', 'PURCHASE')", name="ck_agent_tasks_type"),
        sa.CheckConstraint("state IN ('QUEUED', 'RUNNING', 'WAITING_INPUT', 'WAITING_APPROVAL', 'BLOCKED_RULE', 'FAILED', 'COMPLETED', 'REJECTED')", name="ck_agent_tasks_state"),
        sa.CheckConstraint("execution_epoch >= 0", name="ck_agent_tasks_epoch_nonnegative"),
        sa.CheckConstraint("projection_version >= 0", name="ck_agent_tasks_projection_nonnegative"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], name="fk_agent_tasks_session", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name="fk_agent_tasks_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], name="fk_agent_tasks_department", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_tasks"),
    )
    op.create_index("ix_agent_tasks_owner_created", "agent_tasks", ["owner_id", "created_at"])
    op.create_index("ix_agent_tasks_department_state", "agent_tasks", ["department_id", "state"])
    op.create_foreign_key("fk_sessions_last_task", "sessions", "agent_tasks", ["last_task_id"], ["id"], ondelete="RESTRICT")

    op.create_table(
        "task_commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("target_interrupt_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.CHAR(64), nullable=False),
        sa.Column("state", sa.String(16), server_default="READY", nullable=False),
        sa.Column("result_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("state IN ('READY', 'APPLIED', 'REJECTED', 'SUPERSEDED')", name="ck_task_commands_state"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_task_commands_task", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_task_commands_actor", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_task_commands"),
        sa.UniqueConstraint("task_id", "request_id", name="uq_task_commands_request"),
    )
    op.create_index("ix_task_commands_task_created", "task_commands", ["task_id", "created_at"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), server_default="READY", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("state IN ('READY', 'RUNNING', 'DONE', 'FAILED')", name="ck_jobs_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_jobs_task", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["task_commands.id"], name="fk_jobs_command", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint("command_id", "kind", name="uq_jobs_command_kind"),
    )
    op.create_index("ix_jobs_state_available", "jobs", ["state", "available_at"])
    op.create_index("ix_jobs_task_state", "jobs", ["task_id", "state"])

    # 既有 M1 trace/audit 允许在任务表建立前记录稳定 UUID。NOT VALID 保留历史数据，
    # 但 PostgreSQL 会立即校验新增及更新行；后续数据核对后再选择性 VALIDATE。
    op.execute("ALTER TABLE trace_spans ADD CONSTRAINT fk_trace_spans_task FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON DELETE RESTRICT NOT VALID")
    op.execute("ALTER TABLE task_event_cursors ADD CONSTRAINT fk_task_event_cursors_task FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON DELETE RESTRICT NOT VALID")
    op.execute("ALTER TABLE task_events ADD CONSTRAINT fk_task_events_task FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON DELETE RESTRICT NOT VALID")
    op.execute("ALTER TABLE audit_events ADD CONSTRAINT fk_audit_events_task FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON DELETE RESTRICT NOT VALID")


def downgrade() -> None:
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT fk_audit_events_task")
    op.execute("ALTER TABLE task_events DROP CONSTRAINT fk_task_events_task")
    op.execute("ALTER TABLE task_event_cursors DROP CONSTRAINT fk_task_event_cursors_task")
    op.execute("ALTER TABLE trace_spans DROP CONSTRAINT fk_trace_spans_task")
    op.drop_index("ix_jobs_task_state", table_name="jobs")
    op.drop_index("ix_jobs_state_available", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_task_commands_task_created", table_name="task_commands")
    op.drop_table("task_commands")
    op.drop_constraint("fk_sessions_last_task", "sessions", type_="foreignkey")
    op.drop_index("ix_agent_tasks_department_state", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_owner_created", table_name="agent_tasks")
    op.drop_table("agent_tasks")
    op.drop_index("ix_sessions_owner_updated", table_name="sessions")
    op.drop_table("sessions")
