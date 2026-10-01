"""API tests for `app/api/v1/ontology_update.py` (`POST .../requests`,
`POST .../requests/{thread_id}/decisions`).

Overrides the `ontology_update_service` fixture (same pattern as
`cmir_run_service`/`po_validation_service` in `tests/conftest.py`) with a
fake implementing `OntologyUpdateRunService`'s two methods -- this router
is thin by design (parse, call the service, wrap in `Envelope`), so these
tests exercise the HTTP contract/status codes/schemas, not the graph
itself (already covered end-to-end by
`tests/unit/agents/test_ontology_update_graph.py` and
`tests/integration/test_ontology_update_hitl_postgres.py`).
"""

from __future__ import annotations

import pytest

from app.core.exceptions import NotFoundError
from app.schemas.ontology.update_proposal import (
    OperationProposal,
    ProposalMaterialRef,
    ProposalRelationship,
)
from app.schemas.ontology.update_request import OntologyUpdateResponse, OntologyUpdateStatus


class FakeOntologyUpdateRunService:
    def __init__(self) -> None:
        self.start_calls: list[str] = []
        self.decision_calls: list[tuple[str, str]] = []
        self.start_result: OntologyUpdateResponse | None = None
        self.decision_result: OntologyUpdateResponse | None = None
        self.decision_error: Exception | None = None

    def start(self, message: str) -> OntologyUpdateResponse:
        self.start_calls.append(message)
        if self.start_result is not None:
            return self.start_result
        return OntologyUpdateResponse(
            thread_id="thread_ontology_update_test", status=OntologyUpdateStatus.FAILED
        )

    def submit_decision(self, thread_id: str, decision: str) -> OntologyUpdateResponse:
        self.decision_calls.append((thread_id, decision))
        if self.decision_error is not None:
            raise self.decision_error
        if self.decision_result is not None:
            return self.decision_result
        return OntologyUpdateResponse(thread_id=thread_id, status=OntologyUpdateStatus.FAILED)


@pytest.fixture
def ontology_update_service() -> FakeOntologyUpdateRunService:
    return FakeOntologyUpdateRunService()


def _proposal() -> OperationProposal:
    return OperationProposal(
        operation="UPDATE",
        entity="MaterialMaster",
        target=ProposalMaterialRef(material_code="MAT-DISC-1001", material_master_id="mm-1"),
        relationship=ProposalRelationship(name="succeededBy", target_entity="Material", kind="foreign_key"),
        current_value=ProposalMaterialRef(),
        new_value=ProposalMaterialRef(material_code="MAT-REPL-1001", material_id="m-2"),
        summary="Set MAT-REPL-1001 as the replacement material for MAT-DISC-1001",
    )


def test_start_returns_awaiting_approval_with_the_proposal(client, ontology_update_service):
    ontology_update_service.start_result = OntologyUpdateResponse(
        thread_id="thread_ontology_update_abc123",
        status=OntologyUpdateStatus.AWAITING_APPROVAL,
        proposal=_proposal(),
    )

    response = client.post(
        "/api/v1/ontology-update/requests",
        json={"message": "Update the replacement material for MAT-DISC-1001 to MAT-REPL-1001."},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "awaiting_approval"
    assert body["data"]["thread_id"] == "thread_ontology_update_abc123"
    assert body["data"]["proposal"]["relationship"]["name"] == "succeededBy"
    assert ontology_update_service.start_calls == [
        "Update the replacement material for MAT-DISC-1001 to MAT-REPL-1001."
    ]


def test_start_rejects_a_malformed_request_body(client):
    response = client.post("/api/v1/ontology-update/requests", json={})
    assert response.status_code == 422


def test_approve_decision_returns_completed_with_execution_result(client, ontology_update_service):
    ontology_update_service.decision_result = OntologyUpdateResponse(
        thread_id="thread_ontology_update_abc123",
        status=OntologyUpdateStatus.COMPLETED,
        execution_result={"material_master_id": "mm-1", "follow_up_material_id": "m-2"},
    )

    response = client.post(
        "/api/v1/ontology-update/requests/thread_ontology_update_abc123/decisions",
        json={"decision": "approve"},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "completed"
    assert body["execution_result"]["follow_up_material_id"] == "m-2"
    assert ontology_update_service.decision_calls == [("thread_ontology_update_abc123", "approve")]


def test_reject_decision_returns_rejected_with_no_execution_result(client, ontology_update_service):
    ontology_update_service.decision_result = OntologyUpdateResponse(
        thread_id="thread_ontology_update_abc123", status=OntologyUpdateStatus.REJECTED
    )

    response = client.post(
        "/api/v1/ontology-update/requests/thread_ontology_update_abc123/decisions",
        json={"decision": "reject"},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "rejected"
    assert body["execution_result"] is None
    assert ontology_update_service.decision_calls == [("thread_ontology_update_abc123", "reject")]


def test_decision_rejects_an_invalid_decision_value(client):
    response = client.post(
        "/api/v1/ontology-update/requests/thread_ontology_update_abc123/decisions",
        json={"decision": "maybe"},
    )
    assert response.status_code == 422


def test_decision_on_an_unknown_thread_id_returns_404(client, ontology_update_service):
    ontology_update_service.decision_error = NotFoundError(
        code="ONTOLOGY_UPDATE_THREAD_NOT_FOUND", message="No pending approval for this thread_id."
    )

    response = client.post(
        "/api/v1/ontology-update/requests/thread_ontology_update_does-not-exist/decisions",
        json={"decision": "approve"},
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "ONTOLOGY_UPDATE_THREAD_NOT_FOUND"


def test_start_returns_clarification_required_for_an_ambiguous_material(client, ontology_update_service):
    ontology_update_service.start_result = OntologyUpdateResponse(
        thread_id="thread_ontology_update_abc123",
        status=OntologyUpdateStatus.CLARIFICATION_REQUIRED,
        clarification_reason="MAT-DISC-1001 has 2 MaterialMaster rows; specify which plant.",
    )

    response = client.post(
        "/api/v1/ontology-update/requests",
        json={"message": "Update the replacement material for MAT-DISC-1001 to MAT-REPL-1001."},
    )

    assert response.status_code == 202, response.text
    body = response.json()["data"]
    assert body["status"] == "clarification_required"
    assert "MAT-DISC-1001" in body["clarification_reason"]


def test_start_returns_failed_for_an_unknown_material(client, ontology_update_service):
    ontology_update_service.start_result = OntologyUpdateResponse(
        thread_id="thread_ontology_update_abc123",
        status=OntologyUpdateStatus.FAILED,
        error={"error_type": "unknown_source_identifier", "error_message": "No Material found."},
    )

    response = client.post(
        "/api/v1/ontology-update/requests",
        json={"message": "Update the replacement material for MAT-NOPE to MAT-REPL-1001."},
    )

    assert response.status_code == 202, response.text
    body = response.json()["data"]
    assert body["status"] == "failed"
    assert body["error"]["error_type"] == "unknown_source_identifier"
