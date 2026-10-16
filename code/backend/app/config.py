"""从本地环境文件或进程环境读取配置，不输出密钥值。"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """各独立进程共用的运行配置。"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr
    attachments_root: Path = Path("/var/lib/tradepilot/attachments")
    mcp_url: str = "http://127.0.0.1:8001/mcp"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    model_provider: str | None = None
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    ocr_provider: str | None = None
    ocr_api_key: SecretStr | None = None
    demo_mode: bool = False
    demo_password: SecretStr | None = None
    session_ttl_hours: int = Field(default=12, gt=0, le=720)
    session_cookie_secure: bool = True
    app_origin: str | None = None

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
