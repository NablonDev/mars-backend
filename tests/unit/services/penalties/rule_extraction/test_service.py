"""Tests for `PenaltyRuleExtractionService`
(app.services.penalties.rule_extraction.service).

The graph is a fake with an `.invoke()` (`_FakeGraph`/`_FakeInterruptGraph` below); every
repository is real, running against the shared in-memory SQLite session. No LLM call is
ever reached.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.types import Command

from app.core.exceptions import ConflictError, ValidationError
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    RulePublicationRepository,
)
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.services.penalties.rule_extraction.service import PenaltyRuleExtractionService

CONTRACT_TEXT = "## Penalties\nRetailer may assess a $50 fee per short-shipped case."
EFFECTIVE_DATE = date(2026, 1, 1)


@pytest.fixture
def contracts(db_session) -> RetailerAgreementRepository:
    return RetailerAgreementRepository(db_session)


@pytest.fixture
def extracted_rules(db_session) -> ExtractedPenaltyRuleRepository:
    return ExtractedPenaltyRuleRepository(db_session)


@pytest.fixture
def publications(db_session) -> RulePublicationRepository:
    return RulePublicationRepository(db_session)


class _FakeGraph:
    """Double for the LangGraph extraction graph: records every call, optionally raises.

    Never interrupts: `invoke` echoes the state it's handed straight back, taking the
    touchless "nothing needed review" path through `_handle_graph_state`.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append({"state": state, "config": config})
        if self.exc is not None:
            raise self.exc
        return state


class _FakeInterruptGraph:
    """Double for the extraction graph: interrupts at `human_review` on the first
    invoke, then completes on any `Command(resume=...)` invoke, regardless of payload.
    """

    def __init__(self, *, pending_count: int = 1) -> None:
        self.pending_count = pending_count
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, arg: Any, config: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append({"arg": arg, "config": config})
        if isinstance(arg, Command):
            return {"applied_rule_ids": []}
        return {
            "__interrupt__": [
                SimpleNamespace(
                    value={
                        "reason": "rule_review_required",
                        "run_id": str(arg["run_id"]),
                        "contract_id": str(arg["contract_id"]),
                        "pending_count": self.pending_count,
                        "extracted_rule_ids": [],
                        "instructions": "Review then resume.",
                    }
                )
            ]
        }


@pytest.fixture
def service_factory(repos, contracts, extracted_rules, publications, db_session):
    """Build a `PenaltyRuleExtractionService` sharing the test session's repositories."""

    def _build(graph: Any = None) -> PenaltyRuleExtractionService:
        return PenaltyRuleExtractionService(
            contracts=contracts,
            extracted_rules=extracted_rules,
            publications=publications,
            rules=repos.penalty_rules,
            agent_registry=repos.agent_registry,
            agent_runs=repos.agent_runs,
            master_data=repos.master_data,
            workflow_threads=repos.workflow_threads,
            human_actions=repos.human_actions,
            session=db_session,
            graph=graph,
        )

    return _build


def _make_retailer(repos, code: str = "WMT") -> UUID:
    return repos.master_data.add_retailer(code, "Walmart", None, "SUM")["id"]


def _make_contract(
    contracts: RetailerAgreementRepository, retailer_id: UUID, sha256: str, **overrides
) -> dict:
    fields: dict[str, Any] = {
        "retailer_id": retailer_id,
        "contract_code": f"CONTRACT-{sha256[:8]}",
        "title": "Example Retailer Agreement",
        "document_sha256": sha256,
        "markdown_text": CONTRACT_TEXT,
        "effective_date": EFFECTIVE_DATE,
    }
    fields.update(overrides)
    return contracts.add_retailer_agreement(**fields)


def _make_agent_run(db_session) -> UUID:
    """Register the extractor agent and open one run against it, bypassing the service."""
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="penalty_rule_extractor",
        prompt_version="v1",
        system_prompt="Extract penalty clauses from contract markdown.",
        agent_name="Penalty Rule Extractor",
        domain="penalties",
    )
    return AgentRunRepository(db_session).start(agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION")


# ---------------------------------------------------------------------------
# create_contract
# ---------------------------------------------------------------------------


def test_create_contract_idempotent_returns_existing_row_for_same_markdown_text(repos, service_factory):
    service = service_factory()
    retailer_id = _make_retailer(repos)

    first = service.create_contract(retailer_id, "CONTRACT-1", "Title", CONTRACT_TEXT)
    second = service.create_contract(retailer_id, "CONTRACT-1-DUPLICATE", "Title", CONTRACT_TEXT)

    assert second["id"] == first["id"]
    assert second["contract_code"] == "CONTRACT-1"


def test_create_contract_creates_a_new_row_for_different_markdown_text(repos, service_factory):
    service = service_factory()
    retailer_id = _make_retailer(repos)

    first = service.create_contract(retailer_id, "CONTRACT-1", "Title", CONTRACT_TEXT)
    second = service.create_contract(retailer_id, "CONTRACT-2", "Title", CONTRACT_TEXT + " Amended.")

    assert second["id"] != first["id"]


# ---------------------------------------------------------------------------
# start_extraction
# ---------------------------------------------------------------------------


def test_start_extraction_raises_when_contract_has_no_markdown_text(repos, contracts, service_factory):
    service = service_factory(graph=_FakeGraph())
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "1" * 64, markdown_text=None)

    with pytest.raises(ValidationError) as exc_info:
        service.start_extraction(contract["id"])
    assert exc_info.value.code == "CONTRACT_HAS_NO_TEXT"


