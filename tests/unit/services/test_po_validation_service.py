"""Tests for PoValidationService against the real SQLite-backed
`common`/`process` repositories (see tests/conftest.py's `repos` fixture),
with a hand-written fake LangGraph graph standing in for the actual
compiled graph -- the graph itself (`app/agents/po_validation/{graph,
nodes}.py`) is Phase 4's concern and stays out of scope here; these tests
exercise only the service's own persistence/bookkeeping around whatever
the graph returns.

Was against hand-rolled `FakePoLines`/`FakeMaterialMaster`/
`FakeWorkflowThreads`/`FakePendingActions`/`FakeHITLState` doubles matching
the pre-restructure `PoLine`/`po_validation`-specific repository shapes;
rewritten against the real `PurchaseOrderRepository`/`MasterDataRepository`
/`WorkflowThreadRepository`/`HumanActionRepository`/`AgentRunRepository`/
`AgentRegistryRepository`/`ProcessingErrorRepository` instead -- there is
no PO-validation-specific model/repository any more (see
`app.services.po_validation.service`'s module docstring): a "PO line" is
now a `common.purchase_order_line` row like any other domain's.

A repository gap this exposed and could not silently route around: no
`PurchaseOrderRepository` method sets `purchase_order_line.line_status`
directly (nodes.py -- Phase 4 -- is the intended future writer, and it's
currently broken/unwired, same as `app.services.cmir.service`'s sibling
gap). Where a test needs to simulate "the graph's outcome node already set
this terminal status" (the old fakes' `forced_final_status` override), this
suite writes the ORM row directly via `db_session`, flagged inline at each
call site.
"""

from __future__ import annotations

import copy
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import ConflictError, ExternalServiceError, ValidationError
from app.models import PurchaseOrderLine
from app.services.po_validation.service import INTERRUPT_KEY, PoValidationService


class FakeGraph:
    """A configurable stand-in for the compiled LangGraph graph --
    `invoke_plan` is a list of state-dicts (or Exceptions to raise),
    consumed one per call to `.invoke()`; running past the end repeats the
    last entry."""

    def __init__(self, invoke_plan: list[Any] | None = None) -> None:
        self.invoke_plan = invoke_plan or [{}]
        self.invocations: list[tuple[dict, dict]] = []
        self._call_count = 0

    def invoke(self, value, config=None):
        self.invocations.append((copy.deepcopy(value), copy.deepcopy(config)))
        index = min(self._call_count, len(self.invoke_plan) - 1)
        self._call_count += 1
        result = self.invoke_plan[index]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def _build_service(repos, graph: FakeGraph) -> PoValidationService:
    return PoValidationService(
        graph=graph,
        purchase_orders=repos.purchase_orders,
        master_data=repos.master_data,
        agent_registry=repos.agent_registry,
        agent_runs=repos.agent_runs,
        workflow_threads=repos.workflow_threads,
        human_actions=repos.human_actions,
        processing_errors=repos.processing_errors,
        job_queue=repos.job_queue,
        job_run_context=repos.cmir_job_run_context,
        job_item_context=repos.cmir_job_item_context,
    )


def _line_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "po_number": "PO-2026-0001",
        "po_line_number": "10",
        "customer_id": "CUST-1",
        "customer_material_code": "ACME-MAT-1",
        "plant": "1000",
        "order_quantity": 100,
    }
    payload.update(overrides)
    return payload


def _interrupt_result(reason: str, **extra: Any) -> dict:
    payload = {"reason": reason, "po_number": "PO-2026-0001", "po_line_number": "10", "customer_id": "CUST-1"}
    payload.update(extra)
    return {INTERRUPT_KEY: [SimpleNamespace(value=payload)]}


def _qty_mismatch_interrupt(suggested: str | None = "MAT-SUB-1") -> dict:
    return _interrupt_result(
        "qty_mismatch_decision",
        candidate={
            "sap_material_number": "MAT-1",
            "plant": "1000",
            "available_quantity": 40,
            "shortfall": 60,
            "suggested_substitute_material_code": suggested,
        },
    )


