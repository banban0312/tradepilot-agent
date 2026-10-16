"""Spec 定义的主数据与基础业务表。运行时任务外键由对应 issue 增量迁移。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Alembic 与 ORM 共用的元数据入口。"""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Department(TimestampMixin, Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("code", name="uq_departments_code"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("login", name="uq_users_login"),
        Index("ix_users_department_id", "department_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    login: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)


class UserDepartmentScope(Base):
    __tablename__ = "user_department_scopes"
    __table_args__ = (Index("ix_user_department_scopes_department_id", "department_id"),)

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class AuthSession(Base):
    """仅存会话令牌摘要；CSRF 值由令牌派生，不另存可复用秘密。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_expires", "user_id", "expires_at"),)

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("department_id", "code", name="uq_customers_department_code"),
        UniqueConstraint("id", "department_id", name="uq_customers_id_department"),
        Index("ix_customers_department_id", "department_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    contact: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("department_id", "code", name="uq_projects_department_code"),
        UniqueConstraint("id", "department_id", name="uq_projects_id_department"),
        Index("ix_projects_department_id", "department_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    delivery_address: Mapped[str | None] = mapped_column(Text)


class Material(TimestampMixin, Base):
    __tablename__ = "materials"
    __table_args__ = (UniqueConstraint("code", name="uq_materials_code"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    specification: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class MaterialAlias(TimestampMixin, Base):
    __tablename__ = "material_aliases"
    __table_args__ = (
        UniqueConstraint("material_id", "normalized_alias", name="uq_material_aliases_material_normalized"),
        Index("ix_material_aliases_normalized_alias", "normalized_alias"),
        Index("ix_material_aliases_material_id", "material_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    material_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)


class Contract(TimestampMixin, Base):
    __tablename__ = "contracts"
    __table_args__ = (
        UniqueConstraint("department_id", "number", name="uq_contracts_department_number"),
        UniqueConstraint("id", "department_id", name="uq_contracts_id_department"),
        ForeignKeyConstraint(
            ["customer_id", "department_id"],
            ["customers.id", "customers.department_id"],
            name="fk_contracts_customer_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "department_id"],
            ["projects.id", "projects.department_id"],
            name="fk_contracts_project_department",
            ondelete="RESTRICT",
        ),
        CheckConstraint("type IN ('SALES', 'PURCHASE')", name="ck_contracts_type"),
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_contracts_status"),
        CheckConstraint("total_amount >= 0", name="ck_contracts_total_amount_nonnegative"),
        CheckConstraint("valid_to >= valid_from", name="ck_contracts_valid_date_range"),
        CheckConstraint("version > 0", name="ck_contracts_version_positive"),
        Index("ix_contracts_customer_department", "customer_id", "department_id"),
        Index("ix_contracts_project_department", "project_id", "department_id"),
        Index("ix_contracts_department_type_status", "department_id", "type", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="CNY")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ContractLine(TimestampMixin, Base):
    __tablename__ = "contract_lines"
    __table_args__ = (
        UniqueConstraint("contract_id", "material_id", "unit", name="uq_contract_lines_contract_material_unit"),
        CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="ck_contract_lines_unit_price_nonnegative"),
        CheckConstraint("max_quantity IS NULL OR max_quantity >= 0", name="ck_contract_lines_max_quantity_nonnegative"),
        Index("ix_contract_lines_material_id", "material_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=False
    )
    material_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    max_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))


class ContractBalance(TimestampMixin, Base):
    __tablename__ = "contract_balances"
    __table_args__ = (
        CheckConstraint("occupied_amount >= 0", name="ck_contract_balances_amount_nonnegative"),
        CheckConstraint("occupied_order_count >= 0", name="ck_contract_balances_order_count_nonnegative"),
        CheckConstraint("version >= 0", name="ck_contract_balances_version_nonnegative"),
    )

    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), primary_key=True
    )
    occupied_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, server_default="0"
    )
    occupied_order_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ContractLineBalance(TimestampMixin, Base):
    __tablename__ = "contract_line_balances"
    __table_args__ = (
        CheckConstraint("occupied_quantity >= 0", name="ck_contract_line_balances_nonnegative"),
    )

    contract_line_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contract_lines.id", ondelete="RESTRICT"), primary_key=True
    )
    occupied_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, server_default="0"
    )


class Draft(TimestampMixin, Base):
    """草稿头基础表；任务关联与不可变版本由后续业务 issue 接入。"""

    __tablename__ = "drafts"
    __table_args__ = (
        CheckConstraint("current_version >= 0", name="ck_drafts_current_version_nonnegative"),
        CheckConstraint(
            "lock_state IN ('EDITABLE', 'SUBMITTING', 'SUBMITTED')",
            name="ck_drafts_lock_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lock_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="EDITABLE"
    )


class DraftRevision(Base):
    """不可变的业务草稿快照；每次有效变更递增版本。"""

    __tablename__ = "draft_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["draft_id"], ["drafts.id"], name="fk_draft_revisions_draft", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["changed_by"], ["users.id"], name="fk_draft_revisions_changed_by", ondelete="RESTRICT"
        ),
        CheckConstraint("version > 0", name="ck_draft_revisions_version_positive"),
        CheckConstraint("currency = 'CNY'", name="ck_draft_revisions_currency_cny"),
        CheckConstraint("total_amount IS NULL OR total_amount >= 0", name="ck_draft_revisions_total_nonnegative"),
        CheckConstraint("order_type IS NULL OR order_type IN ('SALES', 'PURCHASE')", name="ck_draft_revisions_order_type"),
        Index("ix_draft_revisions_changed_by", "changed_by"),
    )

    draft_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT")
    )
    order_type: Mapped[str | None] = mapped_column(String(16))
    customer_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT")
    )
    project_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="CNY")
    delivery_date: Mapped[date | None] = mapped_column(Date)
    address: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    business_fields: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    validation: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DraftItem(Base):
    """按草稿版本保存的不可变明细。"""

    __tablename__ = "draft_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["draft_id", "version"],
            ["draft_revisions.draft_id", "draft_revisions.version"],
            name="fk_draft_items_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_draft_items_quantity_nonnegative"),
        CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="ck_draft_items_unit_price_nonnegative"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_draft_items_amount_nonnegative"),
        CheckConstraint("line_no > 0", name="ck_draft_items_line_no_positive"),
        Index("ix_draft_items_material_id", "material_id"),
    )

    draft_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("materials.id", ondelete="RESTRICT")
    )
    raw_name: Mapped[str | None] = mapped_column(Text)
    specification: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    unit: Mapped[str | None] = mapped_column(String(16))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))


class FieldEvidence(Base):
    """字段来源快照；确认人只能由可信服务端身份写入。"""

    __tablename__ = "field_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["draft_id", "version"],
            ["draft_revisions.draft_id", "draft_revisions.version"],
            name="fk_field_evidence_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], name="fk_field_evidence_confirmed_by", ondelete="RESTRICT"
        ),
        Index("ix_field_evidence_draft_version", "draft_id", "version"),
        CheckConstraint(
            "source_type IN ('USER_TEXT', 'FILE_PAGE', 'FILE_CELL', 'MASTER_DATA', 'USER_EDIT', 'SYSTEM_COMPUTED')",
            name="ck_field_evidence_source_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    draft_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    message_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    locator: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    raw_value: Mapped[object | None] = mapped_column(JSONB)
    accepted_value: Mapped[object | None] = mapped_column(JSONB)
    confirmed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class TraceSpan(Base):
    """一次模型、工具、规则、等待或业务动作的真实执行区间。"""

    __tablename__ = "trace_spans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_span_id"],
            ["trace_spans.id"],
            name="fk_trace_spans_parent",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_trace_spans_actor", ondelete="RESTRICT"
        ),
        CheckConstraint("attempt > 0", name="ck_trace_spans_attempt_positive"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_trace_spans_duration_nonnegative",
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'OK', 'ERROR', 'INTERRUPTED')",
            name="ck_trace_spans_status",
        ),
        Index("ix_trace_spans_task_start", "task_id", "start_at"),
        Index("ix_trace_spans_status_start", "status", "start_at"),
        Index("ix_trace_spans_trace", "trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # agent_tasks 在 M2-01 才建立；后续迁移补正式 FK，当前保留稳定的任务标识。
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    parent_span_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    protocol: Mapped[str | None] = mapped_column(String(64))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="RUNNING"
    )
    input_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    output_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class TaskEventCursor(Base):
    """按任务锁定的下一个事件序号；与事件同事务递增。"""

    __tablename__ = "task_event_cursors"
    __table_args__ = (
        CheckConstraint("next_seq > 0", name="ck_task_event_cursors_next_seq_positive"),
    )

    # agent_tasks 由后续任务队列 issue 建立，之后会补充外键。
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    next_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1"
    )


class TaskEvent(Base):
    """供 SSE/回查使用的按任务单调事件记录。"""

    __tablename__ = "task_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["span_id"],
            ["trace_spans.id"],
            name="fk_task_events_span",
            ondelete="SET NULL",
        ),
        Index("ix_task_events_emitted_at", "emitted_at"),
    )

    # agent_tasks 由后续任务队列 issue 建立，之后会补充外键。
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    span_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    """与业务操作同事务写入的追加式责任流水。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_audit_events_actor",
            ondelete="RESTRICT",
        ),
        Index("ix_audit_events_task_created", "task_id", "created_at"),
        Index("ix_audit_events_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    # task_id 在任务表交付后补 FK；审计表保留独立长留存期。
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    before_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    after_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

class Order(TimestampMixin, Base):
    """正式订单基础头表；任务、批准和明细关系按对应 issue 增量添加。"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("number", name="uq_orders_number"),
        ForeignKeyConstraint(
            ["contract_id", "department_id"],
            ["contracts.id", "contracts.department_id"],
            name="fk_orders_contract_department",
            ondelete="RESTRICT",
        ),
        CheckConstraint("type IN ('SALES', 'PURCHASE')", name="ck_orders_type"),
        CheckConstraint("state = 'CREATED'", name="ck_orders_state"),
        CheckConstraint("total_amount >= 0", name="ck_orders_total_amount_nonnegative"),
        Index("ix_orders_department_type", "department_id", "type"),
        Index("ix_orders_contract_department", "contract_id", "department_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    contract_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="CNY")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="CREATED")
