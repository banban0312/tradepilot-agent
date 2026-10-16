"""从本地环境文件或进程环境读取配置，不输出密钥值。"""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """各独立进程共用的运行配置。"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr
    checkpoint_schema: str = Field(
        default="tradepilot_checkpoints", pattern=r"^[a-z_][a-z0-9_]{0,62}$"
    )
    business_timezone: str = Field(
        default="Asia/Hong_Kong", min_length=1, max_length=64
    )
    attachments_root: Path = Path("/var/lib/tradepilot/attachments")
    mcp_url: str = "http://127.0.0.1:8001/mcp"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    mcp_context_signing_key: SecretStr | None = None
    model_provider: str | None = None
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    model_base_url: str = "https://api.openai.com/v1"
    model_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    model_max_retries: int = Field(default=2, ge=0, le=5)
    model_max_rounds: int = Field(default=8, ge=1, le=50)
    model_max_tool_calls: int = Field(default=20, ge=1, le=100)
    ocr_provider: str | None = None
    ocr_api_key: SecretStr | None = None
    demo_mode: bool = False
    demo_password: SecretStr | None = None
    session_ttl_hours: int = Field(default=12, gt=0, le=720)
    session_cookie_secure: bool = True
    app_origin: str | None = None
    trace_retention_days: int = Field(default=30, ge=1, le=3650)

    @field_validator("business_timezone")
    @classmethod
    def validate_business_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("BUSINESS_TIMEZONE 必须是有效的 IANA 时区名称") from exc
        return value

    @property
    def model_configuration_state(self) -> str:
        """仅报告配置是否齐全，不将其误报为在线连通或功能可用。"""
        if (
            self.model_provider
            and self.model_name
            and self.model_api_key
            and self.model_api_key.get_secret_value().strip()
        ):
            return "configured_unverified"
        return "unconfigured"

    @property
    def ocr_configuration_state(self) -> str:
        """仅报告 OCR 配置是否齐全。"""
        if (
            self.ocr_provider
            and self.ocr_api_key
            and self.ocr_api_key.get_secret_value().strip()
        ):
            return "configured_unverified"
        return "unconfigured"


settings = Settings()
