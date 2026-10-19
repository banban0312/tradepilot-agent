"""持久化 Excel 批次及订单组到独立任务的关联。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f4c91b62a7d3"
down_revision: str | None = "c3218be6a410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "state", sa.String(length=16), server_default="QUEUED", nullable=False
        ),
        sa.Column(
            "stop_requested",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
            "state IN ('QUEUED', 'RUNNING', 'STOPPING', 'STOPPED', 'COMPLETED', 'PARTIAL')",
            name="ck_import_batches_state",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["input_artifacts.id"],
            name="fk_import_batches_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_import_batches_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_import_batches_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_batches"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_import_batches_request"
        ),
    )
    op.create_index(
        "ix_import_batches_department_created",
        "import_batches",
        ["department_id", "created_at"],
    )
    op.create_table(
        "import_batch_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("sheet_name", sa.Text(), nullable=False),
        sa.Column(
            "source_rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("preview_status", sa.String(length=32), nullable=False),
        sa.Column(
            "issues",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("stop_result", sa.String(length=24), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "preview_status IN ('READY_FOR_REVIEW', 'OVER_QUOTA_REVIEW', 'NEEDS_CORRECTION')",
            name="ck_import_batch_items_preview_status",
        ),
        sa.CheckConstraint(
            "stop_result IS NULL OR stop_result IN ('NOT_STARTED', 'ALREADY_STARTED')",
            name="ck_import_batch_items_stop_result",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["import_batches.id"],
            name="fk_import_batch_items_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_import_batch_items_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_batch_items"),
        sa.UniqueConstraint(
            "batch_id", "sheet_name", "group_id", name="uq_import_batch_items_group"
        ),
        sa.UniqueConstraint("task_id", name="uq_import_batch_items_task"),
    )
    op.create_index(
        "ix_import_batch_items_batch", "import_batch_items", ["batch_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_import_batch_items_batch", table_name="import_batch_items")
    op.drop_table("import_batch_items")
    op.drop_index("ix_import_batches_department_created", table_name="import_batches")
    op.drop_table("import_batches")
