"""Tests for the `process` schema's shared workflow repositories
(`WorkflowThreadRepository`, `HumanActionRepository`,
`ProcessingErrorRepository`). Was split across
tests/unit/repositories/test_cmir_repositories.py (workflow thread/HITL
coverage, against `PostgresWorkflowThreadRepository`/
`PostgresHITLActionRepository`/`PostgresPendingHumanActionRepository`/
`PostgresHITLStateRepository`) and
tests/unit/repositories/test_po_validation_repositories.py
(`PostgresPoLineErrorRepository`) -- relocated onto the merged
`process.workflow_thread`/`workflow_thread_subject`/`human_action`/
`processing_error` tables (see app/repositories/process/workflow.py's
module docstring for the real shape changes this forces, not just a
rename).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.exceptions import ConflictError


def _seed_purchase_order_line(repos) -> str:
    retailer = repos.master_data.add_retailer("RET-WF", "Retailer", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-WF-1",
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"], line_number="10", ordered_quantity=100, unit_price=25.00
    )
    return line["id"]


def _seed_email_event(repos) -> str:
    return repos.emails.save(sender="customer@example.com", subject="Subj", raw_content="body")


def test_create_requires_exactly_one_subject_fk(repos):
    with pytest.raises(ValueError):
        repos.workflow_threads.create(stage="STARTED")

    email_event_id = _seed_email_event(repos)
    purchase_order_line_id = _seed_purchase_order_line(repos)
    with pytest.raises(ValueError):
        repos.workflow_threads.create(
            stage="STARTED", email_event_id=email_event_id, purchase_order_line_id=purchase_order_line_id
        )


def test_create_creates_thread_and_subject_for_email_event(repos):
    email_event_id = _seed_email_event(repos)

    thread = repos.workflow_threads.create(
        stage="AWAITING_APPROVAL", email_event_id=email_event_id, metadata={"cmir": {"brand": "Brand A"}}
    )

    assert thread["stage"] == "AWAITING_APPROVAL"
    assert thread["email_event_id"] == email_event_id
    assert thread["purchase_order_line_id"] is None
    assert thread["metadata_json"]["cmir"]["brand"] == "Brand A"


def test_get_by_id_returns_thread_with_subject(repos):
    purchase_order_line_id = _seed_purchase_order_line(repos)
    created = repos.workflow_threads.create(stage="STARTED", purchase_order_line_id=purchase_order_line_id)

    fetched = repos.workflow_threads.get_by_id(created["id"])

    assert fetched["purchase_order_line_id"] == purchase_order_line_id
    assert fetched["status"] == "running"


def test_get_latest_by_email_event_returns_the_most_recently_updated(repos):
    email_event_id = _seed_email_event(repos)
    first = repos.workflow_threads.create(stage="STARTED", email_event_id=email_event_id)
    repos.workflow_threads.update_status(first["id"], status="running", stage="EXTRACTING")
    second = repos.workflow_threads.create(stage="STARTED", email_event_id=email_event_id)

    latest = repos.workflow_threads.get_latest_by_email_event(email_event_id)
    assert latest["id"] == second["id"]


def test_update_status_completes_a_thread(repos):
    email_event_id = _seed_email_event(repos)
    thread = repos.workflow_threads.create(stage="STARTED", email_event_id=email_event_id)

    repos.workflow_threads.update_status(
        thread["id"], status="completed_approved", stage="DONE", completed=True
    )

    fetched = repos.workflow_threads.get_by_id(thread["id"])
    assert fetched["status"] == "completed_approved"
    assert fetched["current_node"] is None
    assert fetched["completed_at"] is not None


def test_human_action_create_open_and_complete(repos):
    email_event_id = _seed_email_event(repos)
    thread = repos.workflow_threads.create(stage="AWAITING_APPROVAL", email_event_id=email_event_id)

    action_id = repos.human_actions.create_open(
        interrupt_type="approval_required",
        request_payload={"reason": "approval_required"},
        workflow_thread_id=thread["id"],
    )

    open_action = repos.human_actions.get_open_for_thread(thread["id"])
    assert open_action["id"] == action_id

    completed = repos.human_actions.complete(
        action_id, response_payload={"decision": "approve"}, actor="reviewer@company.com", decision="approve"
    )
    assert completed["status"] == "completed"
    assert repos.human_actions.get_open_for_thread(thread["id"]) is None


def test_human_action_apply_human_action_opens_next_pending_and_transitions_thread(repos):
    email_event_id = _seed_email_event(repos)
    thread = repos.workflow_threads.create(stage="AWAITING_MISSING_FIELDS", email_event_id=email_event_id)
    pending_id = repos.human_actions.create_open(
        interrupt_type="missing_mandatory_fields",
        request_payload={"reason": "missing_mandatory_fields"},
        workflow_thread_id=thread["id"],
    )

    new_pending_id = repos.human_actions.apply_human_action(
        pending_action_id=pending_id,
        workflow_thread_id=thread["id"],
        response_payload={"existing_cmir_ref": "CMIR-1"},
        actor="reviewer@company.com",
        action_type="field_update",
        next_status="waiting_approval",
        next_stage="AWAITING_APPROVAL",
        next_current_node="review_extracted_cmir",
        next_metadata={"cmir": {"existing_cmir_ref": "CMIR-1"}},
        next_pending_interrupt_type="approval_required",
        next_pending_request_payload={"reason": "approval_required"},
    )

    assert new_pending_id is not None
    history = repos.human_actions.list_for_thread(thread["id"])
    assert len(history) == 2
    assert history[0]["status"] == "completed"
    assert history[1]["status"] == "open"

    fetched_thread = repos.workflow_threads.get_by_id(thread["id"])
    assert fetched_thread["status"] == "waiting_approval"
    assert fetched_thread["stage"] == "AWAITING_APPROVAL"


def test_human_action_apply_human_action_raises_for_already_closed_action(repos):
    """B fix (narrow scope, concurrent-decision error-contract): a second
    caller racing to complete an already-resolved pending action must get a
    clean, expected ConflictError/THREAD_STALE -- the same conflict shape
    reviewers already handle for any other "thread moved since you last saw
    it" case -- not a bare ValueError (which every caller's generic
    `except Exception` wrapping used to turn into an opaque 5xx
    WORKFLOW_RESUME_FAILED instead of a 409)."""
    email_event_id = _seed_email_event(repos)
    thread = repos.workflow_threads.create(stage="AWAITING_APPROVAL", email_event_id=email_event_id)
    pending_id = repos.human_actions.create_open(
        interrupt_type="approval_required",
        request_payload={"reason": "approval_required"},
        workflow_thread_id=thread["id"],
    )
    repos.human_actions.complete(pending_id, response_payload={}, actor="reviewer@company.com")

    with pytest.raises(ConflictError) as raised:
        repos.human_actions.apply_human_action(
            pending_action_id=pending_id,
            workflow_thread_id=thread["id"],
            response_payload={},
            actor="reviewer@company.com",
            next_status="waiting_approval",
            next_stage="AWAITING_APPROVAL",
        )

    assert raised.value.code == "THREAD_STALE"
    assert raised.value.status_code == 409


def test_processing_error_log_and_list_for_job_item(repos, db_session):
    from app.models import JobItem, JobRun

    run = JobRun(job_type="PO_VALIDATION_BATCH", trigger_type="ON_DEMAND")
    db_session.add(run)
    db_session.flush()
    item = JobItem(job_run_id=run.id, item_type="PO_VALIDATION", status="RUNNING")
    db_session.add(item)
    db_session.flush()

    repos.processing_errors.log(
        error_type="LOOKUP_FAILURE",
        job_item_id=item.id,
        node_name="check_material_master",
        error_code="LookupError",
        error_message="no material_master row",
    )

    errors = repos.processing_errors.list_for_job_item(item.id)
    assert len(errors) == 1
    assert errors[0]["error_type"] == "LOOKUP_FAILURE"
    assert errors[0]["node_name"] == "check_material_master"


def test_processing_error_mark_resolved(repos, db_session):
    from app.models import JobItem, JobRun

    run = JobRun(job_type="PO_VALIDATION_BATCH", trigger_type="ON_DEMAND")
    db_session.add(run)
    db_session.flush()
    item = JobItem(job_run_id=run.id, item_type="PO_VALIDATION", status="RUNNING")
    db_session.add(item)
    db_session.flush()

    logged = repos.processing_errors.log(error_type="LOOKUP_FAILURE", job_item_id=item.id)
    resolved = repos.processing_errors.mark_resolved(logged["id"], resolved_by="ops@company.com")

    assert resolved["resolved"] is True
    assert resolved["resolved_by"] == "ops@company.com"


def test_list_threads_enriches_po_validation_rows_with_real_business_fields(repos):
    """`list_threads` is what backs the frontend's Error Queue -- it must
    return real PO number/material/quantity/retailer name per row (one batch
    query for the whole page), not just IDs, so the queue doesn't show a bare
    UUID as its only identifier."""
    retailer = repos.master_data.add_retailer("RET-WF2", "Acme Retail Co", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-BIZ-1",
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="20",
        ordered_quantity=480,
        unit_price=10.0,
        retailer_material_code="G10-PED-40LB-NEW",
    )
    repos.workflow_threads.create(
        stage="AWAITING_QTY_MISMATCH_DECISION", purchase_order_line_id=line["id"]
    )

    items, _ = repos.workflow_threads.list_threads()

    row = next(i for i in items if i["purchase_order_line_id"] == line["id"])
    assert row["po_number"] == "PO-BIZ-1"
    assert row["po_line_number"] == "20"
    assert row["retailer_material_code"] == "G10-PED-40LB-NEW"
    assert row["order_quantity"] == 480
    assert row["retailer_name"] == "Acme Retail Co"


def test_list_threads_enriches_cmir_rows_from_existing_metadata_no_extra_query(repos):
    """CMIR business fields are already captured in `metadata_json.latest_snapshot.cmir`
    at every interrupt (app/services/cmir/run_service.py) -- `list_threads` should
    surface them directly, with no extra join/query, unlike the PO-validation case."""
    email_event_id = _seed_email_event(repos)
    repos.workflow_threads.create(
        stage="AWAITING_APPROVAL",
        email_event_id=email_event_id,
        metadata={
            "latest_snapshot": {
                "cmir": {"customer_identity": "Walmart Inc", "material_identity": "MAT-1"}
            }
        },
    )

    items, _ = repos.workflow_threads.list_threads()

    row = next(i for i in items if i["email_event_id"] == email_event_id)
    assert row["customer_identity"] == "Walmart Inc"
    assert row["material_identity"] == "MAT-1"
    assert row["po_number"] is None


def test_list_threads_leaves_business_fields_none_when_data_is_genuinely_missing(repos):
    """A thread with no PO-line subject and no CMIR metadata yet must not
    fabricate values -- every business field stays None."""
    email_event_id = _seed_email_event(repos)
    repos.workflow_threads.create(stage="STARTED", email_event_id=email_event_id)

    items, _ = repos.workflow_threads.list_threads()

    row = next(i for i in items if i["email_event_id"] == email_event_id)
    assert row["customer_identity"] is None
    assert row["material_identity"] is None
    assert row["po_number"] is None
    assert row["retailer_name"] is None
