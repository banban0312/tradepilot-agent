"""持久化版本绑定的人工审批决定。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3ab7d5e9c102"
down_revision: str | None = "61a0b3c2f049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_agent_tasks_state", "agent_tasks", type_="check")
    op.create_check_constraint(
        "ck_agent_tasks_state",
        "agent_tasks",
        "state IN ('QUEUED', 'RUNNING', 'WAITING_INPUT', 'WAITING_APPROVAL', 'SUBMITTING', 'BLOCKED_RULE', 'FAILED', 'COMPLETED', 'REJECTED')",
    )
    op.create_table(
        "task_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("checkpoint_ref", sa.Text(), nullable=False),
        sa.Column("interrupt_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('APPROVE', 'RETURN', 'REJECT')", name="ck_task_approvals_decision"),
        sa.CheckConstraint("version > 0", name="ck_task_approvals_version_positive"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_task_approvals_task", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["task_commands.id"], name="fk_task_approvals_command", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["draft_id"], ["drafts.id"], name="fk_task_approvals_draft", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_task_approvals_actor", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], name="fk_task_approvals_department", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_task_approvals"),
        sa.UniqueConstraint("command_id", name="uq_task_approvals_command"),
    )
    op.create_index("ix_task_approvals_task_created", "task_approvals", ["task_id", "created_at"])
    op.create_index("ix_task_approvals_draft_version", "task_approvals", ["draft_id", "version"])


def downgrade() -> None:
    op.drop_index("ix_task_approvals_draft_version", table_name="task_approvals")
    op.drop_index("ix_task_approvals_task_created", table_name="task_approvals")
    op.drop_table("task_approvals")
    op.drop_constraint("ck_agent_tasks_state", "agent_tasks", type_="check")
    op.create_check_constraint(
        "ck_agent_tasks_state",
        "agent_tasks",
        "state IN ('QUEUED', 'RUNNING', 'WAITING_INPUT', 'WAITING_APPROVAL', 'BLOCKED_RULE', 'FAILED', 'COMPLETED', 'REJECTED')",
    )