def _start_po_line_thread_awaiting(
    repos, reason: str, *, resume_results: list[Any] | None = None
) -> tuple[PoValidationService, FakeGraph, UUID]:
    """Ingests one PO line through the service to create a real
    workflow_thread row parked at the given interrupt reason, returning
    (service, graph, thread_id). `resume_results` are queued as the
    graph's subsequent invoke() results, for the resume call the test
    itself will make."""
    interrupt = _qty_mismatch_interrupt() if reason == "qty_mismatch_decision" else _interrupt_result(reason)
    graph = FakeGraph([interrupt, *(resume_results or [])])
    service = _build_service(repos, graph)
    result = service.ingest_po_lines([_line_payload()])
    thread_id = UUID(result["lines"][0]["thread_id"])
    return service, graph, thread_id


def test_touchless_ready_line_creates_no_thread(repos) -> None:
    """A PO line that's already on file with a terminal status (simulating
    a previous run's outcome) and re-ingested against a graph that doesn't
    interrupt reports that status straight through, with no thread ever
    created."""
    retailer = repos.master_data.add_retailer("CUST-1", "Customer 1", None, "SUM")
    plant = repos.master_data.add_plant("1000", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-2026-0001", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        retailer_material_code="ACME-MAT-1",
        plant_id=plant["id"],
        line_status="READY_FOR_SO_CREATION",
    )
    service = _build_service(repos, FakeGraph([{}]))

    result = service.ingest_po_lines([_line_payload()])

    line = result["lines"][0]
    assert line["thread_id"] is None
    assert line["status"] == "READY_FOR_SO_CREATION"


def test_qty_mismatch_interrupt_creates_thread_and_pending_action(repos) -> None:
    graph = FakeGraph([_qty_mismatch_interrupt()])
    service = _build_service(repos, graph)

    result = service.ingest_po_lines([_line_payload()])

    line = result["lines"][0]
    assert line["thread_id"] is not None
    assert line["status"] == "AWAITING_DECISION"
    thread_id = UUID(line["thread_id"])
    thread = repos.workflow_threads.get_by_id(thread_id)
    assert thread is not None
    assert thread["subject_id"] is not None
    pending = repos.human_actions.get_open_for_thread(thread_id)
    assert pending is not None
    assert pending["interrupt_type"] == "qty_mismatch_decision"


