"""API schemas for rules, projections, mitigations, batch job runs, and the admin endpoints."""

from __future__ import annotations

from app.schemas.penalties.admin import (
    ScenarioDayResult,
    ScenarioSummary,
    SeedDataResponse,
    SimulateDailyRunResponse,
)
from app.schemas.penalties.batches import (
    JobItemListResponse,
    JobItemResponse,
    JobRunRequest,
    JobRunResponse,
    JobRunStatusCounts,
    JobRunStatusResponse,
)
from app.schemas.penalties.mitigations import (
    MitigationOptionDetailResponse,
    MitigationOptionResponse,
    MitigationOptionsResponse,
    PenaltyMitigationSummaryRequest,
    PenaltyMitigationSummaryResponse,
    PenaltyMitigationSummaryStatusResponse,
)
from app.schemas.penalties.projections import (
    PenaltyExposureResponse,
    PenaltyProjectionDetailResponse,
    PenaltyProjectionHistoryRow,
    PenaltyProjectionResultResponse,
    PenaltyProjectionRunRequest,
    PenaltyProjectionSummaryRequest,
    PenaltyProjectionSummaryResponse,
    PenaltyProjectionSummaryStatusResponse,
    ViolationResponse,
)
from app.schemas.penalties.rule_extraction import (
    ContractCreateRequest,
    ContractResponse,
    ExtractedPenaltyRuleAttributeResponse,
    ExtractedPenaltyRuleResponse,
    ExtractedPenaltyRuleReviewRequest,
    ExtractionStartResponse,
    RulePublicationOutcomeResponse,
    RulePublicationResultResponse,
)
from app.schemas.penalties.rules import PenaltyRuleRequest, PenaltyRuleResponse, PenaltyRuleTierSchema

__all__ = [
    "ContractCreateRequest",
    "ContractResponse",
    "ExtractedPenaltyRuleAttributeResponse",
    "ExtractedPenaltyRuleResponse",
    "ExtractedPenaltyRuleReviewRequest",
    "ExtractionStartResponse",
    "JobItemListResponse",
    "JobItemResponse",
    "JobRunRequest",
    "JobRunResponse",
    "JobRunStatusCounts",
    "JobRunStatusResponse",
    "MitigationOptionDetailResponse",
    "MitigationOptionResponse",
    "MitigationOptionsResponse",
    "PenaltyExposureResponse",
    "PenaltyMitigationSummaryRequest",
    "PenaltyMitigationSummaryResponse",
    "PenaltyMitigationSummaryStatusResponse",
    "PenaltyProjectionDetailResponse",
    "PenaltyProjectionHistoryRow",
    "PenaltyProjectionResultResponse",
    "PenaltyProjectionRunRequest",
    "PenaltyProjectionSummaryRequest",
    "PenaltyProjectionSummaryResponse",
    "PenaltyProjectionSummaryStatusResponse",
    "PenaltyRuleRequest",
    "PenaltyRuleResponse",
    "PenaltyRuleTierSchema",
    "RulePublicationOutcomeResponse",
    "RulePublicationResultResponse",
    "ScenarioDayResult",
    "ScenarioSummary",
    "SeedDataResponse",
    "SimulateDailyRunResponse",
    "ViolationResponse",
]
