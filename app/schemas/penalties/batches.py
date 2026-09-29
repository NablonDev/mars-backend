"""API schemas for the `process.job_run`/`job_item` batch trigger and status endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field, model_validator

from app.models.enums import JobItemStatus


class PenaltyProjectionBatchRequest(BaseModel):
    """Request body for a `PENALTY_PROJECTION_BATCH` job run over every OPEN purchase order.

    Maps to `JobTaskType.ORDER_RUN`.
    """

    job_type: Literal["PENALTY_PROJECTION_BATCH"] = "PENALTY_PROJECTION_BATCH"
    projection_date: date | None = None
    stacking_mode_override: Literal["SUM", "MAX"] | None = None


class PenaltyMitigationBatchRequest(BaseModel):
    """Request body for a `PENALTY_MITIGATION_BATCH` job run; maps to `JobTaskType.MITIGATION_RUN`."""

    job_type: Literal["PENALTY_MITIGATION_BATCH"] = "PENALTY_MITIGATION_BATCH"


class PenaltyFullRunScope(BaseModel):
    """Which purchase orders a `PENALTY_FULL_RUN_BATCH` job run applies to.

    Either a `purchase_order_status` filter or an explicit `purchase_order_ids` list, not both.
    """

    purchase_order_status: str | None = "OPEN"
    purchase_order_ids: list[UUID] | None = None

    @model_validator(mode="after")
    def _validate_mutually_exclusive(self) -> PenaltyFullRunScope:
        """Reject a request that explicitly sets both `purchase_order_status` and `purchase_order_ids`."""
        if self.purchase_order_ids is not None and "purchase_order_status" in self.model_fields_set:
            raise ValueError(
                "Provide at most one of `purchase_order_status` or `purchase_order_ids`, not both."
            )
        return self


class PenaltyFullRunBatchRequest(BaseModel):
    """Request body for a `PENALTY_FULL_RUN_BATCH` job run.

    `steps` names the subset to run; they always execute in dependency order. Maps to
    `JobTaskType.PENALTY_FULL_RUN`.
    """

    job_type: Literal["PENALTY_FULL_RUN_BATCH"] = "PENALTY_FULL_RUN_BATCH"
    steps: Annotated[
        list[Literal["projection", "projection_summary", "mitigation", "mitigation_summary"]],
        Field(min_length=1),
    ]
    scope: PenaltyFullRunScope = Field(default_factory=PenaltyFullRunScope)
    projection_date: date | None = None


def _default_job_type(value: Any) -> Any:
    """Backfill `job_type=PENALTY_PROJECTION_BATCH` so an empty body still resolves a union member.

    Must sit after `Field(discriminator=...)` in the `Annotated` chain to run early enough.
    """
    if isinstance(value, dict) and "job_type" not in value:
        return {**value, "job_type": "PENALTY_PROJECTION_BATCH"}
    return value


JobRunRequest = Annotated[
    PenaltyProjectionBatchRequest | PenaltyMitigationBatchRequest | PenaltyFullRunBatchRequest,
    Field(discriminator="job_type"),
    BeforeValidator(_default_job_type),
]


class JobRunResponse(BaseModel):
    """Response shape for `POST /job-runs`, confirming the dispatched batch."""

    job_run_id: UUID
    requested_item_count: int
    # Backend and pickup behavior at dispatch time; not an ETA.
    dispatch_mode: str
    execution_note: str


class JobRunStatusCounts(BaseModel):
    """Per-status item counts for a job run."""

    PENDING: int
    RUNNING: int
    SUCCEEDED: int
    DEAD: int


class JobRunStatusResponse(BaseModel):
    """Response shape for `GET /job-runs/{job_run_id}`, summarizing a batch's overall progress."""

    job_run_id: UUID
    requested_item_count: int
    counts: JobRunStatusCounts
    total_items: int
    is_complete: bool


class JobItemResponse(BaseModel):
    """Response shape for one `process.job_item` row within a job run."""

    id: UUID
    item_type: str
    dedupe_key: str | None = None
    status: JobItemStatus
    attempt_count: int
    max_attempts: int
    # Expose only a safe category/message; raw error text stays internal.
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class JobItemListResponse(BaseModel):
    """Paginated response shape for `GET /job-runs/{job_run_id}/items`."""

    job_run_id: UUID
    items: list[JobItemResponse]
    limit: int
    offset: int
