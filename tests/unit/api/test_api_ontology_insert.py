"""API tests for `app/api/v1/ontology_insert.py` (`POST .../requests`,
`POST .../requests/{thread_id}/decisions`). Mirrors
`tests/unit/api/test_api_ontology_update.py`'s convention -- overrides the
`ontology_insert_service` fixture with a fake, exercising the HTTP
contract/status codes/schemas, not the graph itself (covered by
`tests/unit/agents/test_ontology_insert_graph.py` and
`tests/integration/test_ontology_insert_hitl_postgres.py`/
`test_ontology_insert_api_postgres.py`).
"""

from __future__ import annotations

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.schemas.ontology.insert_proposal import InsertOperationProposal, NewEntityRef
from app.schemas.ontology.insert_request import OntologyInsertResponse, OntologyInsertStatus
from app.schemas.ontology.update_proposal import ProposalRelationship


class FakeOntologyInsertRunService:
    def __init__(self) -> None:
        self.start_calls: list[str] = []
        self.decision_calls: list[tuple] = []
        self.start_result: OntologyInsertResponse | None = None
        self.decision_result: OntologyInsertResponse | None = None
        self.decision_error: Exception | None = None

    def start(self, message: str) -> OntologyInsertResponse:
        self.start_calls.append(message)
        if self.start_result is not None:
            return self.start_result
        return OntologyInsertResponse(thread_id="thread_ontology_insert_test", status=OntologyInsertStatus.FAILED)

    def submit_decision(self, thread_id: str, *, decision=None, details=None) -> OntologyInsertResponse:
        self.decision_calls.append((thread_id, decision, details))
        if self.decision_error is not None:
            raise self.decision_error
        if self.decision_result is not None:
            return self.decision_result
        return OntologyInsertResponse(thread_id=thread_id, status=OntologyInsertStatus.FAILED)


@pytest.fixture
def ontology_insert_service() -> FakeOntologyInsertRunService:
    return FakeOntologyInsertRunService()


def _proposal() -> InsertOperationProposal:
    return InsertOperationProposal(
        material=NewEntityRef(material_code="MAT-3000"),
        plant=NewEntityRef(plant_code="P100", reused_existing=False),
        material_master=NewEntityRef(sap_material_number="SAP-3000"),
        relationship=ProposalRelationship(name="locatedAtPlant", target_entity="Plant", kind="foreign_key"),
        summary="Create material 'MAT-3000' with a MaterialMaster row (SAP number 'SAP-3000') at plant 'P100' (new plant)",
    )


