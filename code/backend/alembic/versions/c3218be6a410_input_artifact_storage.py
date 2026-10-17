"""附件元数据、任务引用与受控存储状态。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3218be6a410"
down_revision: str | None = "bf1824a9c7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "input_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=127), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "state", sa.String(length=16), server_default="STORED", nullable=False
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "byte_size > 0", name="ck_input_artifacts_byte_size_positive"
        ),
        sa.CheckConstraint(
            "state IN ('STORED', 'PROCESSING', 'PARSED', 'FAILED', 'DELETED')",
            name="ck_input_artifacts_state",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_input_artifacts_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_input_artifacts_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_input_artifacts_storage_key"),
    )
    op.create_index(
        "ix_input_artifacts_owner_created",
        "input_artifacts",
        ["owner_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "task_artifacts",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["input_artifacts.id"],
            name="fk_task_artifacts_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_task_artifacts_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", "artifact_id"),
    )
    op.create_index(
        "ix_task_artifacts_artifact_id", "task_artifacts", ["artifact_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_task_artifacts_artifact_id", table_name="task_artifacts")
    op.drop_table("task_artifacts")
    op.drop_index("ix_input_artifacts_owner_created", table_name="input_artifacts")
    op.drop_table("input_artifacts")
