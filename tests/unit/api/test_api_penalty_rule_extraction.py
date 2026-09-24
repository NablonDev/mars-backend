"""API tests for penalty rule extraction, reviewer revisions, and publication.

`PenaltyRuleExtractionService` and `RuleRevisionService` are mocked throughout
(`_FakeRuleExtractionService`/`_FakeRuleRevisionService`). Only the LLM-running and
publish routes remain here -- retailer agreement create/list/get, extraction status,
extraction runs, extracted-rule reads, review, and revision listing moved to mars-bff
(see `docs/API.md`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import ExtractedPenaltyRuleRevisionRepository


class _FakeRuleExtractionService:
    """Double for `PenaltyRuleExtractionService`, covering the router-facing methods."""

    def start_extraction(self, retailer_agreement_id):
        return SimpleNamespace(job_run_id=uuid4(), agent_run_id=uuid4(), staged_count=2)

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
def fake_service():
    return _FakeRuleExtractionService()


@pytest.fixture(autouse=True)
def _override_service(app, fake_service):
    from app.api.dependencies import get_penalty_rule_extraction_service

    app.dependency_overrides[get_penalty_rule_extraction_service] = lambda: fake_service


class _FakeRuleRevisionService:
    """Double for `RuleRevisionService`, writing real revision rows against the shared SQLite `database`."""

    def __init__(self, database) -> None:
        self._database = database

    def request_revision(self, retailer_agreement_id, extracted_rule_id, *, instruction, requested_by):
        session = self._database.new_session()
        try:
            created = ExtractedPenaltyRuleRevisionRepository(session).create(
                extracted_rule_id,
                instruction=instruction,
                requested_by=requested_by,
                before_snapshot={"penalty_category": "SHORT_SHIP"},
            )
            session.commit()
        finally:
            session.close()
        return created


@pytest.fixture
def fake_revision_service(database):
    return _FakeRuleRevisionService(database)


@pytest.fixture(autouse=True)
def _override_revision_service(app, fake_revision_service):
    from app.api.dependencies import get_rule_revision_service

    app.dependency_overrides[get_rule_revision_service] = lambda: fake_revision_service


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


def test_start_extraction_returns_202(client, seeded_retailer_agreement_id):
    resp = client.post(f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/extract")
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert data["retailer_agreement_id"] == seeded_retailer_agreement_id
    assert "agent_run_id" in data
    assert data["staged_count"] == 2


def test_publish_retailer_agreement_rules(client, seeded_retailer_agreement_id):
    resp = client.post(f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}/publish")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["retailer_agreement_id"] == seeded_retailer_agreement_id
    assert data["published_count"] == 1
    assert data["rejected_count"] == 0
    assert len(data["outcomes"]) == 1
    assert data["outcomes"][0]["outcome"] == "PUBLISHED"


# ---------------------------------------------------------------------------
# reviewer revision loop
# ---------------------------------------------------------------------------


def test_request_rule_revision_returns_202_and_schedules_the_background_task(
    client, monkeypatch, seeded_retailer_agreement_id
):
    recorded: dict = {}

    def _recorder(revision_id, *, database, classify, extract_facts):
        recorded["revision_id"] = revision_id

    monkeypatch.setattr("app.api.v1.penalties.rule_extraction.run_rule_revision", _recorder)
    extracted_rule_id = uuid4()

    resp = client.post(
        f"/api/v1/penalties/retailer-agreements/{seeded_retailer_agreement_id}"
        f"/extracted-rules/{extracted_rule_id}/revisions",
        json={"instruction": "Use $75, not $50.", "requested_by": "reviewer@example.com"},
    )

    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert data["extracted_rule_id"] == str(extracted_rule_id)
    assert data["status"] == "QUEUED"
    assert data["revision_no"] == 1
    assert recorded["revision_id"] == UUID(data["id"])