def test_start_with_a_complete_sentence_returns_awaiting_approval_with_the_proposal(client, ontology_insert_service):
    ontology_insert_service.start_result = OntologyInsertResponse(
        thread_id="thread_ontology_insert_abc123",
        status=OntologyInsertStatus.AWAITING_APPROVAL,
        proposal=_proposal(),
    )

    response = client.post(
        "/api/v1/ontology-insert/requests",
        json={"message": "Create material MAT-3000 at plant P100 with SAP number SAP-3000."},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "awaiting_approval"
    assert body["data"]["proposal"]["material"]["material_code"] == "MAT-3000"
    assert ontology_insert_service.start_calls == [
        "Create material MAT-3000 at plant P100 with SAP number SAP-3000."
    ]


def test_start_with_a_partial_sentence_returns_awaiting_details(client, ontology_insert_service):
    ontology_insert_service.start_result = OntologyInsertResponse(
        thread_id="thread_ontology_insert_abc123",
        status=OntologyInsertStatus.AWAITING_DETAILS,
        missing_fields=["plant_code", "sap_material_number"],
    )

    response = client.post("/api/v1/ontology-insert/requests", json={"message": "Create material MAT-3000."})

    assert response.status_code == 202, response.text
    body = response.json()["data"]
    assert body["status"] == "awaiting_details"
    assert body["missing_fields"] == ["plant_code", "sap_material_number"]


def test_start_rejects_a_malformed_request_body(client):
    response = client.post("/api/v1/ontology-insert/requests", json={})
    assert response.status_code == 422


def test_details_decision_advances_to_awaiting_approval(client, ontology_insert_service):
    ontology_insert_service.decision_result = OntologyInsertResponse(
        thread_id="thread_ontology_insert_abc123",
        status=OntologyInsertStatus.AWAITING_APPROVAL,
        proposal=_proposal(),
    )

    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_abc123/decisions",
        json={"details": {"plant_code": "P100", "sap_material_number": "SAP-3000"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "awaiting_approval"
    assert ontology_insert_service.decision_calls == [
        ("thread_ontology_insert_abc123", None, {"plant_code": "P100", "sap_material_number": "SAP-3000"})
    ]


def test_approve_decision_returns_completed_with_execution_result(client, ontology_insert_service):
    ontology_insert_service.decision_result = OntologyInsertResponse(
        thread_id="thread_ontology_insert_abc123",
        status=OntologyInsertStatus.COMPLETED,
        execution_result={"material_id": "m-1", "plant_id": "p-1", "material_master_id": "mm-1"},
    )

    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_abc123/decisions",
        json={"decision": "approve"},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "completed"
    assert body["execution_result"]["material_master_id"] == "mm-1"
    assert ontology_insert_service.decision_calls == [("thread_ontology_insert_abc123", "approve", None)]


def test_reject_decision_returns_rejected_with_no_execution_result(client, ontology_insert_service):
    ontology_insert_service.decision_result = OntologyInsertResponse(
        thread_id="thread_ontology_insert_abc123", status=OntologyInsertStatus.REJECTED
    )

    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_abc123/decisions",
        json={"decision": "reject"},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "rejected"
    assert body["execution_result"] is None


def test_decision_rejects_a_body_with_neither_decision_nor_details(client):
    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_abc123/decisions",
        json={},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ONTOLOGY_INSERT_DECISION_OR_DETAILS_REQUIRED"


def test_decision_rejects_a_body_with_both_decision_and_details(client):
    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_abc123/decisions",
        json={"decision": "approve", "details": {"plant_code": "P100"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ONTOLOGY_INSERT_DECISION_OR_DETAILS_REQUIRED"


def test_decision_rejects_an_invalid_decision_value(client):
    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_abc123/decisions",
        json={"decision": "maybe"},
    )
    assert response.status_code == 422


def test_decision_on_an_unknown_thread_id_returns_404(client, ontology_insert_service):
    ontology_insert_service.decision_error = NotFoundError(
        code="ONTOLOGY_INSERT_THREAD_NOT_FOUND", message="No pending approval for this thread_id."
    )

    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_does-not-exist/decisions",
        json={"decision": "approve"},
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "ONTOLOGY_INSERT_THREAD_NOT_FOUND"


def test_sending_a_decision_when_details_are_awaited_returns_422(client, ontology_insert_service):
    ontology_insert_service.decision_error = ValidationError(
        code="ONTOLOGY_INSERT_DETAILS_REQUIRED", message="This thread is awaiting missing field details."
    )

    response = client.post(
        "/api/v1/ontology-insert/requests/thread_ontology_insert_abc123/decisions",
        json={"decision": "approve"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ONTOLOGY_INSERT_DETAILS_REQUIRED"


def test_start_returns_failed_for_a_duplicate_material(client, ontology_insert_service):
    ontology_insert_service.start_result = OntologyInsertResponse(
        thread_id="thread_ontology_insert_abc123",
        status=OntologyInsertStatus.FAILED,
        error={"error_type": "material_already_exists", "error_message": "Already exists."},
    )

    response = client.post(
        "/api/v1/ontology-insert/requests",
        json={"message": "Create material MAT-3000 at plant P100 with SAP number SAP-3000."},
    )

    assert response.status_code == 202, response.text
    body = response.json()["data"]
    assert body["status"] == "failed"
    assert body["error"]["error_type"] == "material_already_exists"
