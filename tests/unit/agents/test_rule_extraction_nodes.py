"""Tests for `RuleExtractionNodes` (app.agents.penalties.rule_extraction.nodes).

Pure nodes (`split_document`, `resolve_candidates`, `process_clause` and both routing
functions) are exercised with a `FakeLLM` and no database at all. The three nodes that
touch Postgres (`stage_rules`, `human_review`, `apply_decisions`) run against the shared
in-memory SQLite `database` fixture, each opening its own scoped session exactly as
production code does; setup writes are committed explicitly before the node under test
opens its own session, mirroring `tests/unit/workers/test_worker_penalty_mitigation.py`.
"""

from __future__ import annotations

from uuid import UUID

from langgraph.types import Send
from sqlalchemy import select

from app.agents.penalties.rule_extraction.nodes import RuleExtractionNodes
from app.agents.penalties.rule_extraction.schema import (
    CandidateClause,
    CandidateClauseList,
    PenaltyFact,
    PenaltyFactList,
    PenaltyRuleExtraction,
)
from app.db.session import Database
from app.models import ExtractedPenaltyRuleAttribute, ProcessingError
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import ExtractedPenaltyRuleRepository
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository

CONTRACT_TEXT = "Retailer may assess a $50 fee per short-shipped case."


class FakeLLM:
    """Test double for `RuleExtractionLLM`: returns or raises exactly what the test wants."""

    def __init__(
        self,
        *,
        screen_result: CandidateClauseList | None = None,
        screen_exc: Exception | None = None,
        classify_result: PenaltyRuleExtraction | None = None,
        classify_exc: Exception | None = None,
        facts_result: PenaltyFactList | None = None,
        facts_exc: Exception | None = None,
    ) -> None:
        self._screen_result = screen_result
        self._screen_exc = screen_exc
        self._classify_result = classify_result
        self._classify_exc = classify_exc
        self._facts_result = facts_result
        self._facts_exc = facts_exc

    def screen(self, context):
        if self._screen_exc is not None:
            raise self._screen_exc
        return self._screen_result

    def classify(self, context):
        if self._classify_exc is not None:
            raise self._classify_exc
        return self._classify_result

    def extract_facts(self, context):
        if self._facts_exc is not None:
            raise self._facts_exc
        return self._facts_result


def _nodes(llm: FakeLLM, database: Database) -> RuleExtractionNodes:
    return RuleExtractionNodes(
        screen=llm.screen, classify=llm.classify, extract_facts=llm.extract_facts, database=database
    )


def _make_retailer(db_session) -> UUID:
    from app.repositories.common.master_data import MasterDataRepository

    return MasterDataRepository(db_session).add_retailer("WMT", "Walmart", None, "SUM")["id"]


def _make_contract(db_session, retailer_id: UUID, sha256: str) -> UUID:
    contract = RetailerAgreementRepository(db_session).add_retailer_agreement(
        retailer_id=retailer_id,
        contract_code=f"C-{sha256[:8]}",
        title="Example Retailer Agreement",
        document_sha256=sha256,
        markdown_text=CONTRACT_TEXT,
    )
    return contract["id"]


