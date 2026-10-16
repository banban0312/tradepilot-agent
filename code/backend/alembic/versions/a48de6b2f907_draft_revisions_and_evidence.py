"""add versioned drafts and field evidence"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a48de6b2f907"
down_revision: str | None = "7a3811f4d2c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft_revisions",
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("order_type", sa.String(length=16), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("currency", sa.CHAR(length=3), server_default="CNY", nullable=False),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column(
            "business_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("total_amount", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_draft_revisions_version_positive"),
        sa.CheckConstraint("currency = 'CNY'", name="ck_draft_revisions_currency_cny"),
        sa.CheckConstraint(
            "order_type IS NULL OR order_type IN ('SALES', 'PURCHASE')",
            name="ck_draft_revisions_order_type",
        ),
        sa.CheckConstraint(
            "total_amount IS NULL OR total_amount >= 0",
            name="ck_draft_revisions_total_nonnegative",
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["drafts.id"], name="fk_draft_revisions_draft", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], name="fk_draft_revisions_changed_by", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("draft_id", "version"),
    )
    op.create_index("ix_draft_revisions_changed_by", "draft_revisions", ["changed_by"], unique=False)

    op.create_table(
        "draft_items",
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=True),
        sa.Column("raw_name", sa.Text(), nullable=True),
        sa.Column("specification", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_draft_items_quantity_nonnegative"),
        sa.CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="ck_draft_items_unit_price_nonnegative"),
        sa.CheckConstraint("amount IS NULL OR amount >= 0", name="ck_draft_items_amount_nonnegative"),
        sa.CheckConstraint("line_no > 0", name="ck_draft_items_line_no_positive"),
        sa.ForeignKeyConstraint(
            ["draft_id", "version"], ["draft_revisions.draft_id", "draft_revisions.version"],
            name="fk_draft_items_revision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("draft_id", "version", "line_no"),
    )
    op.create_index("ix_draft_items_material_id", "draft_items", ["material_id"], unique=False)

    op.create_table(
        "field_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("accepted_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('USER_TEXT', 'FILE_PAGE', 'FILE_CELL', 'MASTER_DATA', 'USER_EDIT', 'SYSTEM_COMPUTED')",
            name="ck_field_evidence_source_type",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id", "version"], ["draft_revisions.draft_id", "draft_revisions.version"],
            name="fk_field_evidence_revision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], name="fk_field_evidence_confirmed_by", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_field_evidence_draft_version", "field_evidence", ["draft_id", "version"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_field_evidence_draft_version", table_name="field_evidence")
    op.drop_table("field_evidence")
    op.drop_index("ix_draft_items_material_id", table_name="draft_items")
    op.drop_table("draft_items")
    op.drop_index("ix_draft_revisions_changed_by", table_name="draft_revisions")
    op.drop_table("draft_revisions")
