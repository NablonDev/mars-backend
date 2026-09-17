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

from app.models.enums import WorkflowThreadSubjectType


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


def _seed_retailer_agreement(repos):
    retailer = repos.master_data.add_retailer("RET-WF-CONTRACT", "Retailer", None, "SUM")
    retailer_agreement = repos.retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code="CONTRACT-WF-1",
        title="Example Agreement",
        document_sha256="a" * 64,
        markdown_text="# Agreement",
    )
    return retailer_agreement["id"]


def test_create_creates_thread_and_subject_for_email_event(repos):
    email_event_id = _seed_email_event(repos)

    thread = repos.workflow_threads.create(
        stage="AWAITING_APPROVAL",
        subject_type=WorkflowThreadSubjectType.EMAIL_EVENT,
        subject_id=email_event_id,
        metadata={"cmir": {"brand": "Brand A"}},
    )

    assert thread["stage"] == "AWAITING_APPROVAL"
    assert thread["subject_type"] == WorkflowThreadSubjectType.EMAIL_EVENT
    assert thread["subject_id"] == email_event_id
    assert thread["metadata_json"]["cmir"]["brand"] == "Brand A"


def test_create_creates_thread_and_subject_for_retailer_agreement(repos):
    retailer_agreement_id = _seed_retailer_agreement(repos)

    thread = repos.workflow_threads.create(
        stage="AWAITING_RULE_REVIEW",
        subject_type=WorkflowThreadSubjectType.RETAILER_AGREEMENT,
        subject_id=retailer_agreement_id,
    )

    assert thread["subject_type"] == WorkflowThreadSubjectType.RETAILER_AGREEMENT
    assert thread["subject_id"] == retailer_agreement_id


def test_get_latest_by_subject_returns_the_most_recently_updated_for_retailer_agreement(repos):
    retailer_agreement_id = _seed_retailer_agreement(repos)
    first = repos.workflow_threads.create(
        stage="STARTED",
        subject_type=WorkflowThreadSubjectType.RETAILER_AGREEMENT,
        subject_id=retailer_agreement_id,
    )
    repos.workflow_threads.update_status(first["id"], status="running", stage="AWAITING_RULE_REVIEW")
    second = repos.workflow_threads.create(
        stage="STARTED",
        subject_type=WorkflowThreadSubjectType.RETAILER_AGREEMENT,
        subject_id=retailer_agreement_id,
    )

    latest = repos.workflow_threads.get_latest_by_subject(
        WorkflowThreadSubjectType.RETAILER_AGREEMENT, retailer_agreement_id
    )
    assert latest["id"] == second["id"]


def test_get_by_id_returns_thread_with_subject(repos):
    purchase_order_line_id = _seed_purchase_order_line(repos)
    created = repos.workflow_threads.create(
        stage="STARTED",
        subject_type=WorkflowThreadSubjectType.PURCHASE_ORDER_LINE,
        subject_id=purchase_order_line_id,
    )

    fetched = repos.workflow_threads.get_by_id(created["id"])

    assert fetched["subject_id"] == purchase_order_line_id
    assert fetched["status"] == "running"


def test_get_latest_by_subject_returns_the_most_recently_updated_for_email_event(repos):
    email_event_id = _seed_email_event(repos)
    first = repos.workflow_threads.create(
        stage="STARTED", subject_type=WorkflowThreadSubjectType.EMAIL_EVENT, subject_id=email_event_id
    )
    repos.workflow_threads.update_status(first["id"], status="running", stage="EXTRACTING")
    second = repos.workflow_threads.create(
        stage="STARTED", subject_type=WorkflowThreadSubjectType.EMAIL_EVENT, subject_id=email_event_id
    )

    latest = repos.workflow_threads.get_latest_by_subject(
        WorkflowThreadSubjectType.EMAIL_EVENT, email_event_id
    )
    assert latest["id"] == second["id"]


def test_update_status_completes_a_thread(repos):
    email_event_id = _seed_email_event(repos)
    thread = repos.workflow_threads.create(
        stage="STARTED", subject_type=WorkflowThreadSubjectType.EMAIL_EVENT, subject_id=email_event_id
    )

    repos.workflow_threads.update_status(
        thread["id"], status="completed_approved", stage="DONE", completed=True
    )

    fetched = repos.workflow_threads.get_by_id(thread["id"])
    assert fetched["status"] == "completed_approved"
    assert fetched["current_node"] is None
    assert fetched["completed_at"] is not None


def test_human_action_create_open_and_complete(repos):
    email_event_id = _seed_email_event(repos)
    thread = repos.workflow_threads.create(
        stage="AWAITING_APPROVAL",
        subject_type=WorkflowThreadSubjectType.EMAIL_EVENT,
        subject_id=email_event_id,
    )

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
    thread = repos.workflow_threads.create(
        stage="AWAITING_MISSING_FIELDS",
        subject_type=WorkflowThreadSubjectType.EMAIL_EVENT,
        subject_id=email_event_id,
    )
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
    email_event_id = _seed_email_event(repos)
    thread = repos.workflow_threads.create(
        stage="AWAITING_APPROVAL",
        subject_type=WorkflowThreadSubjectType.EMAIL_EVENT,
        subject_id=email_event_id,
    )
    pending_id = repos.human_actions.create_open(
        interrupt_type="approval_required",
        request_payload={"reason": "approval_required"},
        workflow_thread_id=thread["id"],
    )
    repos.human_actions.complete(pending_id, response_payload={}, actor="reviewer@company.com")

    with pytest.raises(ValueError):
        repos.human_actions.apply_human_action(
            pending_action_id=pending_id,
            workflow_thread_id=thread["id"],
            response_payload={},
            actor="reviewer@company.com",
            next_status="waiting_approval",
            next_stage="AWAITING_APPROVAL",
        )


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


def test_processing_error_log_and_list_for_purchase_order_line(repos):
    """Gap 2: a purchase_order_line_id-keyed lookup, independent of any
    job_item/agent_run -- covers a line that fails before ever reaching a
    human interrupt (no workflow_thread exists yet)."""
    retailer = repos.master_data.add_retailer("RET-PE", "Retailer", None, "SUM")
    plant = repos.master_data.add_plant("PLANT-PE", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-PE-1", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
    )

    repos.processing_errors.log(
        error_type="LOOKUP_FAILURE",
        purchase_order_line_id=line["id"],
        node_name="check_material_master",
    )

    errors = repos.processing_errors.list_for_purchase_order_line(line["id"])
    assert len(errors) == 1
    assert errors[0]["purchase_order_line_id"] == line["id"]
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


def test_processing_error_log_rolls_back_only_its_own_savepoint_on_a_write_failure(repos, db_session):
    """A failed flush (a NUL byte in `error_message`/`raw_error_detail` raises this way in
    production) must not leave this shared session in a failed transactional state for a
    later, unrelated caller -- mirrors the same regression covered for AgentTraceRepository."""

    def _failing_flush() -> None:
        raise ValueError("A string literal cannot contain NUL (0x00) characters.")

    db_session.flush = _failing_flush

    with pytest.raises(ValueError):
        repos.processing_errors.log(error_type="LOOKUP_FAILURE", error_message="boom")

    del db_session.flush  # restore the bound method now that the fake did its job

    logged = repos.processing_errors.log(error_type="LOOKUP_FAILURE", error_message="clean write")
    assert logged["error_message"] == "clean write"
