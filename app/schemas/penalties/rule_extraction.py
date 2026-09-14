"""API schemas for contract upload, penalty rule extraction, review, and publication."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RetailerAgreementCreateRequest(BaseModel):
    """Body for `POST /penalties/retailer-agreements`; `document_sha256` is derived server-side from content."""

    retailer_id: UUID
    contract_code: str
    title: str
    # Required: the sha256 that makes upload idempotent is taken over this text, so an
    # absent body would hash every text-less contract to the same unique key.
    markdown_text: str = Field(min_length=1)
    source_uri: str | None = None
    effective_date: date | None = None
    expiration_date: date | None = None


class RetailerAgreementResponse(BaseModel):
    """Response shape for one `retailer_agreement` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    retailer_id: UUID
    contract_code: str
    title: str
    document_sha256: str
    source_uri: str | None
    effective_date: date | None
    expiration_date: date | None


class ExtractionStartResponse(BaseModel):
    """Response shape for `POST /penalties/retailer-agreements/{id}/extract`, starting the extraction graph.

    `workflow_thread_id` is `None` when nothing in the retailer agreement needed review.
    """

    retailer_agreement_id: UUID
    agent_run_id: UUID
    workflow_thread_id: UUID | None


class ExtractionStatusResponse(BaseModel):
    """Response shape for `GET /penalties/retailer-agreements/{id}/extraction`.

    `status` is `NOT_STARTED` when extraction has never run; otherwise mirrors the
    underlying `workflow_thread` for a run that needed review, or a synthesized
    completed status for a touchless run that never created one.
    """

    model_config = ConfigDict(from_attributes=True)

    retailer_agreement_id: UUID
    agent_run_id: UUID | None
    workflow_thread_id: UUID | None
    status: str
    stage: str | None
    current_node: str | None
    completed_at: datetime | None
    error: str | None


class ExtractedPenaltyRuleAttributeResponse(BaseModel):
    """One extracted fact (rate, threshold, cap, or similar) for an extracted rule."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    branch_no: int
    attribute_role: str
    metric_code: str | None
    metric_denominator: str | None
    operator: str | None
    value: float | None
    value_max: float | None
    value_unit: str | None
    value_status: str
    currency_code: str | None
    basis_type: str | None
    applies_per: str | None
    tier_application: str | None
    cap_scope: str | None
    source_text: str
    confidence: float


class ExtractedPenaltyRuleResponse(BaseModel):
    """One extracted penalty clause plus its attributes, as listed for review."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    retailer_agreement_id: UUID
    agent_run_id: UUID
    section: str | None
    clause_text: str
    clause_fingerprint: str
    penalty_category: str
    calc_type: str
    po_shortage_flag: bool
    po_delay_flag: bool
    pricing_readiness: Literal[
        "READY", "NEEDS_EXTERNAL_FIGURE", "AWAITING_DATA", "UNSUPPORTED_SHAPE", "NOT_A_CHARGE"
    ]
    status: Literal["PENDING_REVIEW", "APPROVED", "REJECTED"]
    confidence: float
    review_notes: str | None
    attributes: list[ExtractedPenaltyRuleAttributeResponse] = Field(default_factory=list)


class ExtractedPenaltyRuleReviewRequest(BaseModel):
    """Body for `POST /penalties/retailer-agreements/{id}/extracted-rules/{rid}/review`."""

    status: Literal["APPROVED", "REJECTED"]
    review_notes: str | None = None


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


class PublicationAuditResponse(BaseModel):
    """Every recorded publication outcome for a retailer agreement plus a rejection-reason histogram."""

    model_config = ConfigDict(from_attributes=True)

    retailer_agreement_id: UUID
    outcomes: list[RulePublicationOutcomeResponse]
    rejection_reason_histogram: dict[str, int]
