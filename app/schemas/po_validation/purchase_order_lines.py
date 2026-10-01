"""API schemas for PO-line ingest and the cross-PO `GET /api/v1/purchase-order-lines` listing.

Class names drop the stale `po_line`/`Po*` abbreviation in favor of the full
`purchase_order_line` wording (approved plan's locked-in naming decision),
per §6's "Rename DTO classes dropping stale prefixes where the rest of the
rename already applies elsewhere." Most wire-level payload field names
(`po_number`, `po_line_number`, ...) are read directly by
`PoValidationService._ingest_one_line` (`payload["po_number"]`, ...), a
service-layer contract out of scope for this API-surface phase.

`retailer_code`/`retailer_material_code` (final naming refactor) are the one
exception: previously `customer_id`/`customer_material_code`, renamed to
match the DB/domain vocabulary (`common.retailer.retailer_code`/
`purchase_order_line.retailer_material_code`) -- no DB column changed, only
this application-level contract. See
`app/agents/po_validation/nodes.py::_normalize_po_line` for how already-open
threads (checkpointed before this rename) stay compatible.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.cmir.threads import IsoDatetime
from app.schemas.common.purchase_orders import PurchaseOrderLineResponse


class IngestPurchaseOrderLineItem(BaseModel):
    """One line of an ingest request, as received from the source order system."""

    po_number: str
    po_line_number: str
    # Application-level naming, aligned with the DB/domain vocabulary
    # (common.retailer.retailer_code / purchase_order_line.retailer_material_code)
    # -- was customer_id/customer_material_code (approved final naming refactor;
    # no DB column was renamed, only the API/LangGraph-state layer).
    retailer_code: str
    retailer_material_code: str
    plant: str
    order_quantity: float
    uom: str | None = None
    requested_delivery_date: str | None = None


class IngestPurchaseOrderLinesRequest(BaseModel):
    """Request body for `POST /api/v1/po-validation/purchase-order-lines`."""

    lines: list[IngestPurchaseOrderLineItem] = Field(min_length=1)


class PurchaseOrderLineIngestSummary(BaseModel):
    """Per-line outcome of an ingest request: the line, its status, and its thread if any."""

    po_line_id: str
    batch_id: str
    po_number: str
    po_line_number: str
    status: str
    thread_id: str | None = None
    # `None` on the touchless path; otherwise the thread's real `updated_at`, round-trippable
    # straight into a decision request's `expected_updated_at` without an intermediate GET.
    updated_at: IsoDatetime | None = None


class IngestPurchaseOrderLinesResponse(BaseModel):
    """Response shape for `POST /api/v1/po-validation/purchase-order-lines`."""

    batch_id: str
    total_lines: int
    lines: list[PurchaseOrderLineIngestSummary]


class PurchaseOrderLinesListResponse(BaseModel):
    """Paginated `purchase_order_line` listing, optionally filtered by PO and line status."""

    items: list[PurchaseOrderLineResponse]
    next_cursor: str | None = None


class PoAuditTrailLineResponse(BaseModel):
    """One row of `GET /po-audit-trail` -- the CMIR Intelligence Module's
    "PO Audit Trail" panel. Backed by
    `PurchaseOrderRepository.list_recent_lines`, a genuinely new cross-PO
    query (ordered by `purchase_order_line.created_at desc`) added
    specifically for this panel -- distinct from `PurchaseOrderLinesListResponse`
    above, whose `VIEW_NOT_SUPPORTED` gap is about a filterable, paginated
    cross-PO listing, not a fixed "most recent N" one."""

    po_number: str
    retailer_material_code: str | None = None
    quantity: float
    status: str
    delivery_date: str | None = None
