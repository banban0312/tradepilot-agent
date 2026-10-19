"""批次异常组保留候选数据并增加编辑版本。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f5b0374c9a1d"
down_revision: str | None = "f4c91b62a7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "import_batch_items",
        sa.Column(
            "candidate_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "import_batch_items",
        sa.Column("edit_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        "ck_import_batch_items_edit_version_positive",
        "import_batch_items",
        "edit_version > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_import_batch_items_edit_version_positive",
        "import_batch_items",
        type_="check",
    )
    op.drop_column("import_batch_items", "edit_version")
    op.drop_column("import_batch_items", "candidate_json")
