"""Workflow execution state for the PO validation pipeline.

Carries the purchase-order line, CMIR match, material-master check, and human
decisions through the graph nodes. Updated by each node as it processes the
purchase-order line.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict
from uuid import UUID


class POGraphState(TypedDict, total=False):
    """Workflow execution state.

    Holds all data flowing through the PO validation pipeline, from the initial
    CMIR lookup through the material-master availability check, any human
    decisions on a missing mapping or quantity shortfall, and the final
    status transition.
    """

    po_line_id: UUID  # common.purchase_order_line.id
    po_line: dict[str, Any]  # PoLine, as a dict (po_number, retailer_code, plant, order_quantity, ...)
    # `retailer_code`/`retailer_material_code` (renamed from customer_id/
    # customer_material_code) are populated directly from the ingest payload
    # in PoValidationService._run_po_line -- new checkpoints only ever
    # contain these names; app/agents/po_validation/nodes.py::_normalize_po_line
    # is a back-compat shim for checkpoints written before this rename.
    batch_id: str
    run_id: UUID
    thread_id: str  # internal checkpoint key; only promoted to a reviewer-facing
    # workflow_threads.thread_id by the service on first interrupt
    sap_material_number: str | None
    cmir_match_found: bool
    manual_entry_description: str
    material: dict[str, Any]  # matched material_master row, as a dict
    quantity_sufficient: bool
    decision: Literal["use_substitute", "proceed_anyway", "mark_stale"] | None
    error: dict[str, Any] | None
