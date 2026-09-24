"""Tests for `PenaltyRuleExtractionService`
(app.services.penalties.rule_extraction.service).

Only `start_extraction` and `publish` remain on this service -- extraction status,
extraction runs, extracted-rule reads, and review moved to mars-bff (see `docs/API.md`).

The graph is a fake with an `.invoke()` (`_FakeGraph` below); every repository is real,
running against the shared in-memory SQLite session. No LLM call is ever reached.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import count
from typing import Any
from uuid import UUID

import pytest

from app.core.exceptions import ConflictError, ValidationError
from app.models import AgentRun
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    ExtractedPenaltyRuleRevisionRepository,
    RulePublicationRepository,
)
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.services.penalties.rule_extraction.service import PenaltyRuleExtractionService

RETAILER_AGREEMENT_TEXT = "## Penalties\nRetailer may assess a $50 fee per short-shipped case."
EFFECTIVE_DATE = date(2026, 1, 1)


@pytest.fixture
def retailer_agreements(db_session) -> RetailerAgreementRepository:
    return RetailerAgreementRepository(db_session)


@pytest.fixture
def extracted_rules(db_session) -> ExtractedPenaltyRuleRepository:
    return ExtractedPenaltyRuleRepository(db_session)


@pytest.fixture
def publications(db_session) -> RulePublicationRepository:
    return RulePublicationRepository(db_session)


@pytest.fixture
def revisions(db_session) -> ExtractedPenaltyRuleRevisionRepository:
    return ExtractedPenaltyRuleRevisionRepository(db_session)


class _FakeGraph:
    """Double for the LangGraph extraction graph: records every call, optionally raises.

    `invoke` echoes the state it's handed straight back: the graph ends at `stage_rules`
    and never interrupts, so `start_extraction` only reads the run's own status and the
    staged rules from the database, never anything out of this return value.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append({"state": state, "config": config})
        if self.exc is not None:
            raise self.exc
        return state


@pytest.fixture
def service_factory(repos, retailer_agreements, extracted_rules, publications, revisions, db_session):
    """Build a `PenaltyRuleExtractionService` sharing the test session's repositories."""

    def _build(graph: Any = None) -> PenaltyRuleExtractionService:
        return PenaltyRuleExtractionService(
            retailer_agreements=retailer_agreements,
            extracted_rules=extracted_rules,
            publications=publications,
            rules=repos.penalty_rules,
            agent_registry=repos.agent_registry,
            agent_runs=repos.agent_runs,
            master_data=repos.master_data,
            revisions=revisions,
            session=db_session,
            graph=graph,
        )

    return _build


def _make_retailer(repos, code: str = "WMT") -> UUID:
    return repos.master_data.add_retailer(code, "Walmart", None, "SUM")["id"]


def _make_retailer_agreement(
    retailer_agreements: RetailerAgreementRepository, retailer_id: UUID, sha256: str, **overrides
) -> dict:
    fields: dict[str, Any] = {
        "retailer_id": retailer_id,
        "contract_code": f"CONTRACT-{sha256[:8]}",
        "title": "Example Retailer Agreement",
        "document_sha256": sha256,
        "markdown_text": RETAILER_AGREEMENT_TEXT,
        "effective_date": EFFECTIVE_DATE,
    }
    fields.update(overrides)
    return retailer_agreements.add_retailer_agreement(**fields)


# Monotonically increasing `created_at` stamps for `_make_agent_run` below. SQLite's
# `CURRENT_TIMESTAMP` server default has only second resolution, so runs created
# back-to-back within one test otherwise tie on `created_at` and make "newest run"
# ordering (`list_by_metadata`, `_latest_completed_run_id`) non-deterministic.
_run_created_at_seq = count()


def _make_agent_run(
    db_session, retailer_agreement_id: UUID, *, status: str = "completed", prompt_version: str = "v1"
) -> UUID:
    """Register the extractor agent and open one completed, metadata-tagged run, bypassing the service.

    `_latest_completed_run_id` keys off the run's `metadata_json["retailer_agreement_id"]`
    and `status`, so a run built without them is invisible to the "latest run" lookup
    `publish` uses.
    """
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="penalty_rule_extractor",
        prompt_version=prompt_version,
        system_prompt="Extract penalty clauses from retailer agreement markdown.",
        agent_name="Penalty Rule Extractor",
        domain="penalties",
    )
    agent_runs = AgentRunRepository(db_session)
    run_id = agent_runs.start(
        agent_id=agent_id,
        run_type="PENALTY_RULE_EXTRACTION",
        metadata={"retailer_agreement_id": str(retailer_agreement_id), "prompt_version": prompt_version},
    )
    if status != "running":
        agent_runs.update_status(run_id, status, completed=status in ("completed", "failed"))
    db_session.get(AgentRun, run_id).created_at = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(
        seconds=next(_run_created_at_seq)
    )
    db_session.flush()
    return run_id


# ---------------------------------------------------------------------------
# start_extraction
# ---------------------------------------------------------------------------


