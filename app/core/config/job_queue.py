"""Settings for batch job queue (worker concurrency, backoff, deadlines)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobQueueBackend(StrEnum):
    """Backend that stores and dispatches batch job-queue items."""

    POSTGRES = "postgres"
    SERVICE_BUS = "service_bus"


class JobQueueSettings(BaseSettings):
    """Batch job-queue settings: backend choice, worker concurrency, retry backoff, and deadlines."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    backend: JobQueueBackend = Field(default=JobQueueBackend.POSTGRES, validation_alias="JOB_QUEUE_BACKEND")
    service_bus_namespace: str = Field(  # e.g. "mars-fines.servicebus.windows.net"
        default="", validation_alias="JOB_QUEUE_SERVICE_BUS_NAMESPACE"
    )
    service_bus_queue_name: str = Field(
        default="fine-projection-jobs", validation_alias="JOB_QUEUE_SERVICE_BUS_QUEUE_NAME"
    )
    service_bus_max_wait_seconds: int = Field(
        default=10, validation_alias="JOB_QUEUE_SERVICE_BUS_MAX_WAIT_SECONDS"
    )

    # Worker concurrency controls concurrent Azure OpenAI work. Increase
    # database pool capacity accordingly to avoid workers blocking on sessions.
    worker_concurrency: int = Field(default=5, validation_alias="JOB_QUEUE_WORKER_CONCURRENCY")
    batch_size: int = Field(default=5, validation_alias="JOB_QUEUE_BATCH_SIZE")
    max_attempts: int = Field(default=5, validation_alias="JOB_QUEUE_MAX_ATTEMPTS")
    backoff_base_seconds: int = Field(default=30, validation_alias="JOB_QUEUE_BACKOFF_BASE_SECONDS")
    backoff_cap_seconds: int = Field(default=1800, validation_alias="JOB_QUEUE_BACKOFF_CAP_SECONDS")
    backoff_jitter_seconds: int = Field(default=30, validation_alias="JOB_QUEUE_BACKOFF_JITTER_SECONDS")
    visibility_timeout_seconds: int = Field(
        default=300, validation_alias="JOB_QUEUE_VISIBILITY_TIMEOUT_SECONDS"
    )
    item_deadline_seconds: int = Field(default=600, validation_alias="JOB_QUEUE_ITEM_DEADLINE_SECONDS")
    poll_interval_seconds: int = Field(default=5, validation_alias="JOB_QUEUE_POLL_INTERVAL_SECONDS")
    idle_poll_max_seconds: int = Field(default=30, validation_alias="JOB_QUEUE_IDLE_POLL_MAX_SECONDS")