def test_start_extraction_raises_when_no_graph_is_wired(repos, contracts, service_factory):
    service = service_factory(graph=None)
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "2" * 64)

    with pytest.raises(ConflictError) as exc_info:
        service.start_extraction(contract["id"])
    assert exc_info.value.code == "EXTRACTION_GRAPH_UNAVAILABLE"


def test_start_extraction_marks_the_agent_run_failed_and_reraises_when_the_graph_raises(
    repos, contracts, service_factory
):
    boom = RuntimeError("graph exploded")
    graph = _FakeGraph(exc=boom)
    service = service_factory(graph=graph)
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "3" * 64)

    with pytest.raises(RuntimeError, match="graph exploded"):
        service.start_extraction(contract["id"])

    run_id = graph.invocations[0]["state"]["run_id"]
    run = repos.agent_runs.get(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["error"] == "graph exploded"


def test_start_extraction_mints_a_fresh_checkpoint_thread_id_per_run(repos, contracts, service_factory):
    """A new run over the same contract gets its own LangGraph checkpoint thread, same as
    a new email or PO line does in the CMIR/PO-validation domains: no determinism is
    preserved across separate `start_extraction` calls."""
    graph = _FakeGraph()
    service = service_factory(graph=graph)
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "4" * 64)

    service.start_extraction(contract["id"])
    service.start_extraction(contract["id"])

    first_thread_id = graph.invocations[0]["config"]["configurable"]["thread_id"]
    second_thread_id = graph.invocations[1]["config"]["configurable"]["thread_id"]
    assert first_thread_id != second_thread_id


def test_start_extraction_creates_a_review_thread_when_the_graph_interrupts(
    repos, contracts, service_factory
):
    service = service_factory(graph=_FakeInterruptGraph())
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "a" * 64)

    result = service.start_extraction(contract["id"])

    assert result.thread_id is not None
    stage = repos.workflow_threads.get_stage(result.thread_id)
    assert stage["status"] == "waiting_rule_review"
    assert stage["stage"] == "AWAITING_RULE_REVIEW"
    assert stage["metadata_json"]["contract_id"] == str(contract["id"])
    assert stage["metadata_json"]["agent_run_id"] == str(result.agent_run_id)

    pending = repos.human_actions.get_open_for_thread(result.thread_id)
    assert pending is not None
    assert pending["interrupt_type"] == "rule_review_required"


# ---------------------------------------------------------------------------
# resume_review
# ---------------------------------------------------------------------------


def _start_awaiting_review(repos, contracts, service_factory, sha256: str):
    """Start extraction against a fresh contract on a graph that always interrupts once."""
    graph = _FakeInterruptGraph()
    service = service_factory(graph=graph)
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, sha256)

    result = service.start_extraction(contract["id"])
    stage = repos.workflow_threads.get_stage(result.thread_id)
    return service, graph, contract, result, stage


