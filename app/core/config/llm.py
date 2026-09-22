"""Azure OpenAI settings for LLM integrations."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AzureOpenAISettings(BaseSettings):
    """Azure OpenAI deployment, timeout, retry, and rate-limit-backoff settings."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    api_key: str = Field(default="", validation_alias="AZURE_OPENAI_API_KEY")
    endpoint: str = Field(default="", validation_alias="AZURE_OPENAI_ENDPOINT")
    deployment: str = Field(default="", validation_alias="AZURE_OPENAI_DEPLOYMENT_NAME")
    timeout_seconds: float = Field(default=90.0, validation_alias="AZURE_OPENAI_TIMEOUT_SECONDS")
    max_attempts: int = Field(default=3, validation_alias="AZURE_OPENAI_MAX_ATTEMPTS")
    max_retries: int = Field(default=3, validation_alias="AZURE_OPENAI_MAX_RETRIES")
    temperature: float = Field(default=0.0, validation_alias="AZURE_OPENAI_TEMPERATURE")

    # Shared rate-limit backoff across concurrent LLM calls. Per-call SDK retries
    # are insufficient under fan-out because concurrent workers can otherwise
    # retry in synchronized waves.
    rate_limit_backoff_seconds: int = Field(
        default=60, validation_alias="AZURE_OPENAI_RATE_LIMIT_BACKOFF_SECONDS"
    )
