"""App identity, environment, logging, and authentication configuration."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INTERNAL_API_KEY_MIN_LENGTH = 64
_VALID_ENVIRONMENTS = ("production", "staging", "development")
_VALID_LOG_FORMATS = ("json", "text", "pretty")
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class AppSettings(BaseSettings):
    """Application identity, environment, logging level, and internal-API authentication."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    project_name: str = Field(
        default=(
            "Mars Petcare Backend: CMIR Email Resolution, PO Validation, "
            "and Projected Penalties, Mitigation & Dispute Resolution"
        ),
        validation_alias="APP_PROJECT_NAME",
    )
    version: str = Field(default="0.1.0", validation_alias="APP_VERSION")
    environment: str = Field(
        default="development",  # "production" | "staging" | "development"
        validation_alias="APP_ENVIRONMENT",
    )
    docs_enabled: bool = Field(default=True, validation_alias="APP_DOCS_ENABLED")
    log_level: str = Field(default="INFO", validation_alias="APP_LOG_LEVEL")
    log_format: str = Field(default="json", validation_alias="APP_LOG_FORMAT")
    no_color: bool = Field(default=False, validation_alias="APP_NO_COLOR")
    no_bold: bool = Field(default=False, validation_alias="APP_NO_BOLD")
    # Security: shared-secret gate on every route except /health. No default --
    # a missing APP_INTERNAL_API_KEY must fail app startup, never silently
    # accept unauthenticated requests.
    internal_api_key: str = Field(validation_alias="APP_INTERNAL_API_KEY")

    @field_validator("environment", mode="before")
    @classmethod
    def _validate_environment(cls, value: str) -> str:
        """Reject invalid environments; must be exactly 'production', 'staging', or 'development'."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("APP_ENVIRONMENT must not be blank")
        if value not in _VALID_ENVIRONMENTS:
            raise ValueError(
                f"Invalid APP_ENVIRONMENT: {value!r}. Must be one of: {', '.join(_VALID_ENVIRONMENTS)}"
            )
        return value

    @field_validator("log_format", mode="before")
    @classmethod
    def _validate_log_format(cls, value: str) -> str:
        """Ensure log_format is one of the supported formats."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("LOG_FORMAT must not be blank")
        val = value.strip().lower()
        if val not in _VALID_LOG_FORMATS:
            raise ValueError(
                f"Invalid LOG_FORMAT: {value!r}. Must be one of: {', '.join(_VALID_LOG_FORMATS)}"
            )
        return val

    @field_validator("no_color", "no_bold", mode="before")
    @classmethod
    def _validate_bool_flags(cls, value: object) -> bool:
        """Parse boolean flag values from environment strings gracefully."""
        if isinstance(value, str):
            val = value.strip().lower()
            if not val:
                return False
            if val in ("1", "true", "yes", "on"):
                return True
            if val in ("0", "false", "no", "off"):
                return False
        return bool(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Ensure log_level is a valid standard level."""
        if not isinstance(value, str) or not value.strip():
            return "INFO"
        val = value.strip()
        if val not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"Invalid APP_LOG_LEVEL: {value!r}. Must be one of: {', '.join(_VALID_LOG_LEVELS)}"
            )
        return val

    @field_validator("internal_api_key")
    @classmethod
    def _internal_api_key_not_blank(cls, value: str) -> str:
        """Reject a blank or too-short APP_INTERNAL_API_KEY so a misconfigured app fails at startup."""
        if not value or not value.strip():
            raise ValueError("APP_INTERNAL_API_KEY must not be blank")

        if len(value) < _INTERNAL_API_KEY_MIN_LENGTH:
            # Enforce minimum length to match secrets.token_hex(32).
            raise ValueError(
                f"APP_INTERNAL_API_KEY must be at least {_INTERNAL_API_KEY_MIN_LENGTH} characters "
                '(generate with: python -c "import secrets; print(secrets.token_hex(32))")'
            )
        return value
