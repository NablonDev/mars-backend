"""Tests for the `cmir` schema repositories (`CmirRecordRepository`,
`EmailRepository`). Was tests/unit/repositories/test_cmir_repositories.py
against `PostgresCMIRRepository`/`PostgresEmailRepository`, using a hand
-rolled `FakeSession`/`FakeDatabase` -- relocated onto real in-memory
SQLite (see conftest.py's `db_session`/`repos` fixtures) now that these
repositories take an injected `Session` like the rest of the codebase,
rather than a `Database` they open a session from per call (see
app/repositories/process/agent_registry.py's module docstring for that
switch). The `PostgresWorkflowThreadRepository`/`PostgresHITLActionRepository`/
`PostgresPendingHumanActionRepository`/`PostgresHITLStateRepository` tests
that used to live in this file moved to test_workflow_repository.py,
against their `process`-schema replacements.
"""

from __future__ import annotations

import pytest

from app.repositories.cmir.cmir_record import CmirVersionConflict


def test_get_current_returns_none_when_no_current_row(repos):
    assert repos.cmir_records.get_current("Acme Manufacturing Ltd", "ACME-PE200-STD") is None


def test_supersede_and_insert_create_path_when_no_current_record_exists(repos, db_session):
    new_id = repos.cmir_records.supersede_and_insert(
        customer_identity="Acme Manufacturing Ltd",
        target_customer_material_ref="ACME-PE200-STD",
        merged={
            "sender_type": "customer",
            "customer_identity": "Acme Manufacturing Ltd",
            "material_identity": "Polyethylene Resin PE-200",
            "intent_phrase": None,
            "existing_cmir_ref": "",
            "brand": "AcmePlast",
            "site": "Site A",
            "target_grd_code": "GRD-1",
            "target_customer_material_ref": "ACME-PE200-STD",
            "effective_date": None,
            "reason": "",
        },
        expected_current_id=None,
    )

    current = repos.cmir_records.get_current("Acme Manufacturing Ltd", "ACME-PE200-STD")
    assert current["id"] == new_id
    assert current["brand"] == "AcmePlast"
    # Every content field must be present -- this is the exact shape
    # merge_with_active's `existing` argument depends on.
    for field_name in (
        "sender_type",
        "customer_identity",
        "material_identity",
        "intent_phrase",
        "existing_cmir_ref",
        "brand",
        "site",
        "target_grd_code",
        "target_customer_material_ref",
        "effective_date",
        "reason",
    ):
        assert field_name in current


def test_supersede_and_insert_update_path_retires_old_record(repos, db_session):
    old_id = repos.cmir_records.supersede_and_insert(
        customer_identity="Acme Manufacturing Ltd",
        target_customer_material_ref="ACME-PE200-STD",
        merged={
            "sender_type": "customer",
            "customer_identity": "Acme Manufacturing Ltd",
            "material_identity": "Polyethylene Resin PE-200",
            "existing_cmir_ref": "",
            "brand": "AcmePlast",
            "site": "Site A",
            "target_grd_code": "GRD-1",
            "target_customer_material_ref": "ACME-PE200-STD",
            "reason": "",
        },
        expected_current_id=None,
    )

    new_id = repos.cmir_records.supersede_and_insert(
        customer_identity="Acme Manufacturing Ltd",
        target_customer_material_ref="ACME-PE200-STD",
        merged={
            "sender_type": "customer",
            "customer_identity": "Acme Manufacturing Ltd",
            "material_identity": "Polyethylene Resin PE-200",
            "existing_cmir_ref": "",
            "brand": "AcmePlast Europe",
            "site": "Site A",
            "target_grd_code": "GRD-1",
            "target_customer_material_ref": "ACME-PE200-STD",
            "reason": "",
        },
        expected_current_id=old_id,
    )

    from app.models import CmirRecord

    old_row = db_session.get(CmirRecord, old_id)
    assert old_row.is_current is False
    assert old_row.valid_to is not None
    assert old_row.superseded_by_id == new_id

    current = repos.cmir_records.get_current("Acme Manufacturing Ltd", "ACME-PE200-STD")
    assert current["id"] == new_id
    assert current["brand"] == "AcmePlast Europe"


def test_supersede_and_insert_raises_conflict_when_current_id_mismatches(repos):
    repos.cmir_records.supersede_and_insert(
        customer_identity="Acme Manufacturing Ltd",
        target_customer_material_ref="ACME-PE200-STD",
        merged={
            "sender_type": "customer",
            "customer_identity": "Acme Manufacturing Ltd",
            "material_identity": "MAT",
            "existing_cmir_ref": "",
            "brand": "AcmePlast",
            "site": "Site A",
            "target_grd_code": "GRD-1",
            "target_customer_material_ref": "ACME-PE200-STD",
            "reason": "",
        },
        expected_current_id=None,
    )

    with pytest.raises(CmirVersionConflict):
        repos.cmir_records.supersede_and_insert(
            customer_identity="Acme Manufacturing Ltd",
            target_customer_material_ref="ACME-PE200-STD",
            merged={
                "sender_type": "customer",
                "customer_identity": "Acme Manufacturing Ltd",
                "material_identity": "MAT",
                "existing_cmir_ref": "",
                "brand": "Other",
                "site": "Site A",
                "target_grd_code": "GRD-1",
                "target_customer_material_ref": "ACME-PE200-STD",
                "reason": "",
            },
            expected_current_id=None,  # stale -- someone else already created the current row
        )