def test_submit_qty_mismatch_decision_rejects_wrong_stage(repos) -> None:
    service, _graph, thread_id = _start_po_line_thread_awaiting(repos, "manual_cmir_entry")
    stage = service.get_stage(thread_id)

    with pytest.raises(ConflictError) as raised:
        service.submit_qty_mismatch_decision(
            thread_id,
            actor="csr@company.com",
            decision="proceed_anyway",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "THREAD_NOT_WAITING"


def test_submit_qty_mismatch_decision_stale_timestamp(repos) -> None:
    service, _graph, thread_id = _start_po_line_thread_awaiting(repos, "qty_mismatch_decision")

    with pytest.raises(ConflictError) as raised:
        service.submit_qty_mismatch_decision(
            thread_id,
            actor="csr@company.com",
            decision="proceed_anyway",
            expected_updated_at="2020-01-01T00:00:00+00:00",
        )

    assert raised.value.code == "THREAD_STALE"


def test_submit_qty_mismatch_use_substitute_rejects_unknown_material(repos) -> None:
    service, graph, thread_id = _start_po_line_thread_awaiting(repos, "qty_mismatch_decision")
    stage = service.get_stage(thread_id)

    with pytest.raises(ValidationError) as raised:
        service.submit_qty_mismatch_decision(
            thread_id,
            actor="csr@company.com",
            decision="use_substitute",
            substitute_material_code="MAT-UNKNOWN",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "MATERIAL_NOT_FOUND"
    # The graph must never be invoked with an unvalidated reviewer-submitted
    # material -- only the initial ingest invocation happened.
    assert len(graph.invocations) == 1


def test_submit_manual_cmir_entry_validates_material_before_resume(repos) -> None:
    service, graph, thread_id = _start_po_line_thread_awaiting(repos, "manual_cmir_entry")
    stage = service.get_stage(thread_id)

    with pytest.raises(ValidationError) as raised:
        service.submit_manual_cmir_entry(
            thread_id,
            actor="csr@company.com",
            sap_material_number="MAT-UNKNOWN",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "MATERIAL_NOT_FOUND"
    assert len(graph.invocations) == 1


def test_manual_cmir_entry_resume_failure_keeps_pending_action_open(repos) -> None:
    service, _graph, thread_id = _start_po_line_thread_awaiting(
        repos, "manual_cmir_entry", resume_results=[RuntimeError("checkpoint missing")]
    )
    plant = repos.master_data.get_plant_by_code("1000")
    material = repos.master_data.add_material("MAT-100", None)
    repos.master_data.add_material_master(
        material_id=material["id"],
        sap_material_number="MAT-100",
        plant_id=plant["id"],
        available_quantity=500,
    )
    stage = service.get_stage(thread_id)
    pending_before = repos.human_actions.get_open_for_thread(thread_id)

    with pytest.raises(ExternalServiceError) as raised:
        service.submit_manual_cmir_entry(
            thread_id,
            actor="csr@company.com",
            sap_material_number="MAT-100",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "WORKFLOW_RESUME_FAILED"
    # The invariant: a failed resume must not close the open pending action.
    pending_after = repos.human_actions.get_open_for_thread(thread_id)
    assert pending_after is not None
    assert pending_after["id"] == pending_before["id"]


def test_submit_qty_mismatch_decision_persists_decision_and_reaches_final_stage(repos, db_session) -> None:
    """The human's actual decision (proceed_anyway/mark_stale/use_substitute)
    must reach human_action.decision via apply_human_action's decision=
    kwarg, and the response reports the line's terminal status once the
    graph resumes without a further interrupt.

    The line's terminal status is written directly here (see this module's
    docstring) since no repository method exists yet to set it -- that's
    nodes.py's (Phase 4's) job once it's rewired against the new schema."""
    service, _graph, thread_id = _start_po_line_thread_awaiting(
        repos, "qty_mismatch_decision", resume_results=[{}]
    )
    stage = service.get_stage(thread_id)
    line_row = db_session.get(PurchaseOrderLine, stage["subject_id"])
    line_row.line_status = "READY_FOR_SO_CREATION_PARTIAL"
    db_session.flush()

    response = service.submit_qty_mismatch_decision(
        thread_id,
        actor="csr@company.com",
        decision="proceed_anyway",
        expected_updated_at=stage["updated_at"].isoformat(),
    )

    assert response["stage"] == "READY_FOR_SO_CREATION_PARTIAL"
    history = repos.human_actions.list_for_thread(thread_id)
    completed = next(h for h in history if h["status"] == "completed")
    assert completed["decision"] == "proceed_anyway"


def test_get_errors_rejects_unknown_po_line(repos) -> None:
    service = _build_service(repos, FakeGraph())

    with pytest.raises(ValidationError) as raised:
        service.get_errors(uuid4())

    assert raised.value.code == "VALIDATION_ERROR"


def test_get_errors_finds_pre_interrupt_error_with_no_thread(repos) -> None:
    """A line that fails in a pre-interrupt node (handle_error) has no
    workflow_thread at all -- get_errors must still find it, via
    processing_error.purchase_order_line_id directly."""
    retailer = repos.master_data.add_retailer("CUST-1", "Customer 1", None, "SUM")
    plant = repos.master_data.add_plant("1000", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-2026-0002", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        retailer_material_code="ACME-MAT-1",
        plant_id=plant["id"],
        line_status="FAILED",
    )
    repos.processing_errors.log(
        "LOOKUP_FAILURE",
        purchase_order_line_id=line["id"],
        node_name="check_material_master",
        error_code="LookupError",
        error_message="no material_master row",
    )
    service = _build_service(repos, FakeGraph())

    result = service.get_errors(line["id"])

    assert len(result["items"]) == 1
    assert result["items"][0]["error_type"] == "LOOKUP_FAILURE"
    assert result["items"][0]["purchase_order_line_id"] == line["id"]


def test_ingest_po_lines_creates_job_run_and_settles_job_item_succeeded(repos) -> None:
    """Gap 1: ingest_po_lines must create a real process.job_run/job_item
    trail (mirroring CmirService.start_email_ingest) and settle each
    line's job item inline, in the same request, once its graph invocation
    returns -- without changing the existing synchronous response shape."""
    service = _build_service(repos, FakeGraph([{}]))

    result = service.ingest_po_lines([_line_payload()])

    job_run_id = UUID(result["batch_id"])
    run_summary = repos.job_queue.get_run_summary(job_run_id)
    assert run_summary["counts"]["SUCCEEDED"] == 1

    items = repos.job_queue.list_run_items(job_run_id)
    assert len(items) == 1
    assert items[0]["item_type"] == "PO_VALIDATION"
    assert items[0]["status"] == "SUCCEEDED"

    po_line_id = UUID(result["lines"][0]["po_line_id"])
    context = repos.cmir_job_item_context.get(items[0]["id"])
    assert context is not None
    assert context["purchase_order_line_id"] == po_line_id


def test_ingest_po_lines_marks_job_item_dead_on_graph_exception(repos) -> None:
    """A graph invocation that raises internally is a job-execution
    failure, not just a business-level FAILED line -- the job item settles
    DEAD, not SUCCEEDED (see PoValidationService._settle_job_item)."""
    service = _build_service(repos, FakeGraph([RuntimeError("checkpointer unavailable")]))

    result = service.ingest_po_lines([_line_payload()])

    assert result["lines"][0]["thread_id"] is None
    job_run_id = UUID(result["batch_id"])
    items = repos.job_queue.list_run_items(job_run_id)
    assert items[0]["status"] == "DEAD"
    assert items[0]["last_error_code"] == "PO_VALIDATION_GRAPH_ERROR"


def test_list_ready_lines_defaults_to_ready_statuses(repos) -> None:
    """Gap 3: with no explicit status, only the 'ready' terminal statuses
    are returned, not every line regardless of status."""
    retailer = repos.master_data.add_retailer("CUST-2", "Customer 2", None, "SUM")
    plant = repos.master_data.add_plant("2000", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-READY-1", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    ready_line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
        line_status="READY_FOR_SO_CREATION",
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="20",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
        line_status="AWAITING_DECISION",
    )
    service = _build_service(repos, FakeGraph())

    result = service.list_ready_lines()

    ids = {row["id"] for row in result["items"]}
    assert ready_line["id"] in ids
    assert all(
        row["line_status"] in ("READY_FOR_SO_CREATION", "READY_FOR_SO_CREATION_PARTIAL")
        for row in result["items"]
    )


def test_list_ready_lines_filters_by_explicit_status(repos) -> None:
    retailer = repos.master_data.add_retailer("CUST-3", "Customer 3", None, "SUM")
    plant = repos.master_data.add_plant("3000", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-READY-2", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    awaiting_line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
        line_status="AWAITING_DECISION",
    )
    service = _build_service(repos, FakeGraph())

    result = service.list_ready_lines(status="AWAITING_DECISION")

    assert [row["id"] for row in result["items"]] == [awaiting_line["id"]]


def test_list_ready_lines_scoped_to_purchase_order_ignores_ready_default(repos) -> None:
    """`purchase_order_id` given, `status=None`: every line for that PO
    regardless of status -- the old nested `GET /purchase-orders/{id}/lines`
    route's behavior, now folded into `list_ready_lines` itself (see that
    method's docstring)."""
    retailer = repos.master_data.add_retailer("CUST-4", "Customer 4", None, "SUM")
    plant = repos.master_data.add_plant("4000", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-READY-3", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    awaiting_line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
        line_status="AWAITING_DECISION",
    )
    # A different PO's ready line must not leak into the scoped result.
    other_po = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-READY-4", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    repos.purchase_orders.add_line(
        purchase_order_id=other_po["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
        line_status="READY_FOR_SO_CREATION",
    )
    service = _build_service(repos, FakeGraph())

    result = service.list_ready_lines(purchase_order_id=purchase_order["id"])

    assert [row["id"] for row in result["items"]] == [awaiting_line["id"]]
