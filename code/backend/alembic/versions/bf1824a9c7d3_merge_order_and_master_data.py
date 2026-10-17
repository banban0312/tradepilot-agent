"""合并正式订单与主数据两条迁移分支。"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "bf1824a9c7d3"
down_revision: tuple[str, str] = ("8f41c2d6b903", "e24d903f7a11")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """两条依赖迁移均已应用，本修订只收敛 Alembic 历史。"""


def downgrade() -> None:
    """回滚时仅移除合并点，保留两条已完成的父迁移。"""
