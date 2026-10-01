"""PO-validation snapshot shape for `GET /api/v1/workflow-threads/{thread_id}?include=snapshot`.

`SnapshotHistoryItem` is imported from the cmir package because `human_action` is shared.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from app.schemas.cmir.threads import IsoDatetime, SnapshotHistoryItem


class CandidateInfo(BaseModel):
    """Candidate substitute surfaced when a PO line's material is short or discontinued."""

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
    """PO-validation-domain snapshot shape, returned by `PoValidationService.get_snapshot`."""

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