def _make_agent_run(db_session) -> UUID:
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_rule_extraction",
        prompt_version="v1",
        system_prompt="Extract penalty clauses from contract markdown.",
        agent_name="Test Rule Extraction",
        domain="penalties",
    )
    return AgentRunRepository(db_session).start(agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION")


# ---------------------------------------------------------------------------
# split_document / route_after_split_document
# ---------------------------------------------------------------------------


def test_split_document_produces_one_screening_unit_per_section(database):
    nodes = _nodes(FakeLLM(), database)

    result = nodes.split_document({"contract_text": "## A\nClause one.\n\n## B\nClause two."})

    assert len(result["screening_units"]) == 2
    assert result["screening_units"][0]["section_path"] == "A"
    assert result["screening_units"][1]["section_path"] == "B"


def test_route_after_split_document_sends_one_screen_unit_per_unit(database):
    nodes = _nodes(FakeLLM(), database)
    units = [{"index": 0, "section_path": "A", "text": "x", "start_offset": 0, "end_offset": 1}]

    sends = nodes.route_after_split_document({"screening_units": units})

    assert [type(s) for s in sends] == [Send]
    assert sends[0].node == "screen_unit"
    assert sends[0].arg == {"unit": units[0]}


# ---------------------------------------------------------------------------
# screen_unit
# ---------------------------------------------------------------------------


def _unit(text: str = "Late delivery incurs a $50 fee.") -> dict:
    return {"index": 0, "section_path": "Delivery", "text": text, "start_offset": 0, "end_offset": len(text)}


def test_screen_unit_returns_screened_candidates_carrying_the_source_unit(database):
    unit = _unit()
    candidate = CandidateClause(
        section_title="Delivery", excerpt=unit["text"], reason="late delivery leads to a fee"
    )
    llm = FakeLLM(screen_result=CandidateClauseList(clauses=[candidate]))
    nodes = _nodes(llm, database)

    result = nodes.screen_unit({"unit": unit})

    assert result == {
        "screened_candidates": [
            {
                "section_title": "Delivery",
                "excerpt": unit["text"],
                "reason": "late delivery leads to a fee",
                "unit": unit,
            }
        ]
    }


def test_screen_unit_failure_is_recorded_as_an_extraction_error_not_raised(database):
    llm = FakeLLM(screen_exc=RuntimeError("boom"))
    nodes = _nodes(llm, database)

    result = nodes.screen_unit({"unit": _unit()})

    assert "screened_candidates" not in result
    [error] = result["extraction_errors"]
    assert error["error_code"] == "RULE_EXTRACTION_SCREENING_FAILED"
    assert "boom" in error["message"]


# ---------------------------------------------------------------------------
# resolve_candidates / route_after_resolve_candidates
# ---------------------------------------------------------------------------


def test_resolve_candidates_matches_the_excerpt_against_the_source_text(database):
    unit = _unit()
    candidate = {
        "section_title": "Delivery",
        "excerpt": unit["text"],
        "reason": "late delivery leads to a fee",
        "unit": unit,
    }
    nodes = _nodes(FakeLLM(), database)

    result = nodes.resolve_candidates({"contract_text": unit["text"], "screened_candidates": [candidate]})

    assert result["candidate_clauses"] == [
        {
            "section_title": "Delivery",
            "clause_text": unit["text"],
            "match_kind": "EXACT",
            "reason": "late delivery leads to a fee",
        }
    ]


def test_resolve_candidates_returns_an_empty_list_when_nothing_was_screened(database):
    nodes = _nodes(FakeLLM(), database)

    result = nodes.resolve_candidates({"contract_text": "irrelevant", "screened_candidates": []})

    assert result["candidate_clauses"] == []


def test_route_after_resolve_candidates_goes_straight_to_stage_rules_when_empty(database):
    nodes = _nodes(FakeLLM(), database)

    assert nodes.route_after_resolve_candidates({"candidate_clauses": []}) == "stage_rules"


def test_route_after_resolve_candidates_sends_one_process_clause_per_clause(database):
    nodes = _nodes(FakeLLM(), database)
    clauses = [{"clause_text": "a"}, {"clause_text": "b"}]

    sends = nodes.route_after_resolve_candidates({"candidate_clauses": clauses})

    assert [s.node for s in sends] == ["process_clause", "process_clause"]
    assert [s.arg for s in sends] == [{"clause": c} for c in clauses]


# ---------------------------------------------------------------------------
# process_clause
# ---------------------------------------------------------------------------


def _classification(**overrides) -> PenaltyRuleExtraction:
    fields = {
        "is_penalty_rule": True,
        "penalty_category": "SHORT_SHIP",
        "calc_type": "PER_UNIT",
        "economic_effect_type": "CHARGEBACK",
        "po_shortage_flag": False,
        "po_delay_flag": False,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return PenaltyRuleExtraction(**fields)


def _rate_fact(**overrides) -> PenaltyFact:
    fields = {
        "attribute_role": "RATE",
        "basis_type": "UNIT_COST",
        "value": 50.0,
        "value_status": "PRESENT",
        "source_text": "$50 fee per short-shipped case.",
        "confidence": 0.9,
    }
    fields.update(overrides)
    return PenaltyFact(**fields)


def test_process_clause_builds_a_ready_draft_and_floors_the_po_scope_flags(database):
    # SHORT_SHIP's governed category default is po_shortage_flag=True, so the floor
    # must raise the classifier's False even though the classifier itself said False.
    llm = FakeLLM(
        classify_result=_classification(po_shortage_flag=False),
        facts_result=PenaltyFactList(facts=[_rate_fact()]),
    )
    nodes = _nodes(llm, database)

    result = nodes.process_clause({"clause": {"clause_text": CONTRACT_TEXT, "section_title": "Shortages"}})

    [draft] = result["drafts"]
    assert draft["master"]["penalty_category"] == "SHORT_SHIP"
    assert draft["master"]["po_shortage_flag"] is True
    assert draft["pricing_readiness"] == "READY"
    assert draft["attributes"][0]["branch_no"] == 0
    assert draft["attributes"][0]["group_no"] == 0


def test_process_clause_skips_fact_extraction_when_not_a_penalty_rule(database):
    classification = _classification(
        is_penalty_rule=False,
        penalty_category="UNMAPPED",
        calc_type="UNSPECIFIED",
        economic_effect_type="OTHER",
        review_notes="just a definitions clause",
    )
    llm = FakeLLM(
        classify_result=classification, facts_exc=AssertionError("extract_facts must not be called")
    )
    nodes = _nodes(llm, database)

    result = nodes.process_clause({"clause": {"clause_text": "Definitions.", "section_title": None}})

    [draft] = result["drafts"]
    assert draft["attributes"] == []
    assert draft["pricing_readiness"] == "UNSUPPORTED_SHAPE"


def test_process_clause_classification_failure_is_recorded_as_an_extraction_error(database):
    llm = FakeLLM(classify_exc=RuntimeError("boom"))
    nodes = _nodes(llm, database)

    result = nodes.process_clause({"clause": {"clause_text": "x", "section_title": None}})

    assert "drafts" not in result
    [error] = result["extraction_errors"]
    assert error["error_code"] == "RULE_EXTRACTION_CLASSIFICATION_FAILED"


def test_process_clause_fact_extraction_failure_is_recorded_as_an_extraction_error(database):
    llm = FakeLLM(classify_result=_classification(), facts_exc=RuntimeError("boom"))
    nodes = _nodes(llm, database)

    result = nodes.process_clause({"clause": {"clause_text": "x", "section_title": None}})

    assert "drafts" not in result
    [error] = result["extraction_errors"]
    assert error["error_code"] == "RULE_EXTRACTION_FACT_EXTRACTION_FAILED"


# ---------------------------------------------------------------------------
# stage_rules
# ---------------------------------------------------------------------------


def test_stage_rules_persists_a_draft_with_branch_no_zero_and_flushes_extraction_errors(database, db_session):
    retailer_id = _make_retailer(db_session)
    contract_id = _make_contract(db_session, retailer_id, "1" * 64)
    run_id = _make_agent_run(db_session)
    db_session.commit()

    draft = {
        "clause_text": CONTRACT_TEXT,
        "section_title": "Shortages",
        "master": {
            "penalty_category": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "confidence": 0.9,
            "po_shortage_flag": True,
            "po_delay_flag": False,
            "review_notes": None,
            "economic_effect_type": "CHARGEBACK",
        },
        "attributes": [
            {
                "branch_no": 0,
                "group_no": 0,
                "attribute_role": "RATE",
                "basis_type": "UNIT_COST",
                "value": 50.0,
                "value_status": "PRESENT",
                "source_text": "$50 fee per short-shipped case.",
                "confidence": 0.9,
            }
        ],
        "issues": [],
        "readiness_notes": [],
        "pricing_readiness": "READY",
    }
    error = {
        "error_code": "RULE_EXTRACTION_SCREENING_FAILED",
        "message": "boom",
        "node_name": "screen_unit",
        "detail": {"unit_index": 0},
    }
    nodes = _nodes(FakeLLM(), database)

    result = nodes.stage_rules(
        {"contract_id": contract_id, "run_id": run_id, "drafts": [draft], "extraction_errors": [error]}
    )

    assert len(result["staged_rule_ids"]) == 1

    with database.session() as session:
        staged = ExtractedPenaltyRuleRepository(session).list_for_contract(contract_id)
        assert len(staged) == 1
        assert staged[0].status == "PENDING_REVIEW"

        [attribute] = session.scalars(select(ExtractedPenaltyRuleAttribute)).all()
        assert attribute.branch_no == 0

        errors = session.scalars(select(ProcessingError)).all()
        assert len(errors) == 1
        assert errors[0].error_type == "RULE_EXTRACTION_SCREENING_FAILED"


# ---------------------------------------------------------------------------
# human_review
# ---------------------------------------------------------------------------


def test_human_review_falls_through_without_interrupting_when_queue_is_empty(database, db_session):
    retailer_id = _make_retailer(db_session)
    contract_id = _make_contract(db_session, retailer_id, "2" * 64)
    run_id = _make_agent_run(db_session)
    db_session.commit()

    nodes = _nodes(FakeLLM(), database)

    result = nodes.human_review({"contract_id": contract_id, "run_id": run_id})

    assert result == {}


# ---------------------------------------------------------------------------
# apply_decisions
# ---------------------------------------------------------------------------


def test_apply_decisions_ignores_rows_still_pending_review(database, db_session):
    retailer_id = _make_retailer(db_session)
    contract_id = _make_contract(db_session, retailer_id, "3" * 64)
    run_id = _make_agent_run(db_session)
    ExtractedPenaltyRuleRepository(db_session).add_extracted_rule(
        contract_id=contract_id,
        agent_run_id=run_id,
        clause_text=CONTRACT_TEXT,
        clause_fingerprint="a" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
    )
    db_session.commit()

    nodes = _nodes(FakeLLM(), database)
    result = nodes.apply_decisions({"contract_id": contract_id, "run_id": run_id})

    assert result["applied_rule_ids"] == []


def test_apply_decisions_returns_ids_of_rows_with_a_real_verdict(database, db_session):
    retailer_id = _make_retailer(db_session)
    contract_id = _make_contract(db_session, retailer_id, "4" * 64)
    run_id = _make_agent_run(db_session)
    repo = ExtractedPenaltyRuleRepository(db_session)
    rule = repo.add_extracted_rule(
        contract_id=contract_id,
        agent_run_id=run_id,
        clause_text=CONTRACT_TEXT,
        clause_fingerprint="b" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
    )
    repo.set_review_decision(rule.id, "APPROVED")
    db_session.commit()

    nodes = _nodes(FakeLLM(), database)
    result = nodes.apply_decisions({"contract_id": contract_id, "run_id": run_id})

    assert result["applied_rule_ids"] == [str(rule.id)]
