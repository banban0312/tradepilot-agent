"""正式订单、明细、额度占用和编号计数器。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8f41c2d6b903"
down_revision: str | None = "3ab7d5e9c102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("task_id", sa.Uuid(), nullable=False))
    op.add_column("orders", sa.Column("approval_id", sa.Uuid(), nullable=False))
    op.add_column("orders", sa.Column("submit_key", sa.String(64), nullable=False))
    op.add_column("orders", sa.Column("approved_version", sa.Integer(), nullable=False))
    op.add_column("orders", sa.Column("approved_content_hash", sa.CHAR(64), nullable=False))
    op.create_foreign_key("fk_orders_task", "orders", "agent_tasks", ["task_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_orders_approval", "orders", "task_approvals", ["approval_id"], ["id"], ondelete="RESTRICT")
    op.create_unique_constraint("uq_orders_task", "orders", ["task_id"])
    op.create_unique_constraint("uq_orders_submit_key", "orders", ["submit_key"])
    op.create_unique_constraint("uq_orders_approval", "orders", ["approval_id"])

    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("contract_line_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("raw_name", sa.Text(), nullable=True),
        sa.Column("specification", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.CheckConstraint("line_no > 0", name="ck_order_items_line_no_positive"),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price > 0", name="ck_order_items_unit_price_positive"),
        sa.CheckConstraint("amount >= 0", name="ck_order_items_amount_nonnegative"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_order_items_order", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["contract_line_id"], ["contract_lines.id"], name="fk_order_items_contract_line", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], name="fk_order_items_material", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("order_id", "line_no", name="pk_order_items"),
    )

    op.create_table(
        "quota_occupations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("submit_key", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("quantities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_quota_occupations_amount_nonnegative"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_quota_occupations_order", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_quota_occupations_task", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approval_id"], ["task_approvals.id"], name="fk_quota_occupations_approval", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"], name="fk_quota_occupations_contract", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_quota_occupations"),
        sa.UniqueConstraint("order_id", name="uq_quota_occupations_order"),
        sa.UniqueConstraint("task_id", "submit_key", name="uq_quota_occupations_submit_key"),
        sa.UniqueConstraint("approval_id", name="uq_quota_occupations_approval"),
    )

    op.create_table(
        "order_number_counters",
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("next_value", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("next_value > 0", name="ck_order_number_counters_next_positive"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], name="fk_order_number_counters_department", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("business_date", "type", "department_id", name="pk_order_number_counters"),
    )


def downgrade() -> None:
    op.drop_table("order_number_counters")
    op.drop_table("quota_occupations")
    op.drop_table("order_items")
    op.drop_constraint("uq_orders_approval", "orders", type_="unique")
    op.drop_constraint("uq_orders_submit_key", "orders", type_="unique")
    op.drop_constraint("uq_orders_task", "orders", type_="unique")
    op.drop_constraint("fk_orders_approval", "orders", type_="foreignkey")
    op.drop_constraint("fk_orders_task", "orders", type_="foreignkey")
    op.drop_column("orders", "approved_content_hash")
    op.drop_column("orders", "approved_version")
    op.drop_column("orders", "submit_key")
    op.drop_column("orders", "approval_id")
    op.drop_column("orders", "task_id")
