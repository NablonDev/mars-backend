"""API schemas for penalty rule extraction, reviewer revisions, and publication.

Schemas for the moved (non-LLM) routes -- retailer agreement create/list/get, extraction
status, extraction runs, extracted-rule reads, review, and revision listing -- now live in
mars-bff. See `docs/API.md` "Retailer agreements and rule extraction".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExtractionStartResponse(BaseModel):
    """Response shape for `POST /penalties/retailer-agreements/{id}/extract`, starting the extraction run."""

    retailer_agreement_id: UUID
    agent_run_id: UUID
    staged_count: int


class RulePublicationOutcomeResponse(BaseModel):
    """One `rule_publication` audit row, as returned in a publication run's result."""

    model_config = ConfigDict(from_attributes=True)

    extracted_rule_id: UUID
    outcome: Literal["PUBLISHED", "REJECTED"]
    penalty_rule_id: UUID | None
    reason_code: str | None
    reason_detail: str | None
    created_at: datetime


class RulePublicationResultResponse(BaseModel):
    """Response shape for `POST /penalties/retailer-agreements/{id}/publish`."""

    retailer_agreement_id: UUID
    agent_run_id: UUID
    published_count: int
    rejected_count: int
    outcomes: list[RulePublicationOutcomeResponse]


class RuleRevisionRequest(BaseModel):
    """Body for `POST /penalties/retailer-agreements/{id}/extracted-rules/{rule_id}/revisions`."""

    instruction: str = Field(min_length=3, max_length=2000)
    requested_by: str | None = Field(default=None, max_length=255)


class RuleRevisionResponse(BaseModel):
    """One `extracted_penalty_rule_revision` row: a reviewer's instruction plus its outcome."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    extracted_rule_id: UUID
    revision_no: int
    instruction: str
    requested_by: str | None
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]
    agent_reply: str | None
    error: str | None
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None
