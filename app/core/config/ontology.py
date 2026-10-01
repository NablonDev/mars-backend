"""Settings for the CMIR <-> Material Master traceability graph
(`app.services.ontology`). Read by the in-process background rebuild task
started from `app.main`'s `lifespan`.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OntologySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    refresh_interval_seconds: int = Field(default=300, validation_alias="ONTOLOGY_REFRESH_INTERVAL_SECONDS")
