"""Coordinates PO Validation workflows across repositories and LangGraph.

Entry points:
    ingest_po_lines (POST /api/v1/po-validation/purchase-order-lines)
    list_ready_lines (GET /api/v1/purchase-order-lines)
    get_errors (GET /api/v1/processing-errors)
    get_snapshot (GET /api/v1/workflow-threads/{thread_id})
    submit_qty_mismatch_decision (POST /api/v1/workflow-threads/{thread_id}/decisions)
    submit_manual_cmir_entry (POST /api/v1/workflow-threads/{thread_id}/decisions)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from langgraph.types import Command

from app.core.config import get_settings
from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.enums import JobRunType, JobTaskType, WorkflowThreadSubjectType
from app.repositories.cmir.job_context import CmirJobItemContextRepository, CmirJobRunContextRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.repositories.process.workflow import (
    HumanActionRepository,
    ProcessingErrorRepository,
    WorkflowThreadRepository,
)
from app.utils.clock import utc_today
from app.utils.ids import new_id

logger = logging.getLogger(__name__)

# `process.job_run.job_type` for this domain's ingest batch. `job_type` is a free
# string with no CHECK; the matching `item_type` reuses the
# `JobTaskType.PO_VALIDATION` value already allowed by `ck_job_item_item_type`.
_JOB_TYPE_PO_VALIDATION_BATCH = "PO_VALIDATION_BATCH"

INTERRUPT_KEY = "__interrupt__"

STAGE_BY_INTERRUPT = {
    "manual_cmir_entry": ("AWAITING_MANUAL_CMIR_ENTRY", "waiting_manual_cmir_entry"),
    "qty_mismatch_decision": ("AWAITING_QTY_MISMATCH_DECISION", "waiting_qty_mismatch_decision"),
}

NODE_BY_INTERRUPT = {
    "manual_cmir_entry": "human_manual_cmir_entry",
    "qty_mismatch_decision": "human_qty_mismatch_decision",
}

# "Ready" for downstream sales-order creation: the two terminal line_status
# values FINAL_STAGE_BY_LINE_STATUS maps to READY_FOR_SO_CREATION(_PARTIAL), and
# `list_ready_lines`' default filter when no explicit `status` is requested.
_READY_LINE_STATUSES = ("READY_FOR_SO_CREATION", "READY_FOR_SO_CREATION_PARTIAL")

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


class PoValidationService:
    """Coordinates purchase-order line ingestion and validation across repositories and LangGraph."""

    def __init__(
        self,
        *,
        graph: Any,
        purchase_orders: PurchaseOrderRepository,
        master_data: MasterDataRepository,
        agent_registry: AgentRegistryRepository,
        agent_runs: AgentRunRepository,
        workflow_threads: WorkflowThreadRepository,
        human_actions: HumanActionRepository,
        processing_errors: ProcessingErrorRepository,
        job_queue: JobQueueRepository,
        job_run_context: CmirJobRunContextRepository,
        job_item_context: CmirJobItemContextRepository,
    ) -> None:
        self._graph = graph
        self._purchase_orders = purchase_orders
        self._master_data = master_data
        self._agent_registry = agent_registry
        self._agent_runs = agent_runs
        self._workflow_threads = workflow_threads
        self._human_actions = human_actions
        self._processing_errors = processing_errors
        self._job_queue = job_queue
        self._job_run_context = job_run_context
        self._job_item_context = job_item_context

    def ingest_po_lines(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist each PO line and run it through the graph synchronously.

        Every line's run is tracked through the shared job queue: one
        `process.job_run` per call, one `process.job_item` per line, claimed and
        settled in the same request. The response reports each line's resulting
        status.
        """
        settings = get_settings()
        run = self._job_queue.create_run(
            job_type=_JOB_TYPE_PO_VALIDATION_BATCH,
            trigger_type=JobRunType.ON_DEMAND,
            requested_item_count=len(lines),
        )
        self._job_run_context.create(job_run_id=run["id"], source_type="api")

        summaries = [
            self._ingest_one_line(str(run["id"]), payload, run["id"], settings.job_queue.max_attempts)
            for payload in lines
        ]
        return {"batch_id": str(run["id"]), "total_lines": len(summaries), "lines": summaries}

    def replay_line(self, purchase_order_line_id: UUID) -> None:
        """Re-run one PO line's graph invocation from its originally-ingested payload.

        The recovery path for a `PO_VALIDATION` job item left PENDING/RUNNING, not
        part of the normal synchronous ingest flow, which settles its own job item
        inline. Raises `ValidationError` if the line is unknown or has no
        `raw_payload`.
        """
        po_line = self._purchase_orders.get_line(purchase_order_line_id)
        if po_line is None or not po_line.get("raw_payload"):
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="Unknown purchase_order_line_id, or it has no raw_payload to replay from.",
                details={"purchase_order_line_id": str(purchase_order_line_id)},
            )
        self._run_po_line(
            self._new_batch_id(), purchase_order_line_id, po_line["raw_payload"], po_line["plant_id"]
        )

    def list_ready_lines(
        self,
        *,
        purchase_order_id: UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Flat `purchase_order_line` listing, the one route for this resource.

        With neither argument, defaults to the "ready" set (`_READY_LINE_STATUSES`)
        across every PO. With `purchase_order_id` alone, returns all of that PO's
        lines regardless of status. An explicit `status` filters on exactly that
        `line_status`, optionally scoped to one PO.
        """
        line_status: str | tuple[str, ...] | None
        if status is not None:
            line_status = status
        elif purchase_order_id is not None:
            line_status = None
        else:
            line_status = _READY_LINE_STATUSES
        items, next_cursor = self._purchase_orders.list_lines_by_status(
            line_status, purchase_order_id=purchase_order_id, limit=limit, cursor=cursor
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_errors(self, po_line_id: UUID) -> dict[str, Any]:
        """Return every processing error logged for one PO line, whether or not it ever reached a human interrupt."""
        po_line = self._purchase_orders.get_line(po_line_id)
        if po_line is None:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="Unknown po_line_id.",
                details={"po_line_id": str(po_line_id)},
            )

        # Direct lookup by purchase_order_line_id finds every error `handle_error`
        # logged for this line, whether or not it reached a human interrupt.
        return {"items": self._processing_errors.list_for_purchase_order_line(po_line_id)}

    def get_stage(self, thread_id: UUID) -> dict[str, Any]:
        """Return current UI stage for one thread, raising `NotFoundError` if `thread_id` is unknown."""
        stage = self._workflow_threads.get_stage(thread_id)
        if stage is None:
            raise self._thread_not_found(thread_id)
        return stage

    def get_snapshot(self, thread_id: UUID) -> dict[str, Any]:
        """Return the reviewer-facing snapshot for one thread: the PO line, its pending decision, and its history.

        `editable_fields` and `candidate` depend on which interrupt type the
        thread is currently paused on (or are empty/None once the thread is no
        longer waiting on a human decision).
        """
        stage = self.get_stage(thread_id)
        po_line_id = stage.get("subject_id")
        po_line = self._purchase_orders.get_line(po_line_id) if po_line_id else None
        if po_line is None:
            raise self._thread_not_found(thread_id)

        pending = self._human_actions.get_open_for_thread(thread_id)
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
            "customer_material_code": po_line["retailer_material_code"],
            "order_quantity": po_line["ordered_quantity"],
            "stage": stage["stage"],
            "candidate": candidate,
            "editable_fields": editable_fields,
            "history": self._human_actions.list_for_thread(thread_id),
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
        stage = self._ensure_current(thread_id, expected_updated_at)
        if stage["status"] != "waiting_manual_cmir_entry":
            raise self._thread_not_waiting(thread_id, "waiting_manual_cmir_entry", stage["status"])

        po_line_id = stage["subject_id"]
        po_line = self._purchase_orders.get_line(po_line_id) if po_line_id else None
        if po_line is None:
            raise self._thread_not_found(thread_id)
        self._require_material(sap_material_number, po_line["plant_id"])

        pending = self._require_open_pending(thread_id, "manual_cmir_entry")
        answer = {"sap_material_number": sap_material_number, "description": description}
        checkpoint_thread_id = self._checkpoint_thread_id(stage)
        try:
            state = self._graph.invoke(
                Command(resume=answer), config=self._thread_config(checkpoint_thread_id)
            )
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc
        try:
            return self._handle_graph_state(
                self._agent_run_id(stage),
                stage["metadata_json"].get("batch_id"),
                po_line_id,
                checkpoint_thread_id,
                state,
                resume_context={
                    "workflow_thread_id": thread_id,
                    "pending_action_id": pending["id"],
                    "answer": answer,
                    "actor": actor,
                    "action_type": "manual_entry",
                },
            )
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc

    def submit_qty_mismatch_decision(
        self,
        thread_id: UUID,
        *,
        actor: str,
        decision: str,
        substitute_material_code: str | None = None,
        expected_updated_at: str,
    ) -> dict[str, Any]:
        """Resume a thread waiting for a quantity-mismatch resolution.

        `use_substitute` requires a material to substitute in: either
        `substitute_material_code` explicitly, or the candidate's own
        suggested substitute if the graph offered one; the chosen material
        is validated against `material_master` for the line's plant before
        the decision is sent back into the graph. `proceed_anyway` and
        `mark_stale` need no extra input.
        """
        if decision not in {"use_substitute", "proceed_anyway", "mark_stale"}:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="decision must be 'use_substitute', 'proceed_anyway', or 'mark_stale'.",
                details={"decision": decision},
            )

        stage = self._ensure_current(thread_id, expected_updated_at)
        if stage["status"] != "waiting_qty_mismatch_decision":
            raise self._thread_not_waiting(thread_id, "waiting_qty_mismatch_decision", stage["status"])

        pending = self._require_open_pending(thread_id, "qty_mismatch_decision")
        answer: dict[str, Any] = {"decision": decision}
        if decision == "use_substitute":
            po_line_id = stage["subject_id"]
            po_line = self._purchase_orders.get_line(po_line_id) if po_line_id else None
            if po_line is None:
                raise self._thread_not_found(thread_id)
            candidate = pending["request_payload"].get("candidate", {})
            chosen = substitute_material_code or candidate.get("suggested_substitute_material_code")
            if not chosen:
                raise ValidationError(
                    code="VALIDATION_ERROR",
                    message="use_substitute requires substitute_material_code, since no suggestion was offered.",
                    details={"thread_id": str(thread_id)},
                )
            self._require_material(chosen, po_line["plant_id"])
            answer["substitute_material_code"] = chosen

        checkpoint_thread_id = self._checkpoint_thread_id(stage)
        try:
            state = self._graph.invoke(
                Command(resume=answer), config=self._thread_config(checkpoint_thread_id)
            )
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc
        try:
            return self._handle_graph_state(
                self._agent_run_id(stage),
                stage["metadata_json"].get("batch_id"),
                stage["subject_id"],
                checkpoint_thread_id,
                state,
                resume_context={
                    "workflow_thread_id": thread_id,
                    "pending_action_id": pending["id"],
                    "answer": answer,
                    "actor": actor,
                    "action_type": "decision",
                    "decision": decision,
                },
            )
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc

    def _ingest_one_line(
        self, batch_id: str, payload: dict[str, Any], job_run_id: UUID, max_attempts: int
    ) -> dict[str, Any]:
        """Resolve or create the retailer/plant/purchase-order/line rows for one PO line payload, then run it through the graph.

        Looks up each master-data and PO row by its natural key first and only
        creates a new one on a miss, so re-ingesting a previously-seen PO or
        line reuses the existing rows instead of duplicating them. Enqueues
        and claims a `process.job_item` for the line before invoking the
        graph, and settles it afterward, so every line's run is tracked
        through the shared job queue even though this whole method runs
        synchronously within one ingest request.
        """
        retailer = self._master_data.get_retailer_by_code(payload["customer_id"])
        if retailer is None:
            retailer = self._master_data.add_retailer(payload["customer_id"], payload["customer_id"], None)

        plant = self._master_data.get_plant_by_code(payload["plant"])
        if plant is None:
            plant = self._master_data.add_plant(payload["plant"])

        purchase_order = self._purchase_orders.get_by_number(payload["po_number"])
        if purchase_order is None:
            purchase_order = self._purchase_orders.create_purchase_order(
                purchase_order_number=payload["po_number"],
                retailer_id=retailer["id"],
                order_date=utc_today(),
                requested_delivery_date=_parse_date(payload.get("requested_delivery_date")),
            )

        existing_lines = self._purchase_orders.list_lines(purchase_order["id"])
        line = next(
            (line for line in existing_lines if line["line_number"] == payload["po_line_number"]), None
        )
        if line is None:
            line = self._purchase_orders.add_line(
                purchase_order_id=purchase_order["id"],
                line_number=payload["po_line_number"],
                ordered_quantity=payload["order_quantity"],
                retailer_material_code=payload["customer_material_code"],
                plant_id=plant["id"],
                uom=payload.get("uom"),
                requested_delivery_date=_parse_date(payload.get("requested_delivery_date")),
                # This domain's payload carries no price.
                unit_price=0.0,
                line_status="NEW",
                raw_payload=payload,
            )

        claimed_job_item_id = self._enqueue_and_claim_job_item(job_run_id, line["id"], max_attempts)
        result = self._run_po_line(batch_id, line["id"], payload, plant["id"])
        if claimed_job_item_id is not None:
            self._settle_job_item(claimed_job_item_id, result)
        # This response reports in purchase_order_line.line_status, not
        # workflow_thread.status, which `result` carries and which is meaningful only
        # once a thread exists. Reading it back directly keeps a touchless line
        # correct when it has no thread.
        current = self._purchase_orders.get_line(line["id"])
        # `result` is either the touchless-path literal dict, which carries
        # "thread_id" directly, or `get_stage()`'s shape, which carries the thread id
        # under "id". Normalize here rather than at every caller.
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

    def _enqueue_and_claim_job_item(
        self, job_run_id: UUID, po_line_id: UUID, max_attempts: int
    ) -> UUID | None:
        """Enqueue, context-attach, and claim one `PO_VALIDATION` job item for a line.

        Deduped on the line id, so re-ingesting a line whose prior run is still in
        flight does not double-queue. Returns `None` when no fresh, this-caller-owned
        PENDING item resulted, either from the enqueue race `JobQueueRepository.enqueue`
        documents or from a dedupe hit another in-flight run already owns; the line
        still runs through the graph either way.
        """
        item = self._job_queue.enqueue(
            job_run_id,
            item_type=JobTaskType.PO_VALIDATION,
            dedupe_key=str(po_line_id),
            max_attempts=max_attempts,
        )
        if item is None:
            return None
        if self._job_item_context.get(item["id"]) is None:
            self._job_item_context.create(job_item_id=item["id"], purchase_order_line_id=po_line_id)

        worker_id = self._inline_worker_id(item["id"])
        claimed = self._job_queue.claim_batch(worker_id, 1, job_item_ids=[item["id"]])
        return item["id"] if claimed else None

    def _run_po_line(
        self, batch_id: str, po_line_id: UUID, payload: dict[str, Any], plant_id: UUID
    ) -> dict[str, Any]:
        """Register the agent, start an agent run, and invoke the PO-validation graph for one line.

        A graph invocation that raises is caught here and reported as a
        `FAILED`/`None`-thread result rather than propagated, so one bad line
        never aborts the rest of a batch ingest.
        """
        checkpoint_thread_id = self._new_checkpoint_thread_id()
        agent_id = self._ensure_registered()
        run_id = self._agent_runs.start(agent_id=agent_id, run_type="PO_VALIDATION")
        initial_state = {
            "batch_id": batch_id,
            "run_id": run_id,
            "po_line_id": po_line_id,
            "thread_id": checkpoint_thread_id,
            "po_line": {
                "po_number": payload["po_number"],
                "po_line_number": payload["po_line_number"],
                "customer_id": payload["customer_id"],
                "customer_material_code": payload["customer_material_code"],
                "plant": payload["plant"],
                "plant_id": plant_id,
                "order_quantity": payload["order_quantity"],
                "uom": payload.get("uom"),
            },
        }
        try:
            state = self._graph.invoke(initial_state, config=self._thread_config(checkpoint_thread_id))
        except Exception as exc:
            logger.exception("PO line %s failed during ingest", po_line_id)
            self._agent_runs.update_status(run_id, "failed", error=str(exc), completed=True)
            return {"batch_id": batch_id, "thread_id": None, "status": "FAILED", "updated_at": None}
        return self._handle_graph_state(run_id, batch_id, po_line_id, checkpoint_thread_id, state)

    def _settle_job_item(self, job_item_id: UUID, result: dict[str, Any]) -> None:
        """Mark the job item claimed for this line's inline run terminal.

        `result["status"] == "FAILED"` (upper case) is `_run_po_line`'s literal for
        "the graph invocation raised", distinct from the lower-case `"failed"` that
        `FINAL_STAGE_BY_LINE_STATUS` reports for a business-level FAILED line. Only
        the former counts as a job-execution failure.
        """
        worker_id = self._inline_worker_id(job_item_id)
        if result.get("status") == "FAILED":
            self._job_queue.mark_dead(
                job_item_id,
                worker_id,
                error="PO line validation graph invocation raised.",
                error_code="PO_VALIDATION_GRAPH_ERROR",
            )
        else:
            self._job_queue.mark_succeeded(job_item_id, worker_id)

    @staticmethod
    def _new_checkpoint_thread_id() -> str:
        """Generate a fresh, unique LangGraph checkpoint thread id for a new run."""
        return new_id("thread_po")

    def _ensure_registered(self) -> UUID:
        """Ensure the `po_validation` agent row exists for this prompt version and return its id."""
        return self._agent_registry.ensure_registered(
            agent_code=_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=_SYSTEM_PROMPT,
            agent_name=_AGENT_NAME,
            # PO-validation has no schema of its own; it lives in `cmir`, and
            # ck_agent_domain restricts process.agent.domain to ('cmir',
            # 'penalties'), so "po_validation" is not a valid domain value.
            domain="cmir",
        )

    @staticmethod
    def _thread_config(thread_id: str) -> dict[str, Any]:
        """Build the LangGraph `config` dict that pins a graph call to one checkpoint thread."""
        return {"configurable": {"thread_id": thread_id}}

    def _handle_graph_state(
        self,
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
                created = self._workflow_threads.create(
                    stage=stage,
                    subject_type=WorkflowThreadSubjectType.PURCHASE_ORDER_LINE,
                    subject_id=po_line_id,
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
                self._human_actions.create_open(
                    reason,
                    payload,
                    workflow_thread_id=workflow_thread_id,
                    agent_run_id=run_id,
                    state_snapshot=self._snapshot_state(state),
                )
                self._agent_runs.update_status(run_id, status)
            else:
                workflow_thread_id = resume_context["workflow_thread_id"]
                thread = self._workflow_threads.get_by_id(workflow_thread_id)
                metadata = {
                    **((thread or {}).get("metadata_json") or {}),
                    "latest_snapshot": {"po_line_id": str(po_line_id), "payload": payload},
                }
                self._human_actions.apply_human_action(
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
                self._agent_runs.update_status(run_id, status)
            # Setting AWAITING_DECISION on interrupt is this service's
            # responsibility, not a graph node's.
            self._purchase_orders.update_line_status(po_line_id, "AWAITING_DECISION")
            return self.get_stage(workflow_thread_id)

        # No interrupt: the graph's outcome node already set the terminal
        # purchase_order_line.line_status.
        final_line = self._purchase_orders.get_line(po_line_id)
        final_status = final_line["line_status"] if final_line else "FAILED"
        stage, status = FINAL_STAGE_BY_LINE_STATUS.get(final_status, ("FAILED", "failed"))

        if resume_context is None:
            # Touchless path: no thread was ever created for this line.
            self._agent_runs.update_status(run_id, status, completed=True)
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
        thread = self._workflow_threads.get_by_id(workflow_thread_id)
        metadata = {
            **((thread or {}).get("metadata_json") or {}),
            "latest_snapshot": {"po_line_id": str(po_line_id)},
        }
        self._human_actions.apply_human_action(
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
        self._agent_runs.update_status(run_id, status, completed=True)
        return self.get_stage(workflow_thread_id)

    @staticmethod
    def _inline_worker_id(job_item_id: UUID) -> str:
        """Derived from `job_item_id` so `_settle_job_item` need not be handed one."""
        return f"po-validation-inline-{job_item_id}"

    @staticmethod
    def _new_batch_id() -> str:
        """Generate a fresh, unique ingest-batch id for `replay_line`, since replay has no ingest request of its own."""
        return new_id("batch_po")

    @staticmethod
    def _thread_not_found(thread_id: UUID) -> NotFoundError:
        """Build the `NotFoundError` raised for an unknown `thread_id`."""
        return NotFoundError(
            code="THREAD_NOT_FOUND",
            message="Unknown thread_id.",
            details={"thread_id": str(thread_id)},
        )

    def _ensure_current(self, thread_id: UUID, expected_updated_at: str) -> dict[str, Any]:
        """Return the thread's current stage, or raise `ConflictError` if it has moved since `expected_updated_at`."""
        stage = self.get_stage(thread_id)
        if stage["updated_at"].isoformat() != expected_updated_at:
            raise ConflictError(
                code="THREAD_STALE",
                message="Thread was updated by another reviewer. Refresh snapshot and retry.",
                details={"thread_id": str(thread_id), "latest_updated_at": stage["updated_at"].isoformat()},
            )
        return stage

    @staticmethod
    def _thread_not_waiting(thread_id: UUID, expected: str, actual: str | None) -> ConflictError:
        """Build the `ConflictError` raised when a resume API is called against a thread not paused on that interrupt."""
        return ConflictError(
            code="THREAD_NOT_WAITING",
            message="Resume API called while thread is not paused for that action.",
            details={"thread_id": str(thread_id), "expected": expected, "actual": actual},
        )

    def _require_material(self, sap_material_number: str, plant_id: UUID) -> None:
        """Raise `ValidationError` if `sap_material_number` has no `material_master` row for `plant_id`."""
        if self._master_data.find_material_master(sap_material_number, plant_id) is None:
            raise ValidationError(
                code="MATERIAL_NOT_FOUND",
                message=f"No material_master row for material={sap_material_number} plant_id={plant_id}.",
                details={"sap_material_number": sap_material_number, "plant_id": str(plant_id)},
            )

    def _require_open_pending(self, thread_id: UUID, interrupt_type: str) -> dict[str, Any]:
        """Return the thread's open `human_action` row, or raise if it isn't waiting on `interrupt_type`."""
        pending = self._human_actions.get_open_for_thread(thread_id)
        if pending is None or pending["interrupt_type"] != interrupt_type:
            actual = None if pending is None else pending["interrupt_type"]
            raise self._thread_not_waiting(thread_id, interrupt_type, actual)
        return pending

    @staticmethod
    def _checkpoint_thread_id(stage: dict[str, Any]) -> str:
        """Read the LangGraph checkpoint thread id off a stage's metadata, or raise if it's missing."""
        checkpoint_thread_id = (stage["metadata_json"] or {}).get("checkpoint_thread_id")
        if checkpoint_thread_id is None:
            raise ExternalServiceError(
                code="WORKFLOW_STATE_CORRUPT",
                message="Thread has no checkpoint_thread_id recorded; cannot resume its LangGraph run.",
                details={"thread_id": str(stage["id"])},
            )
        return checkpoint_thread_id

    @staticmethod
    def _resume_failed(thread_id: UUID, pending_action_id: UUID, exc: Exception) -> ExternalServiceError:
        """Log and build the `ExternalServiceError` raised when resuming a thread's graph run fails unexpectedly."""
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

    @staticmethod
    def _agent_run_id(stage: dict[str, Any]) -> UUID:
        """Read the originating agent run id off a stage's metadata, falling back to the thread's own id."""
        agent_run_id = (stage["metadata_json"] or {}).get("agent_run_id")
        return UUID(agent_run_id) if agent_run_id else stage["id"]

    @staticmethod
    def _snapshot_state(state: dict[str, Any]) -> dict[str, Any]:
        """Strip the LangGraph interrupt marker out of graph state before persisting it as a snapshot."""
        return {key: value for key, value in state.items() if key != INTERRUPT_KEY}


def _parse_date(value: str | date | None) -> date | None:
    """Parse an ISO 8601 date string, passing through `None` or an already-parsed `date` unchanged."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)
