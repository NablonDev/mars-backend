"""Application settings from environment variables, nested by concern (app, database, llm, etc.)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config.app import AppSettings
from app.core.config.cors import CorsSettings
from app.core.config.database import DatabaseSettings
from app.core.config.dispute import DisputeSettings
from app.core.config.email import EmailSettings
from app.core.config.job_queue import JobQueueBackend, JobQueueSettings
from app.core.config.llm import AzureOpenAISettings
from app.core.config.service_bus import ServiceBusSettings
from app.core.config.summary import SummarySettings

# Sentinel distinguishing "no _env_file override passed" from an explicit
# `_env_file=None` (used by tests to isolate Settings from a real local
# .env). Distinct from pydantic-settings' own default so both cases are
# told apart below.
_ENV_FILE_UNSET: Any = object()

_GROUP_CLASSES: dict[str, type[BaseSettings]] = {
    "app": AppSettings,
    "database": DatabaseSettings,
    "llm": AzureOpenAISettings,
    "service_bus": ServiceBusSettings,
    "job_queue": JobQueueSettings,
    "email": EmailSettings,
    "summary": SummarySettings,
    "dispute": DisputeSettings,
    "cors": CorsSettings,
}

__all__ = [
    "AppSettings",
    "AzureOpenAISettings",
    "CorsSettings",
    "DatabaseSettings",
    "DisputeSettings",
    "EmailConfig",
    "EmailSettings",
    "JobQueueBackend",
    "JobQueueSettings",
    "LLMConfig",
    "ServiceBusConfig",
    "ServiceBusSettings",
    "Settings",
    "SummarySettings",
    "get_settings",
]


class Settings(BaseSettings):
    """Root application configuration, composed of one nested settings group per domain.

    Groups app, database, llm, service_bus, job_queue, email, summary,
    dispute, and cors settings under one object. Each group is its own
    pydantic-settings model, so a missing required env var fails at
    Settings() construction time rather than when the field is first read.
    """

    # Each nested group validates its own env vars independently, ensuring
    # required fields still fail at Settings() construction time.
    app: AppSettings = Field(default_factory=lambda: AppSettings())  # type: ignore[call-arg]
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: AzureOpenAISettings = Field(default_factory=AzureOpenAISettings)
    service_bus: ServiceBusSettings = Field(default_factory=ServiceBusSettings)
    job_queue: JobQueueSettings = Field(default_factory=JobQueueSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    summary: SummarySettings = Field(default_factory=SummarySettings)
    dispute: DisputeSettings = Field(default_factory=DisputeSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, *, _env_file: Any = _ENV_FILE_UNSET, **values: Any) -> None:
        # Thread _env_file override to all nested groups so it takes effect
        # end-to-end, not just on the root model.
        if _env_file is _ENV_FILE_UNSET:
            super().__init__(**values)
            return

        for name, group_cls in _GROUP_CLASSES.items():
            if name not in values:
                values[name] = group_cls(_env_file=_env_file)  # type: ignore[call-arg]
        super().__init__(_env_file=_env_file, **values)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance, building it on first call."""
    return Settings()  # type: ignore[call-arg]


@dataclass(frozen=True)
class EmailConfig:
    """Flattened IMAP connection settings for GmailImapReader, derived from EmailSettings."""

    address: str
    password: str
    imap_server: str
    imap_port: int
    search_subject: str = "CMIR"
    lookback_days: int = 1
    max_per_run: int = 10

    @classmethod
    def from_settings(cls, settings: Settings) -> EmailConfig:
        """Build an EmailConfig from the app-wide Settings' email group."""
        return cls(
            address=settings.email.username,
            password=settings.email.password,
            imap_server=settings.email.imap_server,
            imap_port=settings.email.imap_port,
            search_subject=settings.email.search_subject,
            lookback_days=settings.email.lookback_days,
            max_per_run=settings.email.max_per_run,
        )


@dataclass(frozen=True)
class LLMConfig:
    """Flattened Azure OpenAI settings for AzureOpenAICmirExtractor, derived from AzureOpenAISettings."""

    api_key: str
    endpoint: str
    deployment: str
    temperature: float = 0.0
    timeout_seconds: float = 90.0
    max_retries: int = 3

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMConfig:
        """Build an LLMConfig from the app-wide Settings' llm group."""
        return cls(
            api_key=settings.llm.api_key,
            endpoint=settings.llm.endpoint,
            deployment=settings.llm.deployment,
            temperature=settings.llm.temperature,
            timeout_seconds=settings.llm.timeout_seconds,
            max_retries=settings.llm.max_attempts,
        )


@dataclass(frozen=True)
class ServiceBusConfig:
    """Flattened Azure Service Bus settings for ServiceBusMailQueue, derived from ServiceBusSettings."""

    fully_qualified_namespace: str
    queue_name: str
    session_id: str = "mail-processing"
    max_wait_time_seconds: int = 30
    lock_renew_seconds: int = 300
    enqueue_batch_limit: int = 25
    agent_api_base_url: str = "http://127.0.0.1:8000"
    agent_api_timeout_seconds: int = 30
    connection_string: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> ServiceBusConfig:
        """Build a ServiceBusConfig from the app-wide Settings' service_bus group."""
        return cls(
            fully_qualified_namespace=settings.service_bus.namespace,
            queue_name=settings.service_bus.queue_name,
            session_id=settings.service_bus.session_id,
            max_wait_time_seconds=settings.service_bus.max_wait_seconds,
            lock_renew_seconds=settings.service_bus.lock_renew_seconds,
            enqueue_batch_limit=settings.service_bus.enqueue_batch_limit,
            agent_api_base_url=settings.service_bus.agent_api_base_url,
            agent_api_timeout_seconds=settings.service_bus.agent_api_timeout_seconds,
            connection_string=settings.service_bus.connection_string,
        )
