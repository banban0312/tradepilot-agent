"""运行配置版本及任务快照外键。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8a34c9f10d62"
down_revision: str | None = "f5b0374c9a1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_config_snapshots_version_positive"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_config_snapshots_creator",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="uq_config_snapshots_version"),
    )
    op.create_index("ix_config_snapshots_current", "config_snapshots", ["is_current"])
    op.create_index(
        "ix_config_snapshots_created", "config_snapshots", ["created_at", "id"]
    )
    op.create_index(
        "uq_config_snapshots_single_current",
        "config_snapshots",
        ["is_current"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_foreign_key(
        "fk_agent_tasks_config_snapshot",
        "agent_tasks",
        "config_snapshots",
        ["config_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "runtime_config_secrets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column(
            "cleared", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.CheckConstraint(
            "name IN ('model_api_key', 'ocr_api_key')",
            name="ck_runtime_config_secrets_name",
        ),
        sa.CheckConstraint(
            "(cleared AND ciphertext IS NULL) OR (NOT cleared AND ciphertext IS NOT NULL)",
            name="ck_runtime_config_secrets_state",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["config_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_runtime_config_secrets_snapshot",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id", "name", name="uq_runtime_config_secrets_snapshot_name"
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_config_secrets")
    op.drop_constraint(
        "fk_agent_tasks_config_snapshot", "agent_tasks", type_="foreignkey"
    )
    op.drop_index("ix_config_snapshots_created", table_name="config_snapshots")
    op.drop_index("ix_config_snapshots_current", table_name="config_snapshots")
    op.drop_index("uq_config_snapshots_single_current", table_name="config_snapshots")
    op.drop_table("config_snapshots")
