"""Spec 定义的主数据与基础业务表。运行时任务外键由对应 issue 增量迁移。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
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
