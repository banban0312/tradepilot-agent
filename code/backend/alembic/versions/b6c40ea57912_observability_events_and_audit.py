"""新增 Trace/Span、任务事件及追加式审计表。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b6c40ea57912"
down_revision: str | None = "a48de6b2f907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trace_spans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        # agent_tasks is introduced in M2-01; keep IDs nullable until that migration.
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("parent_span_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("protocol", sa.String(length=64), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default="RUNNING", nullable=False
        ),
        sa.Column("input_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("attempt > 0", name="ck_trace_spans_attempt_positive"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_trace_spans_duration_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'OK', 'ERROR', 'INTERRUPTED')",
            name="ck_trace_spans_status",
        ),
        sa.ForeignKeyConstraint(
            ["parent_span_id"],
            ["trace_spans.id"],
            name="fk_trace_spans_parent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_trace_spans_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trace_spans_task_start", "trace_spans", ["task_id", "start_at"])
    op.create_index(
        "ix_trace_spans_status_start", "trace_spans", ["status", "start_at"]
    )
    op.create_index("ix_trace_spans_trace", "trace_spans", ["trace_id"])

    op.create_table(
        "task_event_cursors",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("next_seq", sa.BigInteger(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "next_seq > 0", name="ck_task_event_cursors_next_seq_positive"
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "task_events",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("span_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "emitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["span_id"],
            ["trace_spans.id"],
            name="fk_task_events_span",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("task_id", "seq"),
    )
    op.create_index("ix_task_events_emitted_at", "task_events", ["emitted_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("before_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("input_hash", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_audit_events_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_task_created", "audit_events", ["task_id", "created_at"]
    )
    op.create_index(
        "ix_audit_events_actor_created", "audit_events", ["actor_id", "created_at"]
    )

    op.execute(
        """
        CREATE FUNCTION reject_audit_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events are append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION reject_audit_event_mutation()")
    op.drop_index("ix_audit_events_actor_created", table_name="audit_events")
    op.drop_index("ix_audit_events_task_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_task_events_emitted_at", table_name="task_events")
    op.drop_table("task_events")
    op.drop_table("task_event_cursors")
    op.drop_index("ix_trace_spans_trace", table_name="trace_spans")
    op.drop_index("ix_trace_spans_status_start", table_name="trace_spans")
    op.drop_index("ix_trace_spans_task_start", table_name="trace_spans")
    op.drop_table("trace_spans")
