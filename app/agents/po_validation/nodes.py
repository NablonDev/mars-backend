from __future__ import annotations

import functools
from typing import Any, Literal

from langgraph.types import interrupt

from app.agents.po_validation.state import POGraphState
from app.repositories.cmir.cmir_record import CmirRecordRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.process.workflow import ProcessingErrorRepository


def _normalize_po_line(po_line: dict[str, Any]) -> dict[str, Any]:
    """Back-compat shim for LangGraph checkpoints written before the
    customer_id/customer_material_code -> retailer_code/retailer_material_code
    rename (approved final naming refactor). New checkpoints only ever
    contain the new keys (PoValidationService._run_po_line has been updated
    to write them) -- this only fires for threads that were already parked
    at an interrupt before the rename and are now being resumed, so their
    stored `po_line` state still has the old key names. Does not mutate the
    database or the persisted checkpoint row itself; only normalizes the
    in-memory dict for this node invocation's read."""
    if "retailer_code" not in po_line and "customer_id" in po_line:
        po_line["retailer_code"] = po_line["customer_id"]
    if "retailer_material_code" not in po_line and "customer_material_code" in po_line:
        po_line["retailer_material_code"] = po_line["customer_material_code"]
    return po_line


def _capture_errors(error_type: str):
    """Turn an exception raised by a fallible node into a state["error"] value.

    This is what lets `handle_error` be one node reached from several places
    (PRD §6.2) instead of a copy-pasted try/except in every routing function.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(self, state: POGraphState) -> POGraphState:
            try:
                return fn(self, state)
            except Exception as exc:  # noqa: BLE001 - convert to routed state, not a raised exception
                return {
                    "error": {
                        "error_type": error_type,
                        "error_code": type(exc).__name__,
                        "error_message": str(exc),
                        "node_name": fn.__name__,
                    }
                }

        return wrapped

    return decorator


class PoValidationNodes:
    """LangGraph node functions for the PO Validation Agent.

    Mirrors app/agents/cmir/nodes.py: every collaborator is injected, and nodes
    only touch their own domain data (common.purchase_order_line,
    common.material_master, cmir.cmir_record, process.processing_error).
    process.agent_run / process.workflow_thread / process.human_action
    transitions are handled by PoValidationService after each graph.invoke(),
    exactly like CmirRunService._handle_graph_state.
    """

    def __init__(
        self,
        *,
        purchase_order_repository: PurchaseOrderRepository,
        master_data_repository: MasterDataRepository,
        cmir_repository: CmirRecordRepository,
        processing_error_repository: ProcessingErrorRepository,
    ) -> None:
        self._purchase_order_repository = purchase_order_repository
        self._master_data_repository = master_data_repository
        self._cmir_repository = cmir_repository
        self._processing_error_repository = processing_error_repository

    # ---- validation steps ---- #

    @_capture_errors("SYSTEM_ERROR")
    def persist_po_line(self, state: POGraphState) -> POGraphState:
        self._purchase_order_repository.update_line_status(state["po_line_id"], "VALIDATING")
        return {}

    @_capture_errors("LOOKUP_FAILURE")
    def validate_against_cmir(self, state: POGraphState) -> POGraphState:
        # `retailer_code`/`retailer_material_code` are read from graph state (the
        # application-level vocabulary, populated at ingest -- see
        # PoValidationService._run_po_line), not from any purchase_order_line
        # column directly -- the DB stores the same values under
        # common.retailer.retailer_code / purchase_order_line.retailer_material_code
        # instead. `_normalize_po_line` is a back-compat shim for threads whose
        # checkpoint predates the customer_id/customer_material_code ->
        # retailer_code/retailer_material_code rename.
        po_line = _normalize_po_line(state["po_line"])
        match = self._cmir_repository.find_latest_for_customer_material(
            po_line["retailer_code"], po_line["retailer_material_code"]
        )
        if match is None:
            return {"sap_material_number": None, "cmir_match_found": False}
        return {"sap_material_number": match["material_identity"], "cmir_match_found": True}

    @_capture_errors("LOOKUP_FAILURE")
    def check_material_master(self, state: POGraphState) -> POGraphState:
        po_line = state["po_line"]
        sap_material_number = state["sap_material_number"]
        if sap_material_number is None:
            raise LookupError("check_material_master reached with no sap_material_number recorded")
        plant_id = po_line["plant_id"]
        material = self._master_data_repository.find_material_master(sap_material_number, plant_id)
        if material is None:
            raise LookupError(
                f"material_master has no row for material={sap_material_number} plant_id={plant_id}"
            )
        sufficient = material["available_quantity"] >= po_line["order_quantity"]
        return {
            "material": {
                "sap_material_number": material["sap_material_number"],
                "plant_id": material["plant_id"],
                "available_quantity": material["available_quantity"],
                "follow_up_material_id": material["follow_up_material_id"],
            },
            "quantity_sufficient": sufficient,
        }

    # ---- human-in-the-loop steps ---- #

    def human_manual_cmir_entry(self, state: POGraphState) -> POGraphState:
        po_line = _normalize_po_line(state["po_line"])
        answer = interrupt(
            {
                "reason": "manual_cmir_entry",
                "po_line_id": state["po_line_id"],
                "po_number": po_line["po_number"],
                "po_line_number": po_line["po_line_number"],
                "retailer_code": po_line["retailer_code"],
                "retailer_material_code": po_line["retailer_material_code"],
            }
        )
        return {
            "sap_material_number": answer["sap_material_number"],
            "manual_entry_description": answer.get("description", ""),
        }

    def human_qty_mismatch_decision(self, state: POGraphState) -> POGraphState:
        material = state["material"]
        po_line = state["po_line"]
        # material_master.follow_up_material_id is a FK to common.material.id (a
        # plant-agnostic material identity), not a per-plant SAP material number
        # (see app.models.common.material.MaterialMaster) -- resolved here via
        # MasterDataRepository.find_material_master_by_material_id(material_id,
        # plant_id), which didn't exist until this fix. Previously this was
        # surfaced as the raw stringified UUID under
        # "suggested_substitute_material_code" -- not just a display gap:
        # PoValidationService.submit_qty_mismatch_decision's `_require_material`
        # call validates that value as a `sap_material_number`, so accepting the
        # old suggestion as-is would have failed validation. Resolving it here
        # to the real SAP number (plus description/available_quantity for the
        # UI) fixes that and gives a genuinely submittable suggestion.
        follow_up_material_id = material.get("follow_up_material_id")
        suggested_substitute_material_code: str | None = None
        suggested_substitute_description: str | None = None
        suggested_substitute_available_quantity: float | None = None
        # Display-only: common.material.material_code, distinct from
        # suggested_substitute_material_code above (which stays the real,
        # submittable sap_material_number -- unchanged). Never used for
        # resubmission, only so the UI can show the substitute's real
        # business material code alongside its SAP number and quantity.
        suggested_substitute_business_material_code: str | None = None
        if follow_up_material_id:
            substitute_master = self._master_data_repository.find_material_master_by_material_id(
                follow_up_material_id, material["plant_id"]
            )
            if substitute_master is not None:
                suggested_substitute_material_code = substitute_master["sap_material_number"]
                suggested_substitute_description = substitute_master.get("description")
                suggested_substitute_available_quantity = substitute_master.get("available_quantity")
                suggested_substitute_business_material_code = substitute_master.get("material_code")
            # else: the follow-up material has no master record at this same
            # plant -- genuinely no honest suggestion to offer, stays None
            # rather than falling back to the raw UUID.

        answer = interrupt(
            {
                "reason": "qty_mismatch_decision",
                "po_line_id": state["po_line_id"],
                "candidate": {
                    "sap_material_number": material["sap_material_number"],
                    # CandidateInfo (app/schemas/po_validation/threads.py) requires a
                    # human-readable "plant" code, not the plant_id UUID material.plant_id
                    # carries -- po_line["plant"] is the same plant_code string the
                    # ingest payload/get_or_create_plant already established
                    # (service.py::_run_po_line), so no repository lookup is needed here.
                    "plant": po_line["plant"],
                    "available_quantity": material["available_quantity"],
                    "shortfall": po_line["order_quantity"] - material["available_quantity"],
                    "suggested_substitute_material_code": suggested_substitute_material_code,
                    "suggested_substitute_description": suggested_substitute_description,
                    "suggested_substitute_available_quantity": suggested_substitute_available_quantity,
                    "suggested_substitute_business_material_code": suggested_substitute_business_material_code,
                },
            }
        )
        decision = answer["decision"]
        result: POGraphState = {"decision": decision}
        if decision == "use_substitute":
            substitute = answer.get("substitute_material_code") or suggested_substitute_material_code
            result["sap_material_number"] = substitute
        return result

    # ---- outcome steps ---- #

    @_capture_errors("SYSTEM_ERROR")
    def create_cmir_record(self, state: POGraphState) -> POGraphState:
        po_line = _normalize_po_line(state["po_line"])
        sap_material_number = state["sap_material_number"]
        if sap_material_number is None:
            raise ValueError("create_cmir_record reached with no sap_material_number recorded")
        # cmir.cmir_record's own columns (customer_identity/target_customer_material_ref)
        # are CMIR-domain terminology, out of scope for the retailer_code/
        # retailer_material_code rename -- only the PO-side source values change name.
        self._cmir_repository.create_manual_mapping(
            customer_identity=po_line["retailer_code"],
            material_identity=sap_material_number,
            target_customer_material_ref=po_line["retailer_material_code"],
            description=state.get("manual_entry_description", ""),
        )
        return {}

    def mark_ready_for_so_creation(self, state: POGraphState) -> POGraphState:
        self._purchase_order_repository.update_line_status(state["po_line_id"], "READY_FOR_SO_CREATION")
        return {}

    def mark_ready_for_so_creation_partial(self, state: POGraphState) -> POGraphState:
        self._purchase_order_repository.update_line_status(
            state["po_line_id"], "READY_FOR_SO_CREATION_PARTIAL"
        )
        return {}

    def mark_discontinued(self, state: POGraphState) -> POGraphState:
        self._purchase_order_repository.update_line_status(state["po_line_id"], "DISCONTINUED")
        return {}

    def handle_error(self, state: POGraphState) -> POGraphState:
        error = state.get("error") or {}
        # process.processing_error (generalizing the old po_line_errors) has no
        # purchase_order_line_id column -- only job_item_id/agent_run_id (see
        # app.repositories.process.workflow.ProcessingErrorRepository and
        # PoValidationService's own docstring, point 3, for the same documented gap).
        # agent_run_id is the only thread this error row can be found by later.
        self._processing_error_repository.log(
            error.get("error_type", "SYSTEM_ERROR"),
            agent_run_id=state.get("run_id"),
            error_code=error.get("error_code"),
            error_message=error.get("error_message"),
            node_name=error.get("node_name", "unknown"),
            raw_error_detail=error,
        )
        self._purchase_order_repository.update_line_status(state["po_line_id"], "FAILED")
        return {}

    # ---- routing functions ---- #

    def route_after_persist(self, state: POGraphState) -> Literal["error", "continue"]:
        return "error" if state.get("error") else "continue"

    def route_after_cmir_validation(self, state: POGraphState) -> Literal["error", "found", "not_found"]:
        if state.get("error"):
            return "error"
        return "found" if state.get("cmir_match_found") else "not_found"

    def route_after_material_check(
        self, state: POGraphState
    ) -> Literal["error", "sufficient", "insufficient"]:
        if state.get("error"):
            return "error"
        return "sufficient" if state.get("quantity_sufficient") else "insufficient"

    def route_after_create_cmir_record(self, state: POGraphState) -> Literal["error", "continue"]:
        return "error" if state.get("error") else "continue"

    def route_after_qty_mismatch(
        self, state: POGraphState
    ) -> Literal["use_substitute", "proceed_anyway", "mark_stale"]:
        decision = state["decision"]
        if decision is None:
            raise ValueError("route_after_qty_mismatch reached with no decision recorded")
        return decision
