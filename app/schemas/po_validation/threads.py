"""PO-validation-domain snapshot shape for
`GET /api/v1/workflow-threads/{thread_id}?include=snapshot` (was
`GET /threads/{id}/stage` + `GET /threads/{id}/snapshot`, PRD §11.3).

Imports `SnapshotHistoryItem` from `app.schemas.cmir.threads` -- the same
cross-domain import the pre-Phase-7b `app/schemas/po_validation.py` already
made (`human_action` is a shared `process`-schema table, so its history-row
shape is not domain-specific).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from app.schemas.cmir.threads import IsoDatetime, SnapshotHistoryItem


class CandidateInfo(BaseModel):
    sap_material_number: str
    plant: str
    available_quantity: float
    shortfall: float
    suggested_substitute_material_code: str | None = None
    # 2026-09-24: resolved via MasterDataRepository.find_material_master_by_material_id
    # (app/agents/po_validation/nodes.py::human_qty_mismatch_decision) so the UI can
    # show a real substitute name/quantity instead of just a bare code. Both None
    # whenever suggested_substitute_material_code is None -- never fabricated.
    suggested_substitute_description: str | None = None
    suggested_substitute_available_quantity: float | None = None
    # 2026-09-25: common.material.material_code -- distinct from
    # suggested_substitute_material_code above (which is the real, submittable
    # sap_material_number). Display-only, never used for resubmission.
    suggested_substitute_business_material_code: str | None = None


class PoValidationThreadSnapshotResponse(BaseModel):
    """`PoValidationService.get_snapshot`'s real return shape -- structurally
    different from `app.schemas.cmir.threads.CmirThreadSnapshotResponse`
    (PRD §10.4 vs §11.3); see `app/api/v1/workflow_threads.py` for the
    domain-dispatch this forces."""

    agent_run_id: UUID
    thread_id: str
    po_line_id: str
    po_number: str | None = None
    po_line_number: str
    # Was `customer_material_code` (final naming refactor, matches
    # common.purchase_order_line.retailer_material_code) -- breaking response
    # change; the frontend/BFF must be updated to consume this new name (see
    # final report).
    retailer_material_code: str
    order_quantity: float
    stage: str
    candidate: CandidateInfo | None = None
    editable_fields: list[str]
    history: list[SnapshotHistoryItem]
    updated_at: IsoDatetime
