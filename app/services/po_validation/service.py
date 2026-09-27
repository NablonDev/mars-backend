"""Coordinates the PO Validation Agent's API workflows across repositories
and LangGraph.

Was `app/services/po_validation_service.py` (`PoValidationService`).
Mirrors `app.services.cmir.run_service.CmirRunService` -- same
checkpoint-thread-id-vs-reviewer-facing-thread-id bridge (see that module's
docstring, point 1), same lazily-created-`workflow_thread`-on-first-interrupt
pattern, same collapsed `AppError` contract, same `graph.invoke`/`Command(resume=...)`
usage, and (session-lifecycle/persistence fix) the same per-invocation unit-of-work
pattern: every public method opens exactly one fresh Session (via
`self._repos_factory()`/`self._unit_of_work_factory()`, backed by
`app.core.container.Container.po_validation_repos`/`po_validation_unit_of_work`),
committed on success, rolled back on exception, closed either way -- no
repository/graph reference is ever held on `self` across calls.

PO-validation has no dedicated schema of its own (see the approved plan's
§2/§6: it reuses `common` for PO/material master data, `cmir` for CMIR
lookups and the shared job-item-context table, and `process` for the
workflow/agent backbone) -- there is no `PoLine`/`MaterialMasterRecord`
model any more, only `common.purchase_order`/`purchase_order_line` and
`common.material_master`. Real, load-bearing consequences of that reuse,
not pure renames:

1. **`common.purchase_order_line` has no columns for `retailer_code`/`plant`
   as free strings** -- it FKs to `retailer_id`/`plant_id`. `ingest_po_lines`
   now find-or-creates a minimal `Retailer`/`Plant`/`PurchaseOrder` header
   row per incoming line (keyed on the payload's `retailer_code`/`plant`/
   `po_number`) before creating the line itself, so a PO-validation ingest
   payload can land somewhere real. This is new orchestration this phase
   had to design, not something Phase 2's repositories already resolved.
   (`retailer_code`/`retailer_material_code` were `customer_id`/
   `customer_material_code` before the final naming refactor.)
2. **`purchase_order_line.unit_price` is `NOT NULL`**, but no ingest payload
   in this domain ever carries a price (PO-validation is a
   quantity/material check, not a pricing concern). Every ingested line
   gets `unit_price=0.0` as a placeholder -- flagged, not silently
   defaulted: a real integration needs either a real price on the payload
   or that column made nullable, both out of scope for a services-only
   phase.
3. **`processing_error` (generalizing the old `po_line_errors`) has no
   `purchase_order_line_id` column** -- only `job_item_id`/`agent_run_id`
   (see `app.models.process.processing_error`). `get_errors` can only
   discover a line's errors through a `workflow_thread` that was actually
   created for it (via that thread's `agent_run_id`, stashed in its
   `metadata_json`, same bridge as point 1). A line that failed BEFORE ever
   reaching a human interrupt (i.e. at `persist_po_line`/
   `validate_against_cmir`/`check_material_master`, all pre-interrupt
   steps, per `app/agents/po_validation/graph.py`) has no discoverable
   error trail through this method -- a real, flagged capability gap
   versus the old design, not a silent smoothing-over. Fixing it properly
   needs a new repository query capability (Phase 2 scope) or nodes.py
   always creating a thread eagerly (contradicts the model's own "lazy,
   only on first interrupt" design intent) -- neither is this phase's call
   to make unilaterally.
3b. **`purchase_order_line.line_status` had no writer at all** --
   `PurchaseOrderRepository.update_line_status` is a deliberate Phase 3
   addition (flagged in the phase report) closing that gap: the
   pre-restructure service itself (not a graph node) set
   `AWAITING_DECISION` on first interrupt, and this service does the same
   here. The *terminal* statuses (READY_FOR_SO_CREATION/DISCONTINUED/...)
   are still nodes.py's/Phase 4's job once it's rewired against the new
   repositories.
4. **`list_ready_lines` has no repository support.** There is no
   cross-purchase-order "list every purchase_order_line filtered/paginated
   by `line_status`" query in `PurchaseOrderRepository` (Phase 2 scope,
   already reviewed/frozen) -- only `list_lines(purchase_order_id)`, scoped
   to one PO. Raises `ValidationError(code="VIEW_NOT_SUPPORTED")` rather
   than faking an unindexed full scan.
5. **No job-run/job-item queue wiring** (unlike
   `app.services.cmir.run_service.CmirRunService.start_email_ingest`):
   PO-validation ingest stays synchronous/inline, per the PRD's own
   contract ("no queue/internal-process endpoint for this agent") -- there
   is no async dispatch concept here for a job_item to represent, so
   `cmir.cmir_job_item_context`'s `purchase_order_line_id` branch (defined
   in the model from Phase 1) has no writer in this phase either.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from langgraph.types import Command

from app.core.exceptions import AppError, ConflictError, ExternalServiceError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

INTERRUPT_KEY = "__interrupt__"

STAGE_BY_INTERRUPT = {
    "manual_cmir_entry": ("AWAITING_MANUAL_CMIR_ENTRY", "waiting_manual_cmir_entry"),
    "qty_mismatch_decision": ("AWAITING_QTY_MISMATCH_DECISION", "waiting_qty_mismatch_decision"),
}

NODE_BY_INTERRUPT = {
    "manual_cmir_entry": "human_manual_cmir_entry",
    "qty_mismatch_decision": "human_qty_mismatch_decision",
}

# Maps the terminal purchase_order_line.line_status set by the graph's
# outcome nodes to the PRD's UI stage / workflow_thread.status pair.
FINAL_STAGE_BY_LINE_STATUS = {
    "READY_FOR_SO_CREATION": ("READY_FOR_SO_CREATION", "ready_for_so_creation"),
    "READY_FOR_SO_CREATION_PARTIAL": ("READY_FOR_SO_CREATION_PARTIAL", "ready_for_so_creation_partial"),
    "DISCONTINUED": ("COMPLETED_DISCONTINUED", "completed_discontinued"),
    "FAILED": ("FAILED", "failed"),
}

_AGENT_CODE = "po_validation"
_AGENT_NAME = "PO Validation"
_PROMPT_VERSION = "v1"
_SYSTEM_PROMPT = (
    "Validate one purchase order line against known CMIR customer-material mappings "
    "and material_master availability, escalating to a human reviewer on an unknown "
    "mapping or an insufficient-quantity mismatch."
)

UnitOfWorkFactory = Callable[[], AbstractContextManager[SimpleNamespace]]


class PoValidationService:
    def __init__(
        self,
        *,
        repos_factory: UnitOfWorkFactory,
        unit_of_work_factory: UnitOfWorkFactory,
    ) -> None:
        self._repos_factory = repos_factory
        self._unit_of_work_factory = unit_of_work_factory

    def _ensure_registered(self, agent_registry: Any) -> UUID:
        return agent_registry.ensure_registered(
            agent_code=_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=_SYSTEM_PROMPT,
            agent_name=_AGENT_NAME,
            # PO-validation has no schema of its own -- it lives in `cmir`
            # (see this module's docstring, §2/§6) -- and process.agent.domain
            # is restricted by ck_agent_domain to ('cmir', 'penalties');
            # "po_validation" is not a valid domain value.
            domain="cmir",
        )

    def ingest_po_lines(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist each PO line and run it through the graph synchronously.

        There is no queue/internal-process endpoint for this agent per the
        PRD's API contract (see this module's docstring, point 5), so each
        line is validated inline as part of the ingest request. All lines in
        one call share the same unit of work (one Session, one commit).
        """
        batch_id = self._new_batch_id()
        with self._unit_of_work_factory() as uow:
            summaries = [self._ingest_one_line(uow, batch_id, payload) for payload in lines]
            return {"batch_id": batch_id, "total_lines": len(summaries), "lines": summaries}

    def _ingest_one_line(self, uow: SimpleNamespace, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Concurrency fix: these four calls replace what used to be
        # `get_*` -> `if None: add_*`/`create_*` sequences -- each new
        # `get_or_create_*` repository method is atomic against a
        # concurrent identical ingest (Postgres `ON CONFLICT ... DO
        # NOTHING` + re-SELECT; see app/repositories/common/master_data.py
        # and purchase_order.py). `add_retailer`/`add_plant`/
        # `create_purchase_order`/`add_line` themselves are unchanged.
        retailer = uow.master_data.get_or_create_retailer(
            payload["retailer_code"], payload["retailer_code"], None
        )

        plant = uow.master_data.get_or_create_plant(payload["plant"])

        purchase_order = uow.purchase_orders.get_or_create_purchase_order(
            payload["po_number"],
            retailer_id=retailer["id"],
            order_date=datetime.now(UTC).date(),
            requested_delivery_date=_parse_date(payload.get("requested_delivery_date")),
        )

        line = uow.purchase_orders.get_or_create_line(
            purchase_order["id"],
            payload["po_line_number"],
            payload["order_quantity"],
            retailer_material_code=payload["retailer_material_code"],
            plant_id=plant["id"],
            uom=payload.get("uom"),
            requested_delivery_date=_parse_date(payload.get("requested_delivery_date")),
            # See this module's docstring, point 2 -- no price in this domain's payload.
            unit_price=0.0,
            line_status="NEW",
            raw_payload=payload,
        )

        result = self._run_po_line(uow, batch_id, line["id"], payload, plant["id"])
        # purchase_order_line.line_status (NEW/AWAITING_DECISION/READY_FOR_SO_CREATION/...)
        # is the vocabulary this response reports in, not workflow_thread.status (which
        # `result` carries and is only meaningful once a thread exists) -- read it back
        # directly so a touchless line reports correctly even with no thread.
        current = uow.purchase_orders.get_line(line["id"])
        # `result` is either the touchless-path literal dict (carries
        # "thread_id" directly) or `get_stage()`'s own shape (carries the
        # thread's id under "id", not "thread_id") -- normalize here rather
        # than at every caller.
        thread_id = result.get("thread_id", result.get("id"))
        return {
            "po_line_id": str(line["id"]),
            "batch_id": batch_id,
            "po_number": payload["po_number"],
            "po_line_number": payload["po_line_number"],
            "status": current["line_status"] if current else result["status"],
            "thread_id": str(thread_id) if thread_id is not None else None,
            "updated_at": result.get("updated_at"),
        }

    def _run_po_line(
        self,
        uow: SimpleNamespace,
        batch_id: str,
        po_line_id: UUID,
        payload: dict[str, Any],
        plant_id: UUID,
    ) -> dict[str, Any]:
        checkpoint_thread_id = self._new_checkpoint_thread_id()
        agent_id = self._ensure_registered(uow.agent_registry)
        run_id = uow.agent_runs.start(agent_id=agent_id, run_type="PO_VALIDATION")
        # `retailer_code`/`retailer_material_code` (final naming refactor -- was
        # customer_id/customer_material_code) are populated directly from `payload`
        # here, independently of the DB write above, which persists the same values
        # under common.retailer.retailer_code / purchase_order_line.retailer_material_code.
        # Both sides are populated from the same source payload, so they agree in
        # value, but neither is derived from the other. New checkpoints only ever
        # contain these new key names; `nodes.py::_normalize_po_line` is the back-compat
        # shim for threads whose checkpoint predates this rename.
        initial_state = {
            "batch_id": batch_id,
            "run_id": run_id,
            "po_line_id": po_line_id,
            "thread_id": checkpoint_thread_id,
            "po_line": {
                "po_number": payload["po_number"],
                "po_line_number": payload["po_line_number"],
                "retailer_code": payload["retailer_code"],
                "retailer_material_code": payload["retailer_material_code"],
                "plant": payload["plant"],
                "plant_id": plant_id,
                "order_quantity": payload["order_quantity"],
                "uom": payload.get("uom"),
            },
        }
        try:
            state = uow.graph.invoke(initial_state, config=self._thread_config(checkpoint_thread_id))
        except Exception as exc:
            logger.exception("PO line %s failed during ingest", po_line_id)
            uow.agent_runs.update_status(run_id, "failed", error=str(exc), completed=True)
            return {"batch_id": batch_id, "thread_id": None, "status": "FAILED", "updated_at": None}
        return self._handle_graph_state(uow, run_id, batch_id, po_line_id, checkpoint_thread_id, state)

    def list_ready_lines(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        raise ValidationError(
            code="VIEW_NOT_SUPPORTED",
            message=(
                "Cross-purchase-order line listing has no repository support in the "
                "common/process/cmir/penalties restructure -- list a single PO's lines "
                "via GET /purchase-orders/{purchase_order_id}/lines instead."
            ),
            details={"status": status},
        )

    def get_errors(self, po_line_id: UUID) -> dict[str, Any]:
        with self._repos_factory() as repos:
            po_line = repos.purchase_orders.get_line(po_line_id)
            if po_line is None:
                raise ValidationError(
                    code="VALIDATION_ERROR",
                    message="Unknown po_line_id.",
                    details={"po_line_id": str(po_line_id)},
                )

            thread = repos.workflow_threads.get_latest_by_purchase_order_line(po_line_id)
            if thread is None:
                # See this module's docstring, point 3: a line that failed before
                # ever reaching a human interrupt has no discoverable error trail
                # through this method today.
                return {"items": []}

            agent_run_id = (thread.get("metadata_json") or {}).get("agent_run_id")
            if not agent_run_id:
                return {"items": []}
            return {"items": repos.processing_errors.list_for_agent_run(UUID(agent_run_id))}

    def get_stage(self, thread_id: UUID) -> dict[str, Any]:
        with self._repos_factory() as repos:
            return self._get_stage(repos.workflow_threads, thread_id)

    def get_snapshot(self, thread_id: UUID) -> dict[str, Any]:
        with self._repos_factory() as repos:
            stage = self._get_stage(repos.workflow_threads, thread_id)
            po_line_id = stage.get("purchase_order_line_id")
            po_line = repos.purchase_orders.get_line(po_line_id) if po_line_id else None
            if po_line is None:
                raise self._thread_not_found(thread_id)

            pending = repos.human_actions.get_open_for_thread(thread_id)
            candidate: dict[str, Any] | None = None
            editable_fields: list[str] = []
            if pending is not None:
                payload = pending["request_payload"]
                if pending["interrupt_type"] == "qty_mismatch_decision":
                    candidate = payload.get("candidate")
                    editable_fields = ["substitute_material_code"]
                elif pending["interrupt_type"] == "manual_cmir_entry":
                    editable_fields = ["sap_material_number", "description"]

            return {
                "agent_run_id": (stage.get("metadata_json") or {}).get("agent_run_id"),
                "thread_id": str(thread_id),
                "po_line_id": str(po_line_id),
                "po_number": po_line.get("raw_payload", {}).get("po_number")
                if po_line.get("raw_payload")
                else None,
                "po_line_number": po_line["line_number"],
                "retailer_material_code": po_line["retailer_material_code"],
                "order_quantity": po_line["ordered_quantity"],
                "stage": stage["stage"],
                "candidate": candidate,
                "editable_fields": editable_fields,
                "history": repos.human_actions.list_for_thread(thread_id),
                "updated_at": stage["updated_at"],
            }

    def submit_manual_cmir_entry(
        self,
        thread_id: UUID,
        *,
        actor: str,
        sap_material_number: str,
        description: str = "",
        expected_updated_at: str,
    ) -> dict[str, Any]:
        """Resume a thread waiting for a manually entered CMIR mapping."""
        persist_args: dict[str, Any] | None = None
        try:
            with self._unit_of_work_factory() as uow:
                stage = self._ensure_current(uow.workflow_threads, thread_id, expected_updated_at)
                if stage["status"] != "waiting_manual_cmir_entry":
                    raise self._thread_not_waiting(thread_id, "waiting_manual_cmir_entry", stage["status"])

                po_line_id = stage["purchase_order_line_id"]
                po_line = uow.purchase_orders.get_line(po_line_id) if po_line_id else None
                if po_line is None:
                    raise self._thread_not_found(thread_id)
                self._require_material(uow.master_data, sap_material_number, po_line["plant_id"])

                pending = self._require_open_pending(uow.human_actions, thread_id, "manual_cmir_entry")
                answer = {"sap_material_number": sap_material_number, "description": description}
                checkpoint_thread_id = self._checkpoint_thread_id(stage)
                try:
                    state = uow.graph.invoke(
                        Command(resume=answer), config=self._thread_config(checkpoint_thread_id)
                    )
                except Exception as exc:
                    raise self._resume_failed(thread_id, pending["id"], exc) from exc

                persist_args = {
                    "run_id": self._agent_run_id(stage),
                    "batch_id": stage["metadata_json"].get("batch_id"),
                    "po_line_id": po_line_id,
                    "checkpoint_thread_id": checkpoint_thread_id,
                    "state": state,
                    "resume_context": {
                        "workflow_thread_id": thread_id,
                        "pending_action_id": pending["id"],
                        "answer": answer,
                        "actor": actor,
                        "action_type": "manual_entry",
                    },
                }
                # A.2: let any non-AppError exception propagate through this
                # ENTIRE `with` block (correct rollback of the original
                # Session) rather than catching it here -- retried outside.
                return self._handle_graph_state(uow, **persist_args)
        except AppError:
            # B fix (narrow scope): a concurrent-loser ConflictError from
            # apply_human_action must reach the caller as-is (clean 409),
            # not be masked as a generic WORKFLOW_RESUME_FAILED below.
            raise
        except Exception as first_exc:  # noqa: BLE001 -- A.2 retry must catch any DB-layer failure type
            assert persist_args is not None  # graph.invoke() must have succeeded to reach here
            return self._retry_persist_or_raise_corrupt(
                lambda fresh_uow: self._handle_graph_state(fresh_uow, **persist_args),
                thread_id=thread_id,
                pending_action_id=persist_args["resume_context"]["pending_action_id"],
                checkpoint_thread_id=persist_args["checkpoint_thread_id"],
                operation_name="submit_manual_cmir_entry",
                first_exc=first_exc,
            )

    def submit_qty_mismatch_decision(
        self,
        thread_id: UUID,
        *,
        actor: str,
        decision: str,
        substitute_material_code: str | None = None,
        expected_updated_at: str,
    ) -> dict[str, Any]:
        """Resume a thread waiting for a quantity-mismatch resolution."""
        if decision not in {"use_substitute", "proceed_anyway", "mark_stale"}:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="decision must be 'use_substitute', 'proceed_anyway', or 'mark_stale'.",
                details={"decision": decision},
            )

        persist_args: dict[str, Any] | None = None
        try:
            with self._unit_of_work_factory() as uow:
                stage = self._ensure_current(uow.workflow_threads, thread_id, expected_updated_at)
                if stage["status"] != "waiting_qty_mismatch_decision":
                    raise self._thread_not_waiting(
                        thread_id, "waiting_qty_mismatch_decision", stage["status"]
                    )

                pending = self._require_open_pending(uow.human_actions, thread_id, "qty_mismatch_decision")
                answer: dict[str, Any] = {"decision": decision}
                if decision == "use_substitute":
                    po_line_id = stage["purchase_order_line_id"]
                    po_line = uow.purchase_orders.get_line(po_line_id) if po_line_id else None
                    if po_line is None:
                        raise self._thread_not_found(thread_id)
                    candidate = pending["request_payload"].get("candidate", {})
                    chosen = substitute_material_code or candidate.get("suggested_substitute_material_code")
                    if not chosen:
                        raise ValidationError(
                            code="VALIDATION_ERROR",
                            message=(
                                "use_substitute requires substitute_material_code, since no "
                                "suggestion was offered."
                            ),
                            details={"thread_id": str(thread_id)},
                        )
                    self._require_material(uow.master_data, chosen, po_line["plant_id"])
                    answer["substitute_material_code"] = chosen

                checkpoint_thread_id = self._checkpoint_thread_id(stage)
                try:
                    state = uow.graph.invoke(
                        Command(resume=answer), config=self._thread_config(checkpoint_thread_id)
                    )
                except Exception as exc:
                    raise self._resume_failed(thread_id, pending["id"], exc) from exc

                persist_args = {
                    "run_id": self._agent_run_id(stage),
                    "batch_id": stage["metadata_json"].get("batch_id"),
                    "po_line_id": stage["purchase_order_line_id"],
                    "checkpoint_thread_id": checkpoint_thread_id,
                    "state": state,
                    "resume_context": {
                        "workflow_thread_id": thread_id,
                        "pending_action_id": pending["id"],
                        "answer": answer,
                        "actor": actor,
                        "action_type": "decision",
                        "decision": decision,
                    },
                }
                # A.2: let any non-AppError exception propagate through this
                # ENTIRE `with` block (correct rollback of the original
                # Session) rather than catching it here -- retried outside.
                return self._handle_graph_state(uow, **persist_args)
        except AppError:
            # B fix (narrow scope): a concurrent-loser ConflictError from
            # apply_human_action must reach the caller as-is (clean 409),
            # not be masked as a generic WORKFLOW_RESUME_FAILED below.
            raise
        except Exception as first_exc:  # noqa: BLE001 -- A.2 retry must catch any DB-layer failure type
            assert persist_args is not None  # graph.invoke() must have succeeded to reach here
            return self._retry_persist_or_raise_corrupt(
                lambda fresh_uow: self._handle_graph_state(fresh_uow, **persist_args),
                thread_id=thread_id,
                pending_action_id=persist_args["resume_context"]["pending_action_id"],
                checkpoint_thread_id=persist_args["checkpoint_thread_id"],
                operation_name="submit_qty_mismatch_decision",
                first_exc=first_exc,
            )

    def _handle_graph_state(
        self,
        uow: SimpleNamespace,
        run_id: UUID,
        batch_id: str | None,
        po_line_id: UUID,
        checkpoint_thread_id: str,
        state: dict[str, Any],
        *,
        resume_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist purchase_order_line/thread status after a graph invoke or resume."""
        if state.get(INTERRUPT_KEY):
            payload = state[INTERRUPT_KEY][0].value
            reason = payload["reason"]
            stage, status = STAGE_BY_INTERRUPT[reason]

            if resume_context is None:
                # First interrupt for this line: this is the only point at which a
                # reviewer-facing thread is created, per the PRD's "no thread_id for
                # the automatic path" rule.
                created = uow.workflow_threads.create(
                    stage=stage,
                    purchase_order_line_id=po_line_id,
                    status=status,
                    current_node=NODE_BY_INTERRUPT[reason],
                    metadata={
                        "checkpoint_thread_id": checkpoint_thread_id,
                        "agent_run_id": str(run_id),
                        "batch_id": batch_id,
                        "latest_snapshot": {"po_line_id": str(po_line_id), "payload": payload},
                    },
                )
                workflow_thread_id = created["id"]
                uow.human_actions.create_open(
                    reason,
                    payload,
                    workflow_thread_id=workflow_thread_id,
                    agent_run_id=run_id,
                    state_snapshot=self._snapshot_state(state),
                )
                uow.agent_runs.update_status(run_id, status)
            else:
                workflow_thread_id = resume_context["workflow_thread_id"]
                thread = uow.workflow_threads.get_by_id(workflow_thread_id)
                metadata = {
                    **((thread or {}).get("metadata_json") or {}),
                    "latest_snapshot": {"po_line_id": str(po_line_id), "payload": payload},
                }
                uow.human_actions.apply_human_action(
                    pending_action_id=resume_context["pending_action_id"],
                    workflow_thread_id=workflow_thread_id,
                    response_payload=resume_context["answer"],
                    actor=resume_context["actor"],
                    decision=resume_context.get("decision"),
                    action_type=resume_context["action_type"],
                    next_status=status,
                    next_stage=stage,
                    next_current_node=NODE_BY_INTERRUPT[reason],
                    next_metadata=metadata,
                    next_pending_interrupt_type=reason,
                    next_pending_request_payload=payload,
                    next_pending_state_snapshot=self._snapshot_state(state),
                )
                uow.agent_runs.update_status(run_id, status)
            # AWAITING_DECISION on interrupt is this service's own
            # responsibility (mirrors the pre-restructure service, which
            # called this same shape directly -- not a graph node's job).
            uow.purchase_orders.update_line_status(po_line_id, "AWAITING_DECISION")
            return self._get_stage(uow.workflow_threads, workflow_thread_id)

        # No interrupt: the graph's outcome node already set the terminal
        # purchase_order_line.line_status (once Phase 4 rewires nodes.py
        # against the new repositories -- see this module's docstring).
        final_line = uow.purchase_orders.get_line(po_line_id)
        final_status = final_line["line_status"] if final_line else "FAILED"
        stage, status = FINAL_STAGE_BY_LINE_STATUS.get(final_status, ("FAILED", "failed"))

        if resume_context is None:
            # Touchless path: no thread was ever created for this line.
            uow.agent_runs.update_status(run_id, status, completed=True)
            return {
                "batch_id": batch_id,
                "agent_run_id": run_id,
                "thread_id": None,
                "po_line_id": str(po_line_id),
                "stage": stage,
                "status": status,
                "current_node": None,
                "pending_action_id": None,
                "updated_at": None,
            }

        workflow_thread_id = resume_context["workflow_thread_id"]
        thread = uow.workflow_threads.get_by_id(workflow_thread_id)
        metadata = {
            **((thread or {}).get("metadata_json") or {}),
            "latest_snapshot": {"po_line_id": str(po_line_id)},
        }
        uow.human_actions.apply_human_action(
            pending_action_id=resume_context["pending_action_id"],
            workflow_thread_id=workflow_thread_id,
            response_payload=resume_context["answer"],
            actor=resume_context["actor"],
            decision=resume_context.get("decision"),
            action_type=resume_context["action_type"],
            next_status=status,
            next_stage=stage,
            next_metadata=metadata,
            completed=True,
        )
        uow.agent_runs.update_status(run_id, status, completed=True)
        return self._get_stage(uow.workflow_threads, workflow_thread_id)

    def _get_stage(self, workflow_threads: Any, thread_id: UUID) -> dict[str, Any]:
        stage = workflow_threads.get_stage(thread_id)
        if stage is None:
            raise self._thread_not_found(thread_id)
        return stage

    def _require_material(self, master_data: Any, sap_material_number: str, plant_id: UUID) -> None:
        if master_data.find_material_master(sap_material_number, plant_id) is None:
            raise ValidationError(
                code="MATERIAL_NOT_FOUND",
                message=f"No material_master row for material={sap_material_number} plant_id={plant_id}.",
                details={"sap_material_number": sap_material_number, "plant_id": str(plant_id)},
            )

    def _require_open_pending(self, human_actions: Any, thread_id: UUID, interrupt_type: str) -> dict[str, Any]:
        pending = human_actions.get_open_for_thread(thread_id)
        if pending is None or pending["interrupt_type"] != interrupt_type:
            actual = None if pending is None else pending["interrupt_type"]
            raise self._thread_not_waiting(thread_id, interrupt_type, actual)
        return pending

    def _ensure_current(self, workflow_threads: Any, thread_id: UUID, expected_updated_at: str) -> dict[str, Any]:
        stage = self._get_stage(workflow_threads, thread_id)
        if stage["updated_at"].isoformat() != expected_updated_at:
            raise ConflictError(
                code="THREAD_STALE",
                message="Thread was updated by another reviewer. Refresh snapshot and retry.",
                details={"thread_id": str(thread_id), "latest_updated_at": stage["updated_at"].isoformat()},
            )
        return stage

    @staticmethod
    def _checkpoint_thread_id(stage: dict[str, Any]) -> str:
        checkpoint_thread_id = (stage["metadata_json"] or {}).get("checkpoint_thread_id")
        if checkpoint_thread_id is None:
            raise ExternalServiceError(
                code="WORKFLOW_STATE_CORRUPT",
                message="Thread has no checkpoint_thread_id recorded; cannot resume its LangGraph run.",
                details={"thread_id": str(stage["id"])},
            )
        return checkpoint_thread_id

    @staticmethod
    def _agent_run_id(stage: dict[str, Any]) -> UUID:
        agent_run_id = (stage["metadata_json"] or {}).get("agent_run_id")
        return UUID(agent_run_id) if agent_run_id else stage["id"]

    @staticmethod
    def _thread_config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _snapshot_state(state: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in state.items() if key != INTERRUPT_KEY}

    @staticmethod
    def _new_batch_id() -> str:
        return f"batch_po_{uuid4().hex[:12]}"

    @staticmethod
    def _new_checkpoint_thread_id() -> str:
        return f"thread_po_{uuid4().hex[:12]}"

    @staticmethod
    def _thread_not_found(thread_id: UUID) -> NotFoundError:
        return NotFoundError(
            code="THREAD_NOT_FOUND",
            message="Unknown thread_id.",
            details={"thread_id": str(thread_id)},
        )

    @staticmethod
    def _resume_failed(thread_id: UUID, pending_action_id: UUID, exc: Exception) -> ExternalServiceError:
        logger.exception(
            "Failed to resume PO validation thread %s from pending action %s",
            thread_id,
            pending_action_id,
        )
        return ExternalServiceError(
            code="WORKFLOW_RESUME_FAILED",
            message="Unexpected failure while resuming workflow.",
            details={
                "thread_id": str(thread_id),
                "pending_action_id": str(pending_action_id),
                "error": str(exc),
            },
        )

    def _retry_persist_or_raise_corrupt(
        self,
        operation: Callable[[SimpleNamespace], Any],
        *,
        thread_id: UUID,
        pending_action_id: UUID,
        checkpoint_thread_id: str,
        operation_name: str,
        first_exc: Exception,
    ) -> Any:
        """A.2 (PO): mirrors CmirRunService._retry_persist_or_raise_corrupt --
        `operation` is a DB-persistence-only step whose graph/checkpoint work
        has ALREADY succeeded and committed (LangGraph's autocommit
        connection); `operation` must never touch `uow.graph`/re-invoke the
        graph.

        The caller's own `with self._unit_of_work_factory() as uow:` block
        has already let `first_exc` propagate all the way through it before
        reaching here, so the original Session has already been rolled back
        -- this only opens a completely fresh Session via
        `self._repos_factory()` (no graph needed) for the single retry
        attempt. If the retry also fails, raises
        ExternalServiceError(code="WORKFLOW_STATE_CORRUPT") with logged
        context for manual reconciliation -- never silently, never as the
        generic WORKFLOW_RESUME_FAILED. Never claims true atomicity -- see
        docs/implementation-progress.md's A.2 section for the documented
        residual risk.
        """
        logger.error(
            "A.2: DB persistence failed after a successful graph invoke/checkpoint "
            "write; retrying once with a fresh Session (never re-invoking the graph). "
            "thread_id=%s pending_action_id=%s checkpoint_thread_id=%s operation=%s error=%s",
            thread_id,
            pending_action_id,
            checkpoint_thread_id,
            operation_name,
            first_exc,
        )
        try:
            with self._repos_factory() as fresh_uow:
                return operation(fresh_uow)
        except AppError:
            raise
        except Exception as retry_exc:
            logger.critical(
                "A.2: DB persistence retry ALSO failed after a successful graph invoke/"
                "checkpoint write -- workflow state has diverged from the checkpoint and "
                "requires manual reconciliation. thread_id=%s pending_action_id=%s "
                "checkpoint_thread_id=%s operation=%s first_error=%s retry_error=%s",
                thread_id,
                pending_action_id,
                checkpoint_thread_id,
                operation_name,
                first_exc,
                retry_exc,
            )
            raise ExternalServiceError(
                code="WORKFLOW_STATE_CORRUPT",
                message=(
                    "The reviewer's action was recorded in the workflow checkpoint, but the "
                    "application database could not be updated to match, even after a retry. "
                    "This thread's state may be inconsistent and requires manual reconciliation."
                ),
                details={
                    "thread_id": str(thread_id),
                    "pending_action_id": str(pending_action_id),
                    "checkpoint_thread_id": checkpoint_thread_id,
                    "operation": operation_name,
                },
            ) from retry_exc

    @staticmethod
    def _thread_not_waiting(thread_id: UUID, expected: str, actual: str | None) -> ConflictError:
        return ConflictError(
            code="THREAD_NOT_WAITING",
            message="Resume API called while thread is not paused for that action.",
            details={"thread_id": str(thread_id), "expected": expected, "actual": actual},
        )


def _parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)
