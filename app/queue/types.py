"""Framework-independent types shared across queue backends.

This module must not depend on SQLAlchemy or a transport SDK. The types form
the data contract between durable job storage and backend-specific dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class ClaimedJob:
    """Immutable representation of a job claimed for execution.

    `receipt` carries opaque backend-specific settlement state that only the backend
    which created the claim may interpret; `item_type` mirrors
    `process.job_item.item_type`, the discriminator every dispatch table routes on.
    Domain-shaped job fields are absent by design: they live in each domain's own
    `job_item_context` table, which workers look up by `job_item_id`.
    """

    job_item_id: UUID
    job_run_id: UUID
    item_type: str
    dedupe_key: str | None
    attempt_count: int
    max_attempts: int
    receipt: object | None = None


@dataclass
class SweepResult:
    """Domain-neutral result of one stranded-PENDING-summary recovery sweep."""

    recovered_count: int
    job_run_id: UUID | None = None


def claimed_job_from_row(
    row: dict[str, Any],
    *,
    receipt: object | None = None,
) -> ClaimedJob:
    """Convert a repository row into the backend-neutral job contract."""
    return ClaimedJob(
        job_item_id=row["id"],
        job_run_id=row["job_run_id"],
        item_type=row["item_type"],
        dedupe_key=row["dedupe_key"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        receipt=receipt,
    )