def test_resume_review_with_verdicts_completes_and_reaches_end(
    repos, contracts, extracted_rules, service_factory
):
    service, graph, contract, result, stage = _start_awaiting_review(
        repos, contracts, service_factory, "b" * 64
    )
    staged = extracted_rules.add_extracted_rule(
        contract_id=contract["id"],
        agent_run_id=result.agent_run_id,
        clause_text="Retailer may assess a $50 fee per short-shipped case.",
        clause_fingerprint="d" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    resumed = service.resume_review(
        result.thread_id, actor="reviewer@example.com", expected_updated_at=stage["updated_at"].isoformat()
    )

    assert resumed["status"] == "completed"
    assert resumed["stage"] == "RULE_REVIEW_COMPLETE"
    resume_call = graph.invocations[-1]["arg"]
    assert isinstance(resume_call, Command)
    assert resume_call.resume == {"decided_count": 1}


def test_resume_review_with_zero_decidable_rows_still_completes_without_reraising(
    repos, contracts, service_factory
):
    service, graph, _contract, result, stage = _start_awaiting_review(
        repos, contracts, service_factory, "c" * 64
    )

    resumed = service.resume_review(
        result.thread_id, actor="reviewer@example.com", expected_updated_at=stage["updated_at"].isoformat()
    )

    assert resumed["status"] == "completed"
    resume_call = graph.invocations[-1]["arg"]
    assert isinstance(resume_call, Command)
    # A falsy resume value makes LangGraph re-raise the interrupt forever; the resume
    # payload must always be truthy, even with nothing decided.
    assert resume_call.resume
    assert resume_call.resume == {"__ack__": "NO_DECISIONS"}


def test_resume_review_raises_conflict_when_expected_updated_at_is_stale(repos, contracts, service_factory):
    service, _graph, _contract, result, _stage = _start_awaiting_review(
        repos, contracts, service_factory, "e" * 64
    )

    with pytest.raises(ConflictError) as exc_info:
        service.resume_review(result.thread_id, actor="reviewer@example.com", expected_updated_at="stale")
    assert exc_info.value.code == "THREAD_STALE"


def test_resume_review_raises_when_thread_has_no_open_pending_action(repos, contracts, service_factory):
    service, _graph, _contract, result, stage = _start_awaiting_review(
        repos, contracts, service_factory, "f" * 64
    )
    pending = repos.human_actions.get_open_for_thread(result.thread_id)
    repos.human_actions.complete(pending["id"], response_payload={}, actor="someone-else")

    with pytest.raises(ConflictError) as exc_info:
        service.resume_review(
            result.thread_id,
            actor="reviewer@example.com",
            expected_updated_at=stage["updated_at"].isoformat(),
        )
    assert exc_info.value.code == "THREAD_NOT_WAITING"


# ---------------------------------------------------------------------------
# submit_review
# ---------------------------------------------------------------------------


def test_submit_review_rejects_a_status_outside_approved_or_rejected(repos, contracts, service_factory):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "5" * 64)

    with pytest.raises(ValidationError):
        service.submit_review(contract["id"], uuid4(), "PENDING_REVIEW")


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def test_publish_writes_a_penalty_rule_and_a_published_publication_for_an_accepted_rule(
    repos, contracts, extracted_rules, publications, db_session, service_factory
):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "6" * 64)
    run_id = _make_agent_run(db_session)

    staged = extracted_rules.add_extracted_rule(
        contract_id=contract["id"],
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
                "basis_type": "UNIT_COST",
                "currency_code": "USD",
                "source_text": "$50 fee per short-shipped case.",
                "confidence": 0.9,
            }
        ],
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    result = service.publish(contract["id"])

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
    repos, contracts, extracted_rules, publications, db_session, service_factory
):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "7" * 64)
    run_id = _make_agent_run(db_session)

    # pricing_readiness=AWAITING_DATA fails the publisher's admission gate (NOT_READY),
    # so nothing should ever reach penalty_rule.
    staged = extracted_rules.add_extracted_rule(
        contract_id=contract["id"],
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

    result = service.publish(contract["id"])

    assert result.published_count == 0
    assert result.rejected_count == 1
    assert result.outcomes[0].outcome == "REJECTED"
    assert result.outcomes[0].reason_code == "NOT_READY"
    assert result.outcomes[0].penalty_rule_id is None
    assert repos.penalty_rules.list_rules(retailer_id=retailer_id) == []


def test_publish_result_carries_the_run_id_it_published_from(
    repos, contracts, extracted_rules, db_session, service_factory
):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "8" * 64)
    run_id = _make_agent_run(db_session)

    staged = extracted_rules.add_extracted_rule(
        contract_id=contract["id"],
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
                "basis_type": "UNIT_COST",
                "currency_code": "USD",
                "source_text": "$50 fee per short-shipped case.",
                "confidence": 0.9,
            }
        ],
    )
    extracted_rules.set_review_decision(staged.id, "APPROVED")

    result = service.publish(contract["id"])

    assert result.agent_run_id == run_id


def test_publish_carries_basis_type_applies_per_and_currency_into_penalty_rule(
    repos, contracts, extracted_rules, db_session, service_factory
):
    # Crosses the publisher-to-repository seam end to end: a per-day percentage charge
    # must reach `penalty_rule` with its basis, accrual, and currency all populated,
    # not silently dropped at the insert.
    service = service_factory()
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "d1" * 32)
    run_id = _make_agent_run(db_session)

    staged = extracted_rules.add_extracted_rule(
        contract_id=contract["id"],
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

    result = service.publish(contract["id"])

    assert result.published_count == 1
    rule = repos.penalty_rules.list_rules(retailer_id=retailer_id)[0]
    assert rule["basis_type"] == "SHORTFALL_VALUE"
    assert rule["applies_per"] == "DAY"
    assert rule["currency_code"] == "EUR"
    assert "extracted_rule_id" not in rule


def test_publish_raises_when_the_contract_has_no_extracted_rules_at_all(repos, contracts, service_factory):
    service = service_factory()
    retailer_id = _make_retailer(repos)
    contract = _make_contract(contracts, retailer_id, "9" * 64)

    with pytest.raises(ValidationError) as exc_info:
        service.publish(contract["id"])
    assert exc_info.value.code == "NO_EXTRACTION_RUN"
