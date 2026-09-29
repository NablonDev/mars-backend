"""Tests for `app.services.penalties.rule_extraction.revision`
(RuleRevisionService.request_revision, and the module-level `run_rule_revision`
background task). Listing a rule's revision history moved to mars-bff.

`RuleRevisionService` runs against the shared in-memory SQLite `db_session` fixture's
repositories, matching `test_service.py`'s pattern. `run_rule_revision` is exercised
through the `database` fixture directly -- the same object the FastAPI background task
receives -- with fake `classify`/`extract_facts` callables; no LLM call is ever reached.
`database`/`db_session` share one SQLite connection (`StaticPool`), so a commit on
either side is visible to the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from uuid import UUID, uuid4

import pytest

from app.agents.penalties.rule_extraction.schema import PenaltyFact, PenaltyFactList, PenaltyRuleExtraction
from app.core.exceptions import ConflictError, NotFoundError
from app.models import AgentRun
from app.models.penalties import ExtractedPenaltyRuleRevision
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    ExtractedPenaltyRuleRevisionRepository,
)
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.services.penalties.rule_extraction.revision import (
    REVISION_STALE_AFTER,
    RuleRevisionService,
    run_rule_revision,
)

CLAUSE_TEXT = "Retailer may assess a $50 fee per short-shipped case."


def _classification(**overrides) -> PenaltyRuleExtraction:
    fields = {
        "is_penalty_rule": True,
        "penalty_category": "SHORT_SHIP",
        "calc_type": "PER_UNIT",
        "economic_effect_type": "CHARGEBACK",
        "po_shortage_flag": False,
        "po_delay_flag": False,
        "confidence": 0.85,
        "plain_explanation": "Vendor short-ships; retailer charges a flat fee per case.",
        "reviewer_reply": "Updated the rate to $75 per the reviewer's instruction.",
    }
    fields.update(overrides)
    return PenaltyRuleExtraction(**fields)


def _rate_fact(**overrides) -> PenaltyFact:
    fields = {
        "attribute_role": "RATE",
        "basis_type": "UNIT_COST",
        "value": 75.0,
        "value_status": "PRESENT",
        "source_text": "$75 fee per short-shipped case.",
        "confidence": 0.9,
    }
    fields.update(overrides)
    return PenaltyFact(**fields)


@pytest.fixture
def retailer_agreements(db_session) -> RetailerAgreementRepository:
    return RetailerAgreementRepository(db_session)


@pytest.fixture
def extracted_rules(db_session) -> ExtractedPenaltyRuleRepository:
    return ExtractedPenaltyRuleRepository(db_session)


@pytest.fixture
def revisions(db_session) -> ExtractedPenaltyRuleRevisionRepository:
    return ExtractedPenaltyRuleRevisionRepository(db_session)


@pytest.fixture
def agent_runs(db_session) -> AgentRunRepository:
    return AgentRunRepository(db_session)


@pytest.fixture
def service(retailer_agreements, extracted_rules, revisions, agent_runs, db_session) -> RuleRevisionService:
    return RuleRevisionService(
        retailer_agreements=retailer_agreements,
        extracted_rules=extracted_rules,
        revisions=revisions,
        agent_runs=agent_runs,
        session=db_session,
    )


def _make_retailer(repos, code: str = "WMT") -> UUID:
    return repos.master_data.add_retailer(code, "Walmart", None, "SUM")["id"]


def _make_retailer_agreement(
    retailer_agreements: RetailerAgreementRepository, retailer_id: UUID, sha256: str
) -> dict:
    return retailer_agreements.add_retailer_agreement(
        retailer_id=retailer_id,
        contract_code=f"CONTRACT-{sha256[:8]}",
        title="Example Retailer Agreement",
        document_sha256=sha256,
        markdown_text=f"## Penalties\n{CLAUSE_TEXT}",
    )


# Monotonically increasing `created_at` stamps, same reasoning as test_service.py's
# `_run_created_at_seq`: SQLite's `CURRENT_TIMESTAMP` server default has only
# second resolution, so back-to-back runs otherwise tie on `created_at`.
_run_created_at_seq = count()


def _make_agent_run(db_session, retailer_agreement_id: UUID, *, status: str = "completed") -> UUID:
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="penalty_rule_extractor",
        prompt_version="v1",
        system_prompt="Extract penalty clauses from retailer agreement markdown.",
        agent_name="Penalty Rule Extractor",
        domain="penalties",
    )
    agent_runs = AgentRunRepository(db_session)
    run_id = agent_runs.start(
        agent_id=agent_id,
        run_type="PENALTY_RULE_EXTRACTION",
        metadata={"retailer_agreement_id": str(retailer_agreement_id), "prompt_version": "v1"},
    )
    agent_runs.update_status(run_id, status, completed=status in ("completed", "failed"))
    db_session.get(AgentRun, run_id).created_at = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(
        seconds=next(_run_created_at_seq)
    )
    db_session.flush()
    return run_id


def _make_staged_rule(
    extracted_rules, retailer_agreement_id: UUID, agent_run_id: UUID, section: str = "Sec 4.2"
):
    return extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement_id,
        agent_run_id=agent_run_id,
        clause_text=CLAUSE_TEXT,
        clause_fingerprint="a" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        section=section,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 50.0,
                "value_unit": "USD",
                "value_status": "PRESENT",
                "basis_type": "UNIT_COST",
                "source_text": "$50 fee per short-shipped case.",
                "confidence": 0.9,
            }
        ],
    )


# ---------------------------------------------------------------------------
# RuleRevisionService.request_revision
# ---------------------------------------------------------------------------


def test_request_revision_happy_path_queues_a_revision_with_a_populated_before_snapshot(
    repos, retailer_agreements, extracted_rules, db_session, service
):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "d1" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])
    rule = _make_staged_rule(extracted_rules, retailer_agreement["id"], run_id)

    revision = service.request_revision(
        retailer_agreement["id"],
        rule.id,
        instruction="Recheck the rate.",
        requested_by="reviewer@example.com",
    )

    assert revision["status"] == "QUEUED"
    assert revision["revision_no"] == 1
    assert revision["before_snapshot"]["penalty_category"] == "SHORT_SHIP"
    assert revision["before_snapshot"]["attributes"][0]["attribute_role"] == "RATE"


def test_request_revision_raises_conflict_for_a_rule_from_a_superseded_run(
    repos, retailer_agreements, extracted_rules, db_session, service
):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "d2" * 32)
    older_run_id = _make_agent_run(db_session, retailer_agreement["id"])
    _make_agent_run(db_session, retailer_agreement["id"])
    older_rule = _make_staged_rule(extracted_rules, retailer_agreement["id"], older_run_id)

    with pytest.raises(ConflictError) as exc_info:
        service.request_revision(
            retailer_agreement["id"], older_rule.id, instruction="Recheck.", requested_by=None
        )
    assert exc_info.value.code == "RULE_NOT_IN_LATEST_RUN"


def test_request_revision_raises_conflict_while_a_revision_is_already_open(
    repos, retailer_agreements, extracted_rules, db_session, service
):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "d3" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])
    rule = _make_staged_rule(extracted_rules, retailer_agreement["id"], run_id)
    service.request_revision(retailer_agreement["id"], rule.id, instruction="First.", requested_by=None)

    with pytest.raises(ConflictError) as exc_info:
        service.request_revision(retailer_agreement["id"], rule.id, instruction="Second.", requested_by=None)
    assert exc_info.value.code == "RULE_REVISION_IN_PROGRESS"


def test_request_revision_allows_a_new_revision_once_the_open_one_goes_stale(
    repos, retailer_agreements, extracted_rules, revisions, db_session, service
):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "d4" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])
    rule = _make_staged_rule(extracted_rules, retailer_agreement["id"], run_id)
    first = service.request_revision(
        retailer_agreement["id"], rule.id, instruction="First.", requested_by=None
    )

    row = db_session.get(ExtractedPenaltyRuleRevision, first["id"])
    row.updated_at = datetime.now(UTC).replace(tzinfo=None) - REVISION_STALE_AFTER - timedelta(minutes=1)
    db_session.flush()

    second = service.request_revision(
        retailer_agreement["id"], rule.id, instruction="Second.", requested_by=None
    )

    assert second["revision_no"] == 2
    assert revisions.get(first["id"])["status"] == "FAILED"


def test_request_revision_raises_not_found_for_an_unknown_retailer_agreement(service):
    with pytest.raises(NotFoundError) as exc_info:
        service.request_revision(uuid4(), uuid4(), instruction="Recheck.", requested_by=None)
    assert exc_info.value.code == "RETAILER_AGREEMENT_NOT_FOUND"


def test_request_revision_raises_not_found_for_an_unknown_rule(repos, retailer_agreements, service):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "d5" * 32)

    with pytest.raises(NotFoundError) as exc_info:
        service.request_revision(retailer_agreement["id"], uuid4(), instruction="Recheck.", requested_by=None)
    assert exc_info.value.code == "EXTRACTED_RULE_NOT_FOUND"


# ---------------------------------------------------------------------------
# run_rule_revision
# ---------------------------------------------------------------------------


def test_run_rule_revision_success_replaces_the_rules_facts_and_completes_the_revision(
    repos, retailer_agreements, extracted_rules, revisions, db_session, service, database
):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "e1" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])
    rule = _make_staged_rule(extracted_rules, retailer_agreement["id"], run_id)
    extracted_rules.set_review_decision(rule.id, "APPROVED")
    revision = service.request_revision(
        retailer_agreement["id"],
        rule.id,
        instruction="Use $75, not $50.",
        requested_by="reviewer@example.com",
    )

    seen_contexts = {}

    def _classify(context):
        seen_contexts["classification"] = context
        return _classification()

    def _extract_facts(context):
        seen_contexts["facts"] = context
        return PenaltyFactList(facts=[_rate_fact()])

    run_rule_revision(revision["id"], database=database, classify=_classify, extract_facts=_extract_facts)

    # `run_rule_revision` commits through its own, separate sessions; `db_session` (used
    # by `extracted_rules`/`revisions` here) has `expire_on_commit=False` and already
    # holds these rows in its identity map from setup above, so it needs an explicit
    # expire to see the other sessions' committed writes, the same as a fresh request's
    # session naturally would.
    db_session.expire_all()

    updated = extracted_rules.get_with_attributes(rule.id)
    assert updated["status"] == "PENDING_REVIEW"
    assert len(updated["attributes"]) == 1
    assert float(updated["attributes"][0].value) == 75.0

    completed = revisions.get(revision["id"])
    assert completed["status"] == "COMPLETED"
    assert completed["agent_reply"] == "Updated the rate to $75 per the reviewer's instruction."
    assert completed["after_snapshot"]["attributes"][0]["value"] == 75.0

    classification_context = seen_contexts["classification"]
    assert classification_context.reviewer_instruction == "Use $75, not $50."
    assert classification_context.current_rule["penalty_category"] == "SHORT_SHIP"
    facts_context = seen_contexts["facts"]
    assert facts_context.reviewer_instruction == "Use $75, not $50."


def test_run_rule_revision_marks_the_revision_failed_when_classify_raises_and_leaves_the_rule_unchanged(
    repos, retailer_agreements, extracted_rules, revisions, db_session, service, database
):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "e2" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])
    rule = _make_staged_rule(extracted_rules, retailer_agreement["id"], run_id)
    revision = service.request_revision(
        retailer_agreement["id"], rule.id, instruction="Recheck.", requested_by=None
    )

    def _classify(context):
        raise RuntimeError("LLM boom")

    def _extract_facts(context):
        raise AssertionError("must not be called")

    run_rule_revision(revision["id"], database=database, classify=_classify, extract_facts=_extract_facts)
    db_session.expire_all()

    unchanged = extracted_rules.get_with_attributes(rule.id)
    assert unchanged["status"] == "PENDING_REVIEW"
    assert len(unchanged["attributes"]) == 1
    assert float(unchanged["attributes"][0].value) == 50.0

    failed = revisions.get(revision["id"])
    assert failed["status"] == "FAILED"
    assert "LLM boom" in failed["error"]


def test_run_rule_revision_is_a_no_op_for_a_non_queued_revision(
    repos, retailer_agreements, extracted_rules, revisions, db_session, service, database
):
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "e3" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])
    rule = _make_staged_rule(extracted_rules, retailer_agreement["id"], run_id)
    revision = service.request_revision(
        retailer_agreement["id"], rule.id, instruction="Recheck.", requested_by=None
    )
    revisions.mark_completed(revision["id"], agent_reply="Already done.", after_snapshot={"foo": "bar"})

    def _fail(*args, **kwargs):
        raise AssertionError("must not be called")

    run_rule_revision(revision["id"], database=database, classify=_fail, extract_facts=_fail)
    db_session.expire_all()

    unchanged = revisions.get(revision["id"])
    assert unchanged["status"] == "COMPLETED"
    assert unchanged["agent_reply"] == "Already done."
