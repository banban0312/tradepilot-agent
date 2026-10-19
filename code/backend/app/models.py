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
    text,
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
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
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
        Uuid(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
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
    __table_args__ = (
        Index("ix_user_department_scopes_department_id", "department_id"),
    )

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
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskSession(TimestampMixin, Base):
    """登录身份之上的任务导航容器；会话不承载唯一执行状态。"""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_owner_updated", "owner_id", "updated_at"),
        ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_sessions_owner", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["last_task_id"],
            ["agent_tasks.id"],
            name="fk_sessions_last_task",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    last_task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class ConfigSnapshot(Base):
    """仅保存脱敏运行参数的不可变版本；凭据仍由部署环境注入。"""

    __tablename__ = "config_snapshots"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_config_snapshots_version_positive"),
        UniqueConstraint("version", name="uq_config_snapshots_version"),
        Index("ix_config_snapshots_current", "is_current"),
        Index("ix_config_snapshots_created", "created_at", "id"),
        Index(
            "uq_config_snapshots_single_current",
            "is_current",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_config_snapshots_creator", ondelete="RESTRICT"),
        nullable=True,
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RuntimeConfigSecret(Base):
    """由部署密钥加密的写入式配置凭据；读取接口只公开配置状态。"""

    __tablename__ = "runtime_config_secrets"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "name", name="uq_runtime_config_secrets_snapshot_name"
        ),
        CheckConstraint(
            "name IN ('model_api_key', 'ocr_api_key')",
            name="ck_runtime_config_secrets_name",
        ),
        CheckConstraint(
            "(cleared AND ciphertext IS NULL) OR (NOT cleared AND ciphertext IS NOT NULL)",
            name="ck_runtime_config_secrets_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "config_snapshots.id",
            name="fk_runtime_config_secrets_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    ciphertext: Mapped[str | None] = mapped_column(Text)
    cleared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class AgentTask(TimestampMixin, Base):
    """任务是可恢复的业务执行身份，LangGraph thread_id 固定使用该 id。"""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('SALES', 'PURCHASE')", name="ck_agent_tasks_type"
        ),
        CheckConstraint(
            "state IN ('QUEUED', 'RUNNING', 'WAITING_INPUT', 'WAITING_APPROVAL', 'SUBMITTING', 'BLOCKED_RULE', 'FAILED', 'COMPLETED', 'REJECTED')",
            name="ck_agent_tasks_state",
        ),
        CheckConstraint(
            "execution_epoch >= 0", name="ck_agent_tasks_epoch_nonnegative"
        ),
        CheckConstraint(
            "projection_version >= 0", name="ck_agent_tasks_projection_nonnegative"
        ),
        Index("ix_agent_tasks_owner_created", "owner_id", "created_at"),
        Index("ix_agent_tasks_department_state", "department_id", "state"),
        ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_agent_tasks_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_agent_tasks_owner", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_agent_tasks_department",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    department_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)
    graph_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="v1"
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="QUEUED"
    )
    execution_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    projection_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    checkpoint_ref: Mapped[str | None] = mapped_column(Text)
    current_step: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    config_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "config_snapshots.id",
            name="fk_agent_tasks_config_snapshot",
            ondelete="RESTRICT",
        ),
    )


class InputArtifact(TimestampMixin, Base):
    """受控存储的上传文件元数据；文件名不参与存储路径生成。"""

    __tablename__ = "input_artifacts"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_input_artifacts_storage_key"),
        CheckConstraint("byte_size > 0", name="ck_input_artifacts_byte_size_positive"),
        CheckConstraint(
            "state IN ('STORED', 'PROCESSING', 'PARSED', 'FAILED', 'DELETED')",
            name="ck_input_artifacts_state",
        ),
        Index("ix_input_artifacts_owner_created", "owner_id", "created_at"),
        ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_input_artifacts_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_input_artifacts_department",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    department_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    media_type: Mapped[str] = mapped_column(String(127), nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="STORED"
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False
    )