def test_start_extraction_raises_when_retailer_agreement_has_no_markdown_text(
    repos, retailer_agreements, service_factory
):
    service = service_factory(graph=_FakeGraph())
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(
        retailer_agreements, retailer_id, "1" * 64, markdown_text=None
    )

    with pytest.raises(ValidationError) as exc_info:
        service.start_extraction(retailer_agreement["id"])
    assert exc_info.value.code == "RETAILER_AGREEMENT_HAS_NO_TEXT"


def test_start_extraction_raises_when_no_graph_is_wired(repos, retailer_agreements, service_factory):
    service = service_factory(graph=None)
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "2" * 64)

    with pytest.raises(ConflictError) as exc_info:
        service.start_extraction(retailer_agreement["id"])
    assert exc_info.value.code == "EXTRACTION_GRAPH_UNAVAILABLE"


def test_start_extraction_marks_the_agent_run_failed_and_reraises_when_the_graph_raises(
    repos, retailer_agreements, service_factory
):
    boom = RuntimeError("graph exploded")
    graph = _FakeGraph(exc=boom)
    service = service_factory(graph=graph)
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "3" * 64)

    with pytest.raises(RuntimeError, match="graph exploded"):
        service.start_extraction(retailer_agreement["id"])

    run_id = graph.invocations[0]["state"]["run_id"]
    run = repos.agent_runs.get(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["error"] == "graph exploded"


def test_start_extraction_mints_a_fresh_checkpoint_thread_id_per_run(
    repos, retailer_agreements, service_factory
):
    """A new run over the same retailer_agreement gets its own LangGraph checkpoint thread, same as
    a new email or PO line does in the CMIR/PO-validation domains: no determinism is
    preserved across separate `start_extraction` calls."""
    graph = _FakeGraph()
    service = service_factory(graph=graph)
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "4" * 64)

    service.start_extraction(retailer_agreement["id"])
    service.start_extraction(retailer_agreement["id"])

    first_thread_id = graph.invocations[0]["config"]["configurable"]["thread_id"]
    second_thread_id = graph.invocations[1]["config"]["configurable"]["thread_id"]
    assert first_thread_id != second_thread_id


def test_start_extraction_marks_the_agent_run_completed_and_returns_the_staged_count(
    repos, retailer_agreements, service_factory
):
    service = service_factory(graph=_FakeGraph())
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "a" * 64)

    result = service.start_extraction(retailer_agreement["id"])

    run = repos.agent_runs.get(result.agent_run_id)
    assert run["status"] == "completed"
    assert run["completed_at"] is not None
    assert result.staged_count == 0


def test_start_extraction_staged_count_reflects_rows_the_graph_staged(
    repos, retailer_agreements, extracted_rules, service_factory
):
    """`_FakeGraph.invoke` never touches the database itself, so this stages a row directly
    (mirroring what `stage_rules` would have done) before invoking, to prove
    `start_extraction` counts it rather than trusting anything from the graph's return
    value."""
    graph = _FakeGraph()
    service = service_factory(graph=graph)
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "a1" * 32)

    original_invoke = graph.invoke

    def _invoke_and_stage(state, config):
        extracted_rules.add_extracted_rule(
            retailer_agreement_id=state["retailer_agreement_id"],
            agent_run_id=state["run_id"],
            clause_text="Retailer may assess a $50 fee per short-shipped case.",
            clause_fingerprint="a1" * 16,
            penalty_category="SHORT_SHIP",
            calc_type="PER_UNIT",
            pricing_readiness="READY",
            confidence=0.9,
        )
        return original_invoke(state, config)

    graph.invoke = _invoke_and_stage

    result = service.start_extraction(retailer_agreement["id"])

    assert result.staged_count == 1


def test_start_extraction_stores_the_retailer_agreement_id_and_prompt_version_on_the_run(
    repos, retailer_agreements, service_factory
):
    service = service_factory(graph=_FakeGraph())
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "a1" * 32)

    result = service.start_extraction(retailer_agreement["id"])

    run = repos.agent_runs.get(result.agent_run_id)
    assert run["metadata_json"]["retailer_agreement_id"] == str(retailer_agreement["id"])
    assert run["metadata_json"]["prompt_version"]


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def test_publish_writes_a_penalty_rule_and_a_published_publication_for_an_accepted_rule(
    repos, retailer_agreements, extracted_rules, publications, db_session, service_factory
):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "6" * 64)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])

    staged = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=run_id,
        clause_text="Retailer may assess a $50 fee per short-shipped case.",
        clause_fingerprint="a" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 50.0,
                "value_unit": "USD",
                "value_status": "PRESENT",
                "basis_type": "PO_VALUE",
                "currency_code": "USD",
                "source_text": "$50 fee per short-shipped case.",
                "confidence": 0.9,
            }
        ],
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    result = service.publish(retailer_agreement["id"])

    assert result.published_count == 1
    assert result.rejected_count == 0
    assert len(result.outcomes) == 1
    assert result.outcomes[0].outcome == "PUBLISHED"
    assert result.outcomes[0].penalty_rule_id is not None

    rules = repos.penalty_rules.list_rules(retailer_id=retailer_id)
    assert len(rules) == 1
    assert rules[0]["id"] == result.outcomes[0].penalty_rule_id
    assert rules[0]["calc_type"] == "PER_UNIT"


