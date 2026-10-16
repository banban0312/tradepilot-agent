"""为标准物料维护增加乐观锁版本。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e24d903f7a11"
down_revision: str | None = "d1e7a4c9b602"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "materials",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        "ck_materials_version_positive", "materials", "version > 0"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_materials_version_positive", "materials", type_="check"
    )
    op.drop_column("materials", "version")
