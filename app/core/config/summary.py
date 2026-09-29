"""Settings for penalty summary generation (reuse, recovery, batch cadence)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SummarySettings(BaseSettings):
    """Settings for penalty-summary generation: reuse window, recovery sweep, and daily batch cadence."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # Maximum age, in days, for reusing a stored summary when projection
    # outputs remain unchanged.
    max_reuse_days: int = Field(default=7, validation_alias="SUMMARY_MAX_REUSE_DAYS")
    reuse_enabled: bool = Field(default=False, validation_alias="SUMMARY_REUSE_ENABLED")

    # Recovery window for PENDING summary rows whose job item was never
    # committed. The nightly batch re-enqueues eligible rows within this window.
    pending_sweep_days: int = Field(default=3, validation_alias="SUMMARY_PENDING_SWEEP_DAYS")

    # Daily projection+summary cadence (penalties)
    business_timezone: str = Field(default="UTC", validation_alias="SUMMARY_BUSINESS_TIMEZONE")
    daily_run_time: str = Field(default="01:00", validation_alias="SUMMARY_DAILY_RUN_TIME")