def test_publish_writes_only_a_rejected_publication_for_a_rule_the_publisher_rejects(
    repos, retailer_agreements, extracted_rules, publications, db_session, service_factory
):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "7" * 64)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])

    # pricing_readiness=AWAITING_DATA fails the publisher's admission gate (NOT_READY),
    # so nothing should ever reach penalty_rule.
    staged = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=run_id,
        clause_text="Retailer may assess an unquantified fee for late delivery.",
        clause_fingerprint="b" * 32,
        penalty_category="OTIF_LATE",
        calc_type="PER_UNIT",
        pricing_readiness="AWAITING_DATA",
        confidence=0.7,
        po_delay_flag=True,
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    result = service.publish(retailer_agreement["id"])

    assert result.published_count == 0
    assert result.rejected_count == 1
    assert result.outcomes[0].outcome == "REJECTED"
    assert result.outcomes[0].reason_code == "NOT_READY"
    assert result.outcomes[0].penalty_rule_id is None
    assert repos.penalty_rules.list_rules(retailer_id=retailer_id) == []


def test_publish_result_carries_the_run_id_it_published_from(
    repos, retailer_agreements, extracted_rules, db_session, service_factory
):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "8" * 64)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])

    staged = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=run_id,
        clause_text="Retailer may assess a $50 fee per short-shipped case.",
        clause_fingerprint="c" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 50.0,
                "value_unit": "USD",
                "value_status": "PRESENT",
                "basis_type": "PO_VALUE",
                "currency_code": "USD",
                "source_text": "$50 fee per short-shipped case.",
                "confidence": 0.9,
            }
        ],
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    result = service.publish(retailer_agreement["id"])

    assert result.agent_run_id == run_id


def test_publish_carries_basis_type_applies_per_and_currency_into_penalty_rule(
    repos, retailer_agreements, extracted_rules, db_session, service_factory
):
    # Crosses the publisher-to-repository seam end to end: a per-day percentage charge
    # must reach `penalty_rule` with its basis, accrual, and currency all populated,
    # not silently dropped at the insert.
    service = service_factory()
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "d1" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])

    staged = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=run_id,
        clause_text="Retailer may assess 4% of shortfall value per day of delay.",
        clause_fingerprint="d1" * 16,
        penalty_category="OTIF_LATE",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
        po_delay_flag=True,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 4.0,
                "value_unit": "PERCENT",
                "value_status": "PRESENT",
                "basis_type": "SHORTFALL_VALUE",
                "applies_per": "DAY",
                "currency_code": "EUR",
                "source_text": "4% of shortfall value per day of delay.",
                "confidence": 0.9,
            }
        ],
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    result = service.publish(retailer_agreement["id"])

    assert result.published_count == 1
    rule = repos.penalty_rules.list_rules(retailer_id=retailer_id)[0]
    assert rule["basis_type"] == "SHORTFALL_VALUE"
    assert rule["applies_per"] == "DAY"
    assert rule["currency_code"] == "EUR"
    assert rule["extracted_rule_id"] == staged.id


def test_publish_rejects_a_republish_of_an_already_published_rule_instead_of_raising(
    repos, retailer_agreements, extracted_rules, db_session, service_factory
):
    # rule_code is deterministic (retailer_code-penalty_category-fingerprint[:8]), so a
    # second publish run over the same approved rule would otherwise hit penalty_rule's
    # unique constraint as a raw IntegrityError instead of a clean rejection.
    service = service_factory()
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "d2" * 32)
    run_id = _make_agent_run(db_session, retailer_agreement["id"])

    staged = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=run_id,
        clause_text="Retailer may assess a $50 fee per short-shipped case.",
        clause_fingerprint="d2" * 16,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 50.0,
                "value_unit": "USD",
                "value_status": "PRESENT",
                "basis_type": "PO_VALUE",
                "currency_code": "USD",
                "source_text": "$50 fee per short-shipped case.",
                "confidence": 0.9,
            }
        ],
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    first = service.publish(retailer_agreement["id"])
    assert first.published_count == 1
    assert first.rejected_count == 0

    second = service.publish(retailer_agreement["id"])

    assert second.published_count == 0
    assert second.rejected_count == 1
    assert second.outcomes[0].outcome == "REJECTED"
    assert second.outcomes[0].reason_code == "ALREADY_PUBLISHED"
    assert second.outcomes[0].penalty_rule_id is None
    assert len(repos.penalty_rules.list_rules(retailer_id=retailer_id)) == 1


def test_publish_raises_when_the_retailer_agreement_has_no_extracted_rules_at_all(
    repos, retailer_agreements, service_factory
):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    retailer_agreement = _make_retailer_agreement(retailer_agreements, retailer_id, "9" * 64)

    with pytest.raises(ValidationError) as exc_info:
        service.publish(retailer_agreement["id"])
    assert exc_info.value.code == "NO_EXTRACTION_RUN"
