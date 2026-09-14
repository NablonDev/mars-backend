"""API tests for retailer_agreement upload, penalty rule extraction, review, and publication.

`PenaltyRuleExtractionService` is mocked throughout (`_FakeRuleExtractionService`),
since the concrete service is being built concurrently with this router. The
retailer_agreement create/get/publications-audit endpoints go straight to the real
`RetailerAgreementRepository`/`RulePublicationRepository` against the shared SQLite
`database` fixture, the same pattern `test_api_penalty_disputes.py` uses.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    RulePublicationRepository,
)

RETAILER_AGREEMENT_BODY = {
    "retailer_id": str(uuid4()),
    "contract_code": "CONTRACT-RE-1",
    "title": "Test Retailer Agreement",
    "markdown_text": "## Penalties\nRetailer may assess a $50 fee per short-shipped case.",
}


def _fake_extracted_rule(**overrides) -> SimpleNamespace:
    fields = {
        "id": uuid4(),
        "retailer_agreement_id": uuid4(),
        "agent_run_id": uuid4(),
        "section": "Section 4.2",
        "clause_text": "Retailer may assess a $50 fee per short-shipped case.",
        "clause_fingerprint": "a" * 32,
        "penalty_category": "SHORT_SHIP",
        "calc_type": "PER_UNIT",
        "po_shortage_flag": True,
        "po_delay_flag": False,
        "pricing_readiness": "READY",
        "status": "PENDING_REVIEW",
        "confidence": 0.9,
        "review_notes": None,
        "attributes": [],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _FakeRuleExtractionService:
    """Double for `PenaltyRuleExtractionService`, covering the router-facing methods."""

    def __init__(self, database) -> None:
        self._database = database

    def create_retailer_agreement(
        self,
        retailer_id,
        contract_code,
        title,
        markdown_text,
        source_uri=None,
        effective_date=None,
        expiration_date=None,
    ):
        digest = hashlib.sha256((markdown_text or "").encode("utf-8")).hexdigest()
        session = self._database.new_session()
        retailer_agreements = RetailerAgreementRepository(session)
        created = retailer_agreements.add_retailer_agreement(
            retailer_id=retailer_id,
            contract_code=contract_code,
            title=title,
            document_sha256=digest,
            source_uri=source_uri,
            markdown_text=markdown_text,
            effective_date=effective_date,
            expiration_date=expiration_date,
        )
        session.commit()
        return created

    def start_extraction(self, retailer_agreement_id):
        return SimpleNamespace(job_run_id=uuid4(), agent_run_id=uuid4(), thread_id=uuid4(), staged_count=2)

    def get_extraction_status(self, retailer_agreement_id):
        return {
            "retailer_agreement_id": retailer_agreement_id,
            "agent_run_id": uuid4(),
            "workflow_thread_id": uuid4(),
            "status": "waiting_rule_review",
            "stage": "AWAITING_RULE_REVIEW",
            "current_node": "human_review",
            "completed_at": None,
            "error": None,
        }

    def list_extracted_rules(self, retailer_agreement_id, status=None):
        rows = [_fake_extracted_rule(retailer_agreement_id=retailer_agreement_id)]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        return rows

    def get_extracted_rule(self, retailer_agreement_id, extracted_rule_id):
        return _fake_extracted_rule(retailer_agreement_id=retailer_agreement_id, id=extracted_rule_id)

    def submit_review(self, retailer_agreement_id, extracted_rule_id, status, review_notes=None):
        return _fake_extracted_rule(
            retailer_agreement_id=retailer_agreement_id,
            id=extracted_rule_id,
            status=status,
            review_notes=review_notes,
        )

    def publish(self, retailer_agreement_id):
        outcome = SimpleNamespace(
            extracted_rule_id=uuid4(),
            outcome="PUBLISHED",
            penalty_rule_id=uuid4(),
            reason_code=None,
            reason_detail=None,
            created_at=datetime.now(UTC),
        )
        return SimpleNamespace(published_count=1, rejected_count=0, agent_run_id=uuid4(), outcomes=[outcome])


@pytest.fixture
def fake_service(database):
    return _FakeRuleExtractionService(database)


@pytest.fixture(autouse=True)
def _override_service(app, fake_service):
    from app.api.dependencies import get_penalty_rule_extraction_service

    app.dependency_overrides[get_penalty_rule_extraction_service] = lambda: fake_service


@pytest.fixture
def seeded_retailer_agreement_id(database) -> str:
    with database.session() as session:
        retailer_agreement = RetailerAgreementRepository(session).add_retailer_agreement(
            retailer_id=uuid4(),
            contract_code="CONTRACT-RE-SEED",
            title="Seeded Retailer Agreement",
            document_sha256="0" * 64,
            markdown_text="Seed clause text.",
        )
        retailer_agreement_id = str(retailer_agreement["id"])
    return retailer_agreement_id


def test_create_retailer_agreement_then_get(client):
    created = client.post("/api/v1/penalties/retailer-agreements", json=RETAILER_AGREEMENT_BODY)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["message"] == "Retailer agreement created."
    retailer_agreement_id = body["data"]["id"]

    fetched = client.get(f"/api/v1/penalties/retailer-agreements/{retailer_agreement_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["contract_code"] == RETAILER_AGREEMENT_BODY["contract_code"]


def test_create_retailer_agreement_idempotent_on_sha256_returns_200(client):
    first = client.post("/api/v1/penalties/retailer-agreements", json=RETAILER_AGREEMENT_BODY)
    assert first.status_code == 201

    second_body = {**RETAILER_AGREEMENT_BODY, "contract_code": "CONTRACT-RE-1-DUPLICATE"}
    second = client.post("/api/v1/penalties/retailer-agreements", json=second_body)
    assert second.status_code == 200, second.text
    assert second.json()["message"] == "Retailer agreement already exists."
    assert second.json()["data"]["id"] == first.json()["data"]["id"]


def test_get_retailer_agreement_unknown_id_returns_404(client):
    resp = client.get(f"/api/v1/penalties/retailer-agreements/{uuid4()}")
    assert resp.status_code == 404


def test_start_extraction_returns_202(client, seeded_retailer_agreement_id):
    resp = client.post(f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extract")
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert data["retailer_agreement_id"] == seeded_retailer_agreement_id
    assert "agent_run_id" in data
    assert "workflow_thread_id" in data


def test_list_retailer_agreements_returns_the_seeded_row(client, seeded_retailer_agreement_id):
    resp = client.get("/api/v1/penalties/retailer-agreements")
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()["data"]]
    assert seeded_retailer_agreement_id in ids


def test_get_extraction_status_returns_the_service_result(client, seeded_retailer_agreement_id):
    resp = client.get(f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extraction")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["retailer_agreement_id"] == seeded_retailer_agreement_id
    assert data["status"] == "waiting_rule_review"
    assert data["stage"] == "AWAITING_RULE_REVIEW"


def test_list_extracted_rules_with_status_filter(client, seeded_retailer_agreement_id):
    all_rows = client.get(
        f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extracted-rules"
    )
    assert all_rows.status_code == 200
    assert len(all_rows.json()["data"]) == 1

    filtered = client.get(
        f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extracted-rules",
        params={"status": "APPROVED"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["data"] == []


def test_get_extracted_rule_returns_one_rule(client, seeded_retailer_agreement_id):
    extracted_rule_id = uuid4()
    resp = client.get(
        f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extracted-rules/{extracted_rule_id}"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == str(extracted_rule_id)
    assert data["retailer_agreement_id"] == seeded_retailer_agreement_id


def test_review_extracted_rule_happy_path(client, seeded_retailer_agreement_id):
    extracted_rule_id = uuid4()
    resp = client.post(
        f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extracted-rules/{extracted_rule_id}/review",
        json={"status": "APPROVED", "review_notes": "Looks right."},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["review_notes"] == "Looks right."


def test_review_extracted_rule_invalid_status_returns_422(client, seeded_retailer_agreement_id):
    resp = client.post(
        f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extracted-rules/{uuid4()}/review",
        json={"status": "MAYBE"},
    )
    assert resp.status_code == 422


def test_publish_retailer_agreement_rules(client, seeded_retailer_agreement_id):
    resp = client.post(f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/publish")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["retailer_agreement_id"] == seeded_retailer_agreement_id
    assert data["published_count"] == 1
    assert data["rejected_count"] == 0
    assert len(data["outcomes"]) == 1
    assert data["outcomes"][0]["outcome"] == "PUBLISHED"


def test_publication_audit_includes_rejection_histogram(client, database, seeded_retailer_agreement_id):
    # The audit endpoint reads rule_publication directly rather than through the service,
    # so the rows have to be real: the stubbed publish() returns a result without writing one.
    with database.session() as session:
        extracted = ExtractedPenaltyRuleRepository(session).add_extracted_rule(
            retailer_agreement_id=UUID(seeded_retailer_agreement_id),
            agent_run_id=uuid4(),
            clause_text="Seed clause text.",
            clause_fingerprint="a" * 32,
            penalty_category="SHORT_SHIP",
            calc_type="FORMULA_OTHER",
            pricing_readiness="UNSUPPORTED_SHAPE",
            confidence=0.9,
        )
        RulePublicationRepository(session).record(
            extracted_rule_id=extracted.id,
            agent_run_id=extracted.agent_run_id,
            outcome="REJECTED",
            reason_code="UNSUPPORTED_CALC_TYPE",
            reason_detail="calc_type='FORMULA_OTHER'",
        )

    resp = client.get(f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/publications")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["retailer_agreement_id"] == seeded_retailer_agreement_id
    assert len(data["outcomes"]) == 1
    assert data["outcomes"][0]["outcome"] == "REJECTED"
    assert data["rejection_reason_histogram"] == {"UNSUPPORTED_CALC_TYPE": 1}
