from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    workbook_path: Path = Field(alias="WORKBOOK_PATH")
    workbook_output_dir: Path = Field(alias="WORKBOOK_OUTPUT_DIR")
    workbook_backup_dir: Path = Field(alias="WORKBOOK_BACKUP_DIR")
    authorized_telegram_user_ids: list[int] = Field(alias="AUTHORIZED_TELEGRAM_USER_IDS")
    export_filename_prefix: str = Field(default="Jess_Lombard_Junior_Dance_Centre_Accounting", alias="EXPORT_FILENAME_PREFIX")
    backup_retention_count: int = Field(default=20, alias="BACKUP_RETENTION_COUNT")
    default_batch_mode: bool = Field(default=False, alias="DEFAULT_BATCH_MODE")
    default_dry_run_mode: bool = Field(default=False, alias="DEFAULT_DRY_RUN_MODE")
    data_dir: Path = Field(default=Path("data"), alias="DATA_DIR")
    sqlite_path: Path = Field(default=Path("data/audit.sqlite3"), alias="SQLITE_PATH")
    max_upload_bytes: int = Field(default=15 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")
    uploads_dir: Path = Field(default=Path("data/uploads"), alias="UPLOADS_DIR")
    previews_dir: Path = Field(default=Path("data/previews"), alias="PREVIEWS_DIR")
    rate_limit_window_seconds: int = Field(default=60, alias="RATE_LIMIT_WINDOW_SECONDS")
    rate_limit_max_uploads: int = Field(default=8, alias="RATE_LIMIT_MAX_UPLOADS")

    @field_validator("authorized_telegram_user_ids", mode="before")
    @classmethod
    def parse_authorized_ids(cls, value: str | list[int]) -> list[int]:
        if isinstance(value, list):
            return value
        return [int(item.strip()) for item in str(value).split(",") if item.strip()]

    @field_validator("openai_base_url", mode="before")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.workbook_output_dir,
            self.workbook_backup_dir,
            self.uploads_dir,
            self.previews_dir,
            self.sqlite_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(dotenv_path: str | None = None) -> Settings:
    load_dotenv(dotenv_path=dotenv_path)
    settings = Settings.model_validate(os.environ)
    settings.ensure_directories()
    return settings
