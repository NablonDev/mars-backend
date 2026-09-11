"""Shared job and summary enums used across persistence, API, and worker layers."""

from __future__ import annotations

from enum import StrEnum


class JobItemStatus(StrEnum):
    """Execution lifecycle for a job item."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    DEAD = "DEAD"


class JobTaskType(StrEnum):
    """`process.job_item.item_type` values, shared by every domain."""

    ORDER_RUN = "ORDER_RUN"
    PROJECTION_SUMMARY_REGEN = "PROJECTION_SUMMARY_REGEN"
    MITIGATION_SUMMARY_REGEN = "MITIGATION_SUMMARY_REGEN"
    # Computes and persists fresh mitigation options against the purchase
    # order's latest projection. MITIGATION_SUMMARY_REGEN, by contrast, only
    # regenerates the LLM summary over options that already exist.
    MITIGATION_RUN = "MITIGATION_RUN"
    # Regenerates only the LLM narrative for an already-ANALYZED or terminal
    # penalty_dispute row; the verdict itself is never recomputed here.
    DISPUTE_SUMMARY_REGEN = "DISPUTE_SUMMARY_REGEN"
    EMAIL_INGEST = "EMAIL_INGEST"
    PO_VALIDATION = "PO_VALIDATION"
    # Dispatched from job_type=PENALTY_FULL_RUN_BATCH, one item per matching
    # purchase order. Each runs its requested subset of the projection,
    # projection_summary, mitigation and mitigation_summary steps (stored in
    # process.job_item.metadata) in that fixed dependency order.
    PENALTY_FULL_RUN = "PENALTY_FULL_RUN"


class JobRunType(StrEnum):
    """`process.job_run.trigger_type` values: how a batch run was started."""

    SCHEDULED_DAILY = "SCHEDULED_DAILY"
    MANUAL_BATCH = "MANUAL_BATCH"
    ON_DEMAND = "ON_DEMAND"


class SummaryStatus(StrEnum):
    """Persistence state shared by every penalty summary record."""

    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class SummaryType(StrEnum):
    """Discriminator for penalty_summary.summary_type: PROJECTION, MITIGATION, or DISPUTE."""

    PROJECTION = "PROJECTION"
    MITIGATION = "MITIGATION"
    DISPUTE = "DISPUTE"


class DisputeStatus(StrEnum):
    """`penalties.penalty_dispute.dispute_status` lifecycle.

    OPEN -> ANALYZED (engine verdict persisted) -> terminal RESOLVED (human
    accepted) or OVERRIDDEN (human chose another, with `override_reason`).
    """

    OPEN = "OPEN"
    ANALYZED = "ANALYZED"
    RESOLVED = "RESOLVED"
    OVERRIDDEN = "OVERRIDDEN"


class DisputeVerdict(StrEnum):
    """Dispute outcomes, always computed by the deterministic engine, never the LLM."""

    NO_PAY = "NO_PAY"
    PAY_PARTIAL = "PAY_PARTIAL"
    PAY_FULL = "PAY_FULL"


class DisputeReasonCode(StrEnum):
    """Retailer or ops grounds for disputing a charge.

    Recorded for audit and passed to the dispute-summary LLM as context; the
    deterministic engine never reads it.
    """

    AMOUNT_INCORRECT = "AMOUNT_INCORRECT"
    NOT_LATE = "NOT_LATE"
    QTY_CONFIRMED = "QTY_CONFIRMED"
    RULE_MISAPPLIED = "RULE_MISAPPLIED"
    OTHER = "OTHER"


class AgentDomain(StrEnum):
    """`process.agent.domain` values, lowercase unlike this module's other enums."""

    CMIR = "cmir"
    PENALTIES = "penalties"


class WorkflowThreadSubjectType(StrEnum):
    """`process.workflow_thread_subject.subject_type` values, one per domain that owns a review thread."""

    EMAIL_EVENT = "EMAIL_EVENT"
    PURCHASE_ORDER_LINE = "PURCHASE_ORDER_LINE"
    RETAILER_AGREEMENT = "RETAILER_AGREEMENT"
