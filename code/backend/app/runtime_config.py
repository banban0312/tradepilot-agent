"""脱敏运行配置版本与任务配置绑定。"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models import ConfigSnapshot, RuntimeConfigSecret

CONFIG_FIELDS = (
    "model_provider",
    "model_name",
    "model_base_url",
    "model_timeout_seconds",
    "model_max_retries",
    "model_max_rounds",
    "model_max_tool_calls",
    "max_concurrency",
    "ocr_provider",
    "mcp_url",
    "business_timezone",
    "trace_retention_days",
    "artifact_retention_days",
    "approval_mode",
)


def environment_config() -> dict[str, Any]:
    """返回运行时可管理字段；SecretStr 和其他凭据不进入序列化数据。"""
    values = settings.model_dump(mode="json")
    return {
        **{key: values[key] for key in CONFIG_FIELDS if key in values},
        "approval_mode": "human_required",
    }


async def current_snapshot(session: AsyncSession) -> ConfigSnapshot | None:
    return await session.scalar(
        select(ConfigSnapshot).where(ConfigSnapshot.is_current.is_(True))
    )


async def config_for_snapshot(
    session: AsyncSession, snapshot_id: UUID | None
) -> Settings:
    """Worker 按任务创建时绑定的版本恢复参数，密钥继续来自部署环境。"""
    if snapshot_id is None:
        return settings
    snapshot = await session.get(ConfigSnapshot, snapshot_id)
    if snapshot is None:
        raise RuntimeError("任务绑定的运行配置版本不存在")
    values = {**settings.model_dump(), **snapshot.content}
    records = await session.scalars(
        select(RuntimeConfigSecret).where(
            RuntimeConfigSecret.snapshot_id == snapshot_id
        )
    )
    for record in records:
        if record.cleared:
            values[record.name] = ""
        elif record.ciphertext:
            values[record.name] = decrypt_secret(record.ciphertext)
    return Settings.model_validate(values)


def _fernet() -> Fernet:
    key = settings.runtime_config_encryption_key
    if key is None or not key.get_secret_value():
        raise RuntimeError("未配置运行密钥加密密钥")
    try:
        return Fernet(key.get_secret_value().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("运行密钥加密密钥格式无效") from exc


def secret_storage_ready() -> bool:
    try:
        _fernet()
    except RuntimeError:
        return False
    return True


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            "运行配置凭据无法解密；请检查加密密钥是否与写入时一致"
        ) from exc


async def retention_days(session: AsyncSession, key: str) -> int:
    snapshot = await current_snapshot(session)
    fallback = (
        settings.trace_retention_days
        if key == "trace_retention_days"
        else settings.artifact_retention_days
    )
    value = snapshot.content.get(key) if snapshot else None
    return value if isinstance(value, int) and 1 <= value <= 3650 else fallback


async def max_concurrency(session: AsyncSession) -> int:
    snapshot = await current_snapshot(session)
    value = snapshot.content.get("max_concurrency") if snapshot else None
    return (
        value
        if isinstance(value, int) and 1 <= value <= 5
        else settings.max_concurrency
    )


def content_hash(content: dict[str, Any]) -> str:
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_snapshot(
    *, content: dict[str, Any], version: int, actor_id: UUID | None
) -> ConfigSnapshot:
    return ConfigSnapshot(
        id=uuid4(),
        version=version,
        content=content,
        content_hash=content_hash(content),
        created_by=actor_id,
        is_current=True,
    )