def test_find_latest_for_customer_material_filters_on_is_current(repos):
    repos.cmir_records.supersede_and_insert(
        customer_identity="CUST-1",
        target_customer_material_ref="ACME-MAT-1",
        merged={
            "sender_type": "customer",
            "customer_identity": "CUST-1",
            "material_identity": "MAT-1",
            "existing_cmir_ref": "",
            "brand": "Brand",
            "site": "Site",
            "target_grd_code": "GRD",
            "target_customer_material_ref": "ACME-MAT-1",
            "reason": "",
        },
        expected_current_id=None,
    )

    match = repos.cmir_records.find_latest_for_customer_material("CUST-1", "ACME-MAT-1")
    assert match["material_identity"] == "MAT-1"


def test_find_latest_for_customer_material_returns_none_when_no_current_row(repos):
    assert repos.cmir_records.find_latest_for_customer_material("CUST-1", "ACME-MAT-1") is None


def test_create_manual_mapping_has_no_email_event_id(repos, db_session):
    from app.models import CmirRecord

    new_id = repos.cmir_records.create_manual_mapping(
        customer_identity="CUST-1",
        material_identity="MAT-100",
        target_customer_material_ref="ACME-MAT-1",
        description="Legacy SKU",
    )

    row = db_session.get(CmirRecord, new_id)
    assert row.email_event_id is None
    assert row.material_identity == "MAT-100"
    assert row.reason == "Legacy SKU"
    assert row.is_current is True


def test_create_manual_mapping_raises_conflict_if_current_appeared(repos):
    # A current row unexpectedly exists (a race between validate_against_cmir's
    # "not found" and this call) -- create_manual_mapping must not silently
    # create a second current row for the same entity.
    repos.cmir_records.create_manual_mapping(
        customer_identity="CUST-1",
        material_identity="MAT-100",
        target_customer_material_ref="ACME-MAT-1",
    )

    with pytest.raises(CmirVersionConflict):
        repos.cmir_records.create_manual_mapping(
            customer_identity="CUST-1",
            material_identity="MAT-200",
            target_customer_material_ref="ACME-MAT-1",
        )


def test_email_repository_save_persists_a_processed_row(repos):
    email_id = repos.emails.save(
        sender="customer@example.com",
        subject="CMIR Request",
        raw_content="body",
        source_message_id="msg-001",
    )

    state = repos.emails.get_queue_state(email_id)
    assert state["queue_status"] == "processed"


def test_email_repository_save_for_queue_is_idempotent_on_source_message_id(repos):
    first = repos.emails.save_for_queue(
        sender="customer@example.com", subject="Subj", raw_content="body", source_message_id="msg-001"
    )
    second = repos.emails.save_for_queue(
        sender="customer@example.com", subject="Subj", raw_content="body", source_message_id="msg-001"
    )

    assert first["id"] == second["id"]


def test_email_repository_queue_lifecycle_transitions(repos):
    saved = repos.emails.save_for_queue(
        sender="customer@example.com", subject="Subj", raw_content="body", source_message_id="msg-002"
    )
    email_id = saved["id"]

    repos.emails.mark_queued(email_id, "queue-msg-1")
    assert repos.emails.get_queue_state(email_id)["queue_status"] == "queued"

    repos.emails.mark_processing(email_id, "queue-msg-1")
    assert repos.emails.get_queue_state(email_id)["queue_status"] == "processing"
    assert repos.emails.get_queue_state(email_id)["queue_delivery_count"] == 1

    repos.emails.mark_queue_processed(email_id)
    assert repos.emails.get_queue_state(email_id)["queue_status"] == "processed"


def test_email_repository_list_pending_excludes_processed_rows(repos):
    new_row = repos.emails.save_for_queue(
        sender="new@example.com", subject="New", raw_content="body", source_message_id="msg-pending-1"
    )
    queued_row = repos.emails.save_for_queue(
        sender="queued@example.com", subject="Queued", raw_content="body", source_message_id="msg-pending-2"
    )
    repos.emails.mark_queued(queued_row["id"], "queue-msg-2")
    processed_row = repos.emails.save_for_queue(
        sender="processed@example.com",
        subject="Processed",
        raw_content="body",
        source_message_id="msg-pending-3",
    )
    repos.emails.mark_queue_processed(processed_row["id"])

    pending_ids = {row["id"] for row in repos.emails.list_pending(limit=50)}

    assert new_row["id"] in pending_ids
    assert queued_row["id"] in pending_ids
    assert processed_row["id"] not in pending_ids


def test_email_repository_mark_queue_failed_retryable_goes_back_to_new(repos):
    saved = repos.emails.save_for_queue(
        sender="customer@example.com", subject="Subj", raw_content="body", source_message_id="msg-003"
    )
    email_id = saved["id"]

    repos.emails.mark_queue_failed(email_id, "boom", retryable=True)
    state = repos.emails.get_queue_state(email_id)
    assert state["queue_status"] == "new"
    assert state["queue_error"] == "boom"


def test_email_repository_update_extraction_persists_extracted_json(repos):
    email_id = repos.emails.save(
        sender="customer@example.com", subject="Subj", raw_content="body", source_message_id="msg-004"
    )

    repos.emails.update_extraction(
        email_id,
        extracted_json={"brand": "AcmePlast"},
        missing_fields=["site"],
        status="incomplete",
    )

    row = repos.emails.get(email_id)
    assert row["extracted_json"] == {"brand": "AcmePlast"}
    assert row["missing_fields"] == ["site"]
    assert row["status"] == "incomplete"
