"""将任务绑定到草稿并为 MCP 写工具增加事务幂等账本。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1e7a4c9b602"
down_revision: str | None = "c8d31a7b2f40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("drafts", sa.Column("task_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_drafts_task",
        "drafts",
        "agent_tasks",
        ["task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_drafts_task", "drafts", ["task_id"])
    op.create_table(
        "tool_effects",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("args_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "result_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_tool_effects_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_tool_effects"),
    )
    op.create_index(
        "ix_tool_effects_task_created", "tool_effects", ["task_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tool_effects_task_created", table_name="tool_effects")
    op.drop_table("tool_effects")
    op.drop_constraint("uq_drafts_task", "drafts", type_="unique")
    op.drop_constraint("fk_drafts_task", "drafts", type_="foreignkey")
    op.drop_column("drafts", "task_id")
