"""API schemas for `GET /cmir-records/health` -- CMIR table health snapshot.

Ported from the tail of the flat `app/schemas/cmir.py` on
`fix/cmir-uuid-servicebus-llm` (field names/semantics unchanged -- the
`cmir_record` column shape this reports on is unaffected by the Phase 2/6
restructure; see `app/repositories/cmir/cmir_record.py::get_health_snapshot`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

HealthCategory = Literal["stale_validation", "missing_required_field", "duplicate_row"]


class UnhealthyCategoryCount(BaseModel):
    category: HealthCategory
    count: int


class MonthlyCreationCount(BaseModel):
    month: str
    count: int


class CMIRRecordAttentionItem(BaseModel):
    id: str
    customer_identity: str
    target_customer_material_ref: str
    target_grd_code: str
    brand: str
    site: str
    valid_from: str
    reasons: list[HealthCategory]


class CMIRHealthSnapshotResponse(BaseModel):
    total_current: int
    healthy_current: int
    unhealthy_breakdown: list[UnhealthyCategoryCount]
    creation_trend: list[MonthlyCreationCount]
    needing_attention: list[CMIRRecordAttentionItem]


class HealthTrendPoint(BaseModel):
    """One point of `GET /cmir-records/health-trend` -- see
    `CmirRecordRepository.get_health_trend` for how `total`/`healthy` are
    reconstructed as of each past month-end from real SCD2 columns."""

    month: str
    total: int
    healthy: int


class HousekeepingAuditLogEntry(BaseModel):
    """One row of `GET /cmir-records/housekeeping-audit-log` -- see
    `CmirRecordRepository.get_housekeeping_audit_log` for how `action`/
    `executed_by`/`outcome` are derived from real SCD2/workflow columns."""

    id: str
    timestamp: str
    action: Literal["Create", "Refresh"]
    customer_identity: str
    target_grd_code: str
    target_customer_material_ref: str
    executed_by: str | None = None
    outcome: str | None = None