class ImportBatch(TimestampMixin, Base):
    """可恢复的 Excel 导入批次；相同幂等键只对应一组子任务。"""

    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "state IN ('QUEUED', 'RUNNING', 'STOPPING', 'STOPPED', 'COMPLETED', 'PARTIAL')",
            name="ck_import_batches_state",
        ),
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_import_batches_request"
        ),
        ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_import_batches_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_import_batches_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["artifact_id"],
            ["input_artifacts.id"],
            name="fk_import_batches_artifact",
            ondelete="RESTRICT",
        ),
        Index("ix_import_batches_department_created", "department_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    department_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="QUEUED"
    )
    stop_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class ImportBatchItem(Base):
    """批次订单组及其源行；有效组关联独立 AgentTask/checkpoint。"""

    __tablename__ = "import_batch_items"
    __table_args__ = (
        CheckConstraint(
            "preview_status IN ('READY_FOR_REVIEW', 'OVER_QUOTA_REVIEW', 'NEEDS_CORRECTION')",
            name="ck_import_batch_items_preview_status",
        ),
        CheckConstraint(
            "stop_result IS NULL OR stop_result IN ('NOT_STARTED', 'ALREADY_STARTED')",
            name="ck_import_batch_items_stop_result",
        ),
        CheckConstraint(
            "edit_version > 0", name="ck_import_batch_items_edit_version_positive"
        ),
        UniqueConstraint(
            "batch_id", "sheet_name", "group_id", name="uq_import_batch_items_group"
        ),
        UniqueConstraint("task_id", name="uq_import_batch_items_task"),
        ForeignKeyConstraint(
            ["batch_id"],
            ["import_batches.id"],
            name="fk_import_batch_items_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_import_batch_items_task",
            ondelete="RESTRICT",
        ),
        Index("ix_import_batch_items_batch", "batch_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    group_id: Mapped[str] = mapped_column(Text, nullable=False)
    sheet_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_rows: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    preview_status: Mapped[str] = mapped_column(String(32), nullable=False)
    issues: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    stop_result: Mapped[str | None] = mapped_column(String(24))
    candidate_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    edit_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskArtifact(Base):
    """显式任务引用；只有当前身份可见的任务可以授权读取其附件。"""

    __tablename__ = "task_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_task_artifacts_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["artifact_id"],
            ["input_artifacts.id"],
            name="fk_task_artifacts_artifact",
            ondelete="RESTRICT",
        ),
        Index("ix_task_artifacts_artifact_id", "artifact_id"),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskCommand(Base):
    """持久化用户动作；request_id 重放返回原命令，不重复排队。"""

    __tablename__ = "task_commands"
    __table_args__ = (
        UniqueConstraint("task_id", "request_id", name="uq_task_commands_request"),
        CheckConstraint(
            "state IN ('READY', 'APPLIED', 'REJECTED', 'SUPERSEDED')",
            name="ck_task_commands_state",
        ),
        Index("ix_task_commands_task_created", "task_id", "created_at"),
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_task_commands_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_task_commands_actor",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    expected_version: Mapped[int | None] = mapped_column(Integer)
    target_interrupt_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="READY"
    )
    result_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskJob(Base):
    """可抢占的持久作业；租约超时只用于发现，advisory lock 才决定所有权。"""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("command_id", "kind", name="uq_jobs_command_kind"),
        CheckConstraint(
            "state IN ('READY', 'RUNNING', 'DONE', 'FAILED')", name="ck_jobs_state"
        ),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts_nonnegative"),
        Index("ix_jobs_state_available", "state", "available_at"),
        Index("ix_jobs_task_state", "task_id", "state"),
        ForeignKeyConstraint(
            ["task_id"], ["agent_tasks.id"], name="fk_jobs_task", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["command_id"],
            ["task_commands.id"],
            name="fk_jobs_command",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="READY"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_epoch: Mapped[int | None] = mapped_column(BigInteger)


class TaskWait(Base):
    """当前图中断的业务投影；checkpoint 本身仍由官方 LangGraph 表维护。"""

    __tablename__ = "task_waits"
    __table_args__ = (
        CheckConstraint("kind IN ('INPUT', 'APPROVAL')", name="ck_task_waits_kind"),
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_task_waits_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["pending_command_id"],
            ["task_commands.id"],
            name="fk_task_waits_pending_command",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["consumed_command_id"],
            ["task_commands.id"],
            name="fk_task_waits_consumed_command",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "checkpoint_ref", "interrupt_id", name="uq_task_waits_checkpoint_interrupt"
        ),
        Index(
            "uq_task_waits_active_task",
            "task_id",
            unique=True,
            postgresql_where=text("active"),
        ),
        Index("ix_task_waits_task_created", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    checkpoint_ref: Mapped[str] = mapped_column(Text, nullable=False)
    interrupt_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    draft_version: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    pending_command_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    consumed_command_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskApproval(Base):
    """不可变人工决定；有效性由当前草稿版本与 invalidated_at 共同约束。"""

    __tablename__ = "task_approvals"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVE', 'RETURN', 'REJECT')",
            name="ck_task_approvals_decision",
        ),
        CheckConstraint("version > 0", name="ck_task_approvals_version_positive"),
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_task_approvals_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["command_id"],
            ["task_commands.id"],
            name="fk_task_approvals_command",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["draft_id"],
            ["drafts.id"],
            name="fk_task_approvals_draft",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_task_approvals_actor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_task_approvals_department",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("command_id", name="uq_task_approvals_command"),
        Index("ix_task_approvals_task_created", "task_id", "created_at"),
        Index("ix_task_approvals_draft_version", "draft_id", "version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    draft_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    department_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    checkpoint_ref: Mapped[str] = mapped_column(Text, nullable=False)
    interrupt_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        Uuid(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
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
        Uuid(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    delivery_address: Mapped[str | None] = mapped_column(Text)


class Material(TimestampMixin, Base):
    __tablename__ = "materials"
    __table_args__ = (
        UniqueConstraint("code", name="uq_materials_code"),
        CheckConstraint("version > 0", name="ck_materials_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    specification: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class MaterialAlias(TimestampMixin, Base):
    __tablename__ = "material_aliases"
    __table_args__ = (
        UniqueConstraint(
            "material_id",
            "normalized_alias",
            name="uq_material_aliases_material_normalized",
        ),
        Index("ix_material_aliases_normalized_alias", "normalized_alias"),
        Index("ix_material_aliases_material_id", "material_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    material_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("materials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)


class Contract(TimestampMixin, Base):
    __tablename__ = "contracts"
    __table_args__ = (
        UniqueConstraint(
            "department_id", "number", name="uq_contracts_department_number"
        ),
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
        CheckConstraint(
            "total_amount >= 0", name="ck_contracts_total_amount_nonnegative"
        ),
        CheckConstraint("valid_to >= valid_from", name="ck_contracts_valid_date_range"),
        CheckConstraint("version > 0", name="ck_contracts_version_positive"),
        Index("ix_contracts_customer_department", "customer_id", "department_id"),
        Index("ix_contracts_project_department", "project_id", "department_id"),
        Index("ix_contracts_department_type_status", "department_id", "type", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
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
        UniqueConstraint(
            "contract_id",
            "material_id",
            "unit",
            name="uq_contract_lines_contract_material_unit",
        ),
        CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="ck_contract_lines_unit_price_nonnegative",
        ),
        CheckConstraint(
            "max_quantity IS NULL OR max_quantity >= 0",
            name="ck_contract_lines_max_quantity_nonnegative",
        ),
        Index("ix_contract_lines_material_id", "material_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    material_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("materials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    max_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))


class ContractBalance(TimestampMixin, Base):
    __tablename__ = "contract_balances"
    __table_args__ = (
        CheckConstraint(
            "occupied_amount >= 0", name="ck_contract_balances_amount_nonnegative"
        ),
        CheckConstraint(
            "occupied_order_count >= 0",
            name="ck_contract_balances_order_count_nonnegative",
        ),
        CheckConstraint(
            "version >= 0", name="ck_contract_balances_version_nonnegative"
        ),
    )

    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    occupied_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, server_default="0"
    )
    occupied_order_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ContractLineBalance(TimestampMixin, Base):
    __tablename__ = "contract_line_balances"
    __table_args__ = (
        CheckConstraint(
            "occupied_quantity >= 0", name="ck_contract_line_balances_nonnegative"
        ),
    )

    contract_line_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contract_lines.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    occupied_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, server_default="0"
    )


class MCPToolEffect(Base):
    """副作用工具的幂等账本；结果引用与业务写入同事务提交。"""

    __tablename__ = "tool_effects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_tool_effects_task",
            ondelete="RESTRICT",
        ),
        Index("ix_tool_effects_task_created", "task_id", "created_at"),
    )

    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    result_ref: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Draft(TimestampMixin, Base):
    """草稿头；M2 将每个任务唯一绑定到其可编辑草稿。"""

    __tablename__ = "drafts"
    __table_args__ = (
        CheckConstraint(
            "current_version >= 0", name="ck_drafts_current_version_nonnegative"
        ),
        CheckConstraint(
            "lock_state IN ('EDITABLE', 'SUBMITTING', 'SUBMITTED')",
            name="ck_drafts_lock_state",
        ),
        ForeignKeyConstraint(
            ["task_id"], ["agent_tasks.id"], name="fk_drafts_task", ondelete="RESTRICT"
        ),
        UniqueConstraint("task_id", name="uq_drafts_task"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    lock_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="EDITABLE"
    )


class DraftRevision(Base):
    """不可变的业务草稿快照；每次有效变更递增版本。"""

    __tablename__ = "draft_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["draft_id"],
            ["drafts.id"],
            name="fk_draft_revisions_draft",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["changed_by"],
            ["users.id"],
            name="fk_draft_revisions_changed_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="ck_draft_revisions_version_positive"),
        CheckConstraint("currency = 'CNY'", name="ck_draft_revisions_currency_cny"),
        CheckConstraint(
            "total_amount IS NULL OR total_amount >= 0",
            name="ck_draft_revisions_total_nonnegative",
        ),
        CheckConstraint(
            "order_type IS NULL OR order_type IN ('SALES', 'PURCHASE')",
            name="ck_draft_revisions_order_type",
        ),
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
    business_fields: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
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
        CheckConstraint(
            "quantity IS NULL OR quantity >= 0",
            name="ck_draft_items_quantity_nonnegative",
        ),
        CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="ck_draft_items_unit_price_nonnegative",
        ),
        CheckConstraint(
            "amount IS NULL OR amount >= 0", name="ck_draft_items_amount_nonnegative"
        ),
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
            ["confirmed_by"],
            ["users.id"],
            name="fk_field_evidence_confirmed_by",
            ondelete="RESTRICT",
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
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_trace_spans_task",
            ondelete="RESTRICT",
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
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_task_event_cursors_task",
            ondelete="RESTRICT",
        ),
    )

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
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_task_events_task",
            ondelete="RESTRICT",
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
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_audit_events_task",
            ondelete="RESTRICT",
        ),
        Index("ix_audit_events_task_created", "task_id", "created_at"),
        Index("ix_audit_events_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
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
    """正式订单头；创建时绑定唯一任务、批准快照和幂等提交键。"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("number", name="uq_orders_number"),
        UniqueConstraint("task_id", name="uq_orders_task"),
        UniqueConstraint("submit_key", name="uq_orders_submit_key"),
        UniqueConstraint("approval_id", name="uq_orders_approval"),
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
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_approvals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    submit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_version: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    contract_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="CNY")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="CREATED"
    )


class OrderItem(Base):
    """正式订单行保存批准时的不可变业务快照。"""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("line_no > 0", name="ck_order_items_line_no_positive"),
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price > 0", name="ck_order_items_unit_price_positive"),
        CheckConstraint("amount >= 0", name="ck_order_items_amount_nonnegative"),
        ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_items_order",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["contract_line_id"],
            ["contract_lines.id"],
            name="fk_order_items_contract_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="fk_order_items_material",
            ondelete="RESTRICT",
        ),
    )

    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_line_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    material_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    raw_name: Mapped[str | None] = mapped_column(Text)
    specification: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)


class QuotaOccupation(Base):
    """正式订单的唯一额度占用流水；与订单及余额聚合在同一事务提交。"""

    __tablename__ = "quota_occupations"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_quota_occupations_order"),
        UniqueConstraint(
            "task_id", "submit_key", name="uq_quota_occupations_submit_key"
        ),
        UniqueConstraint("approval_id", name="uq_quota_occupations_approval"),
        ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_quota_occupations_order",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            name="fk_quota_occupations_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approval_id"],
            ["task_approvals.id"],
            name="fk_quota_occupations_approval",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.id"],
            name="fk_quota_occupations_contract",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount >= 0", name="ck_quota_occupations_amount_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    approval_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    contract_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    quantities: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrderNumberCounter(Base):
    """按订单方向、部门和业务日串行生成正式订单号。"""

    __tablename__ = "order_number_counters"
    __table_args__ = (
        CheckConstraint(
            "next_value > 0", name="ck_order_number_counters_next_positive"
        ),
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_order_number_counters_department",
            ondelete="RESTRICT",
        ),
    )

    business_date: Mapped[date] = mapped_column(Date, primary_key=True)
    type: Mapped[str] = mapped_column(String(16), primary_key=True)
    department_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
