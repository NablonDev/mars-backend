"""Settings for penalty dispute resolution (response-window defaults)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DisputeSettings(BaseSettings):
    """Settings for the penalty dispute lifecycle."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # Days from dispute-open to response_due_date, used when no retailer
    # agreement has a dispute_window_days set, or none is currently effective.
    default_window_days: int = Field(default=90, validation_alias="DISPUTE_DEFAULT_WINDOW_DAYS")
