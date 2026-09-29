"""CMIR inbound-email (Gmail IMAP) settings."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmailSettings(BaseSettings):
    """CMIR inbound-email (Gmail IMAP) connection and polling settings."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    username: str = Field(default="", validation_alias="EMAIL_USERNAME")
    password: str = Field(default="", validation_alias="EMAIL_PASSWORD")
    imap_server: str = Field(default="imap.gmail.com", validation_alias="EMAIL_IMAP_SERVER")
    imap_port: int = Field(default=993, validation_alias="EMAIL_IMAP_PORT")
    search_subject: str = Field(default="CMIR", validation_alias="EMAIL_SEARCH_SUBJECT")
    lookback_days: int = Field(default=1, validation_alias="EMAIL_LOOKBACK_DAYS")
    max_per_run: int = Field(default=10, validation_alias="EMAIL_MAX_PER_RUN")
