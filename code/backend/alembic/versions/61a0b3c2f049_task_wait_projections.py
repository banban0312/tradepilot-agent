"""为 LangGraph 中断建立独立的业务等待投影。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "61a0b3c2f049"
down_revision: str | None = "d1e7a4c9b602"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_waits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_ref", sa.Text(), nullable=False),
        sa.Column("interrupt_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pending_command_id", sa.Uuid(), nullable=True),
        sa.Column("consumed_command_id", sa.Uuid(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('INPUT', 'APPROVAL')", name="ck_task_waits_kind"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_task_waits_task", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pending_command_id"], ["task_commands.id"], name="fk_task_waits_pending_command", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["consumed_command_id"], ["task_commands.id"], name="fk_task_waits_consumed_command", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_task_waits"),
        sa.UniqueConstraint("checkpoint_ref", "interrupt_id", name="uq_task_waits_checkpoint_interrupt"),
    )
    op.create_index("uq_task_waits_active_task", "task_waits", ["task_id"], unique=True, postgresql_where=sa.text("active"))
    op.create_index("ix_task_waits_task_created", "task_waits", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_task_waits_task_created", table_name="task_waits")
    op.drop_index("uq_task_waits_active_task", table_name="task_waits")
    op.drop_table("task_waits")
