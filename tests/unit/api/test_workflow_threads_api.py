"""API tests for `app/api/v1/workflow_threads.py` -- the shared
`process.workflow_thread` surface used by `cmir` and `po_validation`
(Phase 7b route redesign, approved plan §6).

Was split across the pre-restructure `test_cmir_api.py` (`/threads/{id}/stage`,
`/snapshot`, `/missing-fields`, `/update`, `/decision`) and
`test_po_validation_api.py` (`/threads/{id}/qty-mismatch-decision`,
`/manual-cmir-entry`, and the snapshot-dispatch fallback tests) -- both now
collapse onto this one router.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.core.exceptions import NotFoundError

THREAD_CMIR = UUID("11111111-1111-1111-1111-111111111111")
THREAD_PO = UUID("22222222-2222-2222-2222-222222222222")
THREAD_UNKNOWN = UUID("99999999-9999-9999-9999-999999999999")

_UPDATED_AT = "2026-08-09T10:00:00+00:00"


def _cmir_stage_dict(*, stage: str = "AWAITING_APPROVAL", status: str = "waiting_approval") -> dict:
    return {
        "id": THREAD_CMIR,
        "job_item_id": None,
        "status": status,
        "stage": stage,
        "current_node": "review_extracted_cmir",
        "completed_at": None,
        "error": None,
        "metadata_json": {"checkpoint_thread_id": "thread_abc123", "agent_run_id": str(THREAD_CMIR)},
        "email_event_id": "9c76f0b3-1e8d-4f31-9d17-15f42ad8f970",
        "purchase_order_line_id": None,
        "updated_at": _UPDATED_AT,
    }


def _po_stage_dict(
    *, stage: str = "AWAITING_QTY_MISMATCH_DECISION", status: str = "waiting_qty_mismatch_decision"
) -> dict:
    return {
        "id": THREAD_PO,
        "job_item_id": None,
        "status": status,
        "stage": stage,
        "current_node": "human_qty_mismatch_decision",
        "completed_at": None,
        "error": None,
        "metadata_json": {"checkpoint_thread_id": "thread_po_abc123", "agent_run_id": str(THREAD_PO)},
        "email_event_id": None,
        "purchase_order_line_id": "66666666-6666-6666-6666-666666666666",
        "updated_at": _UPDATED_AT,
    }


class FakeCmirService:
    def get_stage(self, thread_id):
        if thread_id == THREAD_CMIR:
            return _cmir_stage_dict()
        if thread_id == THREAD_PO:
            return _po_stage_dict()
        raise NotFoundError(code="THREAD_NOT_FOUND", message="Unknown thread_id.", details={})

    def get_snapshot(self, thread_id):
        # Domain-agnostic-permissive at the repository level -- succeeds
        # regardless of which domain actually owns the thread (see
        # `app/api/v1/workflow_threads.py`'s docstring).
        base = self.get_stage(thread_id)
        return {**base, "history": []}

    def list_runs(self, *, view="threads", status=None, stage=None, agent_id=None, limit=50, cursor=None):
        return {"items": [_cmir_stage_dict(), _po_stage_dict()], "next_cursor": None}

    def submit_missing_fields(self, thread_id, *, actor, fields, expected_updated_at):
        return _cmir_stage_dict(stage="AWAITING_APPROVAL", status="waiting_approval")

    def update_draft(self, thread_id, *, actor, fields, expected_updated_at):
        return {
            "agent_run_id": "00000000-0000-0000-0000-000000001042",
            "thread_id": str(thread_id),
            "stage": "AWAITING_APPROVAL",
            "status": "waiting_approval",
            "pending_action_id": "00000000-0000-0000-0000-000000003003",
            "message": "Draft saved. Review again.",
        }

    def submit_decision(self, thread_id, *, actor, decision, expected_updated_at, reason=""):
        if decision == "reject" and not reason.strip():
            from app.core.exceptions import ValidationError

            raise ValidationError(code="VALIDATION_ERROR", message="Reject requires reason.", details={})
        return _cmir_stage_dict(stage="COMPLETED_APPROVED", status="completed_approved")


class FakePoValidationService:
    def get_snapshot(self, thread_id):
        if thread_id != THREAD_PO:
            raise NotFoundError(code="THREAD_NOT_FOUND", message="Unknown thread_id.", details={})
        return {
            "agent_run_id": "00000000-0000-0000-0000-000000000042",
            "thread_id": str(thread_id),
            "po_line_id": "66666666-6666-6666-6666-666666666666",
            "po_number": "PO-1",
            "po_line_number": "10",
            "customer_material_code": "ACME-MAT-1",
            "order_quantity": 100,
            "stage": "AWAITING_QTY_MISMATCH_DECISION",
            "candidate": {
                "sap_material_number": "MAT-1",
                "plant": "1000",
                "available_quantity": 40,
                "shortfall": 60,
                "suggested_substitute_material_code": "MAT-SUB",
            },
            "editable_fields": ["substitute_material_code"],
            "history": [],
            "updated_at": _UPDATED_AT,
        }

    def submit_qty_mismatch_decision(
        self, thread_id, *, actor, decision, substitute_material_code, expected_updated_at
    ):
        return _po_stage_dict(stage="READY_FOR_SO_CREATION_PARTIAL", status="ready_for_so_creation_partial")

    def submit_manual_cmir_entry(
        self, thread_id, *, actor, sap_material_number, description, expected_updated_at
    ):
        return _po_stage_dict(stage="READY_FOR_SO_CREATION", status="ready_for_so_creation")


@pytest.fixture
def cmir_service():
    return FakeCmirService()


@pytest.fixture
def po_validation_service():
    return FakePoValidationService()


def test_list_workflow_threads_returns_both_domains(client):
    response = client.get("/api/v1/workflow-threads")

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert {item["id"] for item in items} == {str(THREAD_CMIR), str(THREAD_PO)}


def test_list_workflow_threads_filters_by_domain_cmir(client):
    response = client.get("/api/v1/workflow-threads", params={"domain": "cmir"})

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(THREAD_CMIR)
    assert items[0]["email_event_id"] is not None


def test_list_workflow_threads_filters_by_domain_po_validation(client):
    response = client.get("/api/v1/workflow-threads", params={"domain": "po_validation"})

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(THREAD_PO)
    assert items[0]["purchase_order_line_id"] is not None


def test_get_workflow_thread_returns_stage_without_snapshot_by_default(client):
    response = client.get(f"/api/v1/workflow-threads/{THREAD_CMIR}")

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["stage"] == "AWAITING_APPROVAL"
    assert body["snapshot"] is None


def test_get_workflow_thread_unknown_returns_404(client):
    response = client.get(f"/api/v1/workflow-threads/{THREAD_UNKNOWN}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "THREAD_NOT_FOUND"


def test_get_workflow_thread_include_snapshot_dispatches_to_po_service_for_po_thread(client):
    response = client.get(f"/api/v1/workflow-threads/{THREAD_PO}", params={"include": "snapshot"})

    assert response.status_code == 200, response.text
    snapshot = response.json()["data"]["snapshot"]
    assert snapshot["po_line_id"] == "66666666-6666-6666-6666-666666666666"
    assert snapshot["candidate"]["suggested_substitute_material_code"] == "MAT-SUB"


def test_get_workflow_thread_include_snapshot_falls_back_to_cmir_service_for_cmir_thread(client):
    response = client.get(f"/api/v1/workflow-threads/{THREAD_CMIR}", params={"include": "snapshot"})

    assert response.status_code == 200, response.text
    snapshot = response.json()["data"]["snapshot"]
    assert snapshot["id"] == str(THREAD_CMIR)
    assert snapshot["history"] == []


def test_get_workflow_thread_rejects_unknown_include_value(client):
    response = client.get(f"/api/v1/workflow-threads/{THREAD_CMIR}", params={"include": "bogus"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INCLUDE"


def test_submit_missing_fields_returns_stage(client):
    response = client.post(
        f"/api/v1/workflow-threads/{THREAD_CMIR}/missing-fields",
        json={
            "actor": "reviewer@company.com",
            "fields": {"brand": "Brand A"},
            "expected_updated_at": _UPDATED_AT,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["stage"] == "AWAITING_APPROVAL"


def test_update_draft_returns_review_again_message(client):
    response = client.patch(
        f"/api/v1/workflow-threads/{THREAD_CMIR}/draft",
        json={
            "actor": "reviewer@company.com",
            "fields": {"brand": "Brand A"},
            "expected_updated_at": _UPDATED_AT,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["pending_action_id"] == "00000000-0000-0000-0000-000000003003"
    assert body["message"] == "Draft saved. Review again."


def test_submit_decision_cmir_approval_dispatches_to_cmir_service(client):
    response = client.post(
        f"/api/v1/workflow-threads/{THREAD_CMIR}/decisions",
        json={
            "decision_type": "CMIR_APPROVAL",
            "actor": "reviewer@company.com",
            "decision": "approve",
            "expected_updated_at": _UPDATED_AT,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["stage"] == "COMPLETED_APPROVED"


def test_submit_decision_qty_mismatch_dispatches_to_po_service(client):
    response = client.post(
        f"/api/v1/workflow-threads/{THREAD_PO}/decisions",
        json={
            "decision_type": "QTY_MISMATCH",
            "actor": "csr@company.com",
            "decision": "proceed_anyway",
            "expected_updated_at": _UPDATED_AT,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["stage"] == "READY_FOR_SO_CREATION_PARTIAL"


def test_submit_decision_manual_cmir_entry_dispatches_to_po_service(client):
    response = client.post(
        f"/api/v1/workflow-threads/{THREAD_PO}/decisions",
        json={
            "decision_type": "MANUAL_CMIR_ENTRY",
            "actor": "csr@company.com",
            "sap_material_number": "MAT-100",
            "description": "Legacy SKU",
            "expected_updated_at": _UPDATED_AT,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["stage"] == "READY_FOR_SO_CREATION"


def test_submit_decision_rejects_unknown_decision_type(client):
    response = client.post(
        f"/api/v1/workflow-threads/{THREAD_CMIR}/decisions",
        json={"decision_type": "BOGUS", "actor": "x", "expected_updated_at": _UPDATED_AT},
    )

    assert response.status_code == 422
