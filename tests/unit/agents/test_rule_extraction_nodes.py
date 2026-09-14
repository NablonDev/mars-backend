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

RETAILER_AGREEMENT_TEXT = "Retailer may assess a $50 fee per short-shipped case."


class FakeLLM:
    """Test double for `RuleExtractionLLM`: returns or raises exactly what the test wants."""

    def __init__(
        self,
        *,
        screen_result: CandidateClauseList | None = None,
        screen_exc: Exception | None = None,
        classify_result: PenaltyRuleExtraction | None = None,
        classify_results: list[PenaltyRuleExtraction] | None = None,
        classify_exc: Exception | None = None,
        facts_result: PenaltyFactList | None = None,
        facts_results: list[PenaltyFactList] | None = None,
        facts_exc: Exception | None = None,
    ) -> None:
        self._screen_result = screen_result
        self._screen_exc = screen_exc
        self._classify_result = classify_result
        self._classify_queue = list(classify_results) if classify_results is not None else None
        self._classify_exc = classify_exc
        self._facts_result = facts_result
        self._facts_queue = list(facts_results) if facts_results is not None else None
        self._facts_exc = facts_exc
        self.facts_call_count = 0

    def screen(self, context):
        if self._screen_exc is not None:
            raise self._screen_exc
        return self._screen_result

    def classify(self, context):
        if self._classify_exc is not None:
            raise self._classify_exc
        if self._classify_queue is not None:
            return self._classify_queue.pop(0)
        return self._classify_result

    def extract_facts(self, context):
        self.facts_call_count += 1
        if self._facts_exc is not None:
            raise self._facts_exc
        if self._facts_queue is not None:
            return self._facts_queue.pop(0)
        return self._facts_result


def _nodes(llm: FakeLLM, database: Database) -> RuleExtractionNodes:
    return RuleExtractionNodes(
        screen=llm.screen, classify=llm.classify, extract_facts=llm.extract_facts, database=database
    )


def _make_retailer(db_session) -> UUID:
    from app.repositories.common.master_data import MasterDataRepository

    return MasterDataRepository(db_session).add_retailer("WMT", "Walmart", None, "SUM")["id"]


def _make_retailer_agreement(db_session, retailer_id: UUID, sha256: str) -> UUID:
    retailer_agreement = RetailerAgreementRepository(db_session).add_retailer_agreement(
        retailer_id=retailer_id,
        contract_code=f"C-{sha256[:8]}",
        title="Example Retailer Agreement",
        document_sha256=sha256,
        markdown_text=RETAILER_AGREEMENT_TEXT,
    )
    return retailer_agreement["id"]


def _make_agent_run(db_session) -> UUID:
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_rule_extraction",
        prompt_version="v1",
        system_prompt="Extract penalty clauses from retailer agreement markdown.",
        agent_name="Test Rule Extraction",
        domain="penalties",
    )
    return AgentRunRepository(db_session).start(agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION")


# ---------------------------------------------------------------------------
# split_document / route_after_split_document
# ---------------------------------------------------------------------------


def test_split_document_produces_one_screening_unit_per_section(database):
    nodes = _nodes(FakeLLM(), database)

    result = nodes.split_document({"retailer_agreement_text": "## A\nClause one.\n\n## B\nClause two."})

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

    result = nodes.resolve_candidates(
        {"retailer_agreement_text": unit["text"], "screened_candidates": [candidate]}
    )

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

    result = nodes.resolve_candidates({"retailer_agreement_text": "irrelevant", "screened_candidates": []})

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


def _threshold_fact(**overrides) -> PenaltyFact:
    fields = {
        "attribute_role": "THRESHOLD",
        "operator": "GTE",
        "value": 95.0,
        "value_status": "PRESENT",
        "source_text": "95% fill rate",
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

    result = nodes.process_clause(
        {"clause": {"clause_text": RETAILER_AGREEMENT_TEXT, "section_title": "Shortages"}}
    )

    # The dominant, first-attempt-clean path: consistency_issues finds nothing, so the
    # retry loop must not call extract_facts a second time.
    assert llm.facts_call_count == 1
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


def test_process_clause_leaves_a_single_category_clause_completely_unsplit(database):
    # Same clause_text/fields as test_process_clause_builds_a_ready_draft_and_floors_the_po_scope_flags:
    # a single-category clause must take the exact same, unsplit path (one classify call,
    # one extract_facts call, one draft), never the multi-excerpt loop.
    llm = FakeLLM(
        classify_result=_classification(po_shortage_flag=False),
        facts_result=PenaltyFactList(facts=[_rate_fact()]),
    )
    nodes = _nodes(llm, database)

    result = nodes.process_clause(
        {"clause": {"clause_text": RETAILER_AGREEMENT_TEXT, "section_title": "Shortages"}}
    )

    assert len(result["drafts"]) == 1
    [draft] = result["drafts"]
    assert draft["clause_text"] == RETAILER_AGREEMENT_TEXT
    assert draft["master"]["penalty_category"] == "SHORT_SHIP"
    assert draft["master"]["po_shortage_flag"] is True
    assert draft["pricing_readiness"] == "READY"


def test_process_clause_splits_a_bundled_clause_into_three_independent_drafts(database):
    # GT-04 (`data/rule_extraction_ground_truth/contract_3.yaml`): one clause bundling
    # three distinct charge types (late delivery, shortage, early delivery) under lettered
    # sub-items must now yield three independent candidates, not one.
    clause_text = (
        "3.4. OTIF Penalties. In the event Vendor fails to meet the delivery requirements "
        "specified in the P.O., Purchaser shall automatically assess, and Vendor agrees to "
        "pay, the following chargebacks via invoice deduction:\n"
        "\n"
        "(a) Late Delivery: For any Products delivered after the confirmed P.O. delivery "
        "window, Vendor shall be subject to a late delivery penalty equal to four percent "
        "(4.0%) of the total Cost of Goods Sold (COGS) of the delayed Products for each "
        "calendar day the delivery is delayed, capped at a maximum of twenty percent (20.0%).\n"
        "\n"
        "(b) Shortages: For any Order where the unit volume delivered is less than ninety-five "
        "percent (95.0%) of the ordered unit volume, Purchaser shall assess a short-shipment "
        "penalty of seven percent (7.0%) on the total invoice value of the missing merchandise.\n"
        "\n"
        "(c) Early Delivery: Products delivered more than three (3) business days prior to the "
        "confirmed delivery window may be refused or subject to an early delivery warehousing "
        "fee of $150.00 per pallet per day.\n"
    )
    llm = FakeLLM(
        classify_results=[
            _classification(penalty_category="OTIF_LATE", po_delay_flag=True),
            _classification(penalty_category="SHORT_SHIP", po_shortage_flag=True),
            _classification(penalty_category="DELIVERY_WINDOW_VIOLATION", po_delay_flag=True),
        ],
        facts_results=[
            PenaltyFactList(facts=[_rate_fact(value=4.0, source_text="four percent (4.0%)")]),
            PenaltyFactList(facts=[_rate_fact(value=7.0, source_text="seven percent (7.0%)")]),
            PenaltyFactList(facts=[_rate_fact(value=150.0, source_text="$150.00 per pallet per day")]),
        ],
    )
    nodes = _nodes(llm, database)

    result = nodes.process_clause(
        {"clause": {"clause_text": clause_text, "section_title": "3.4 OTIF Penalties"}}
    )

    assert len(result["drafts"]) == 3
    categories = {draft["master"]["penalty_category"] for draft in result["drafts"]}
    assert categories == {"OTIF_LATE", "SHORT_SHIP", "DELIVERY_WINDOW_VIOLATION"}
    # Each draft's clause_text is its own distinct sub-excerpt, never the whole original
    # clause repeated, so the fingerprint each later stages under can never collide.
    clause_texts = [draft["clause_text"] for draft in result["drafts"]]
    assert len(set(clause_texts)) == 3
    assert all(text != clause_text for text in clause_texts)


def test_process_clause_retries_fact_extraction_until_consistency_issues_clear_then_stages(database):
    # A RATE beside a THRESHOLD with tier_application unset trips Rule 4; the 4th
    # attempt fixes it, so the candidate must still reach `drafts`, staged normally.
    unresolved = PenaltyFactList(facts=[_rate_fact(), _threshold_fact()])
    resolved = PenaltyFactList(facts=[_rate_fact(tier_application="NOT_APPLICABLE"), _threshold_fact()])
    llm = FakeLLM(
        classify_result=_classification(),
        facts_results=[unresolved, unresolved, unresolved, resolved],
    )
    nodes = _nodes(llm, database)

    result = nodes.process_clause(
        {"clause": {"clause_text": RETAILER_AGREEMENT_TEXT, "section_title": "Shortages"}}
    )

    assert llm.facts_call_count == 4
    assert "extraction_errors" not in result
    [draft] = result["drafts"]
    assert draft["issues"] == []
    roles = {a["attribute_role"] for a in draft["attributes"]}
    assert roles == {"RATE", "THRESHOLD"}


def test_process_clause_stages_for_review_when_consistency_issues_never_clear(database):
    # Before this retry feature existed, a candidate with unresolved consistency issues
    # was still staged as PENDING_REVIEW with the issues folded into review_notes for a
    # human to see; exhausting the retry budget must not make this worse by turning it
    # into a lost `extraction_errors` entry instead.
    unresolved = PenaltyFactList(facts=[_rate_fact(), _threshold_fact()])
    llm = FakeLLM(
        classify_result=_classification(),
        facts_results=[unresolved, unresolved, unresolved, unresolved],
    )
    nodes = _nodes(llm, database)

    result = nodes.process_clause(
        {"clause": {"clause_text": RETAILER_AGREEMENT_TEXT, "section_title": "Shortages"}}
    )

    assert llm.facts_call_count == 4
    assert "extraction_errors" not in result
    [draft] = result["drafts"]
    assert any("Rule 4" in issue for issue in draft["issues"])
    roles = {a["attribute_role"] for a in draft["attributes"]}
    assert roles == {"RATE", "THRESHOLD"}


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
    retailer_agreement_id = _make_retailer_agreement(db_session, retailer_id, "1" * 64)
    run_id = _make_agent_run(db_session)
    db_session.commit()

    draft = {
        "clause_text": RETAILER_AGREEMENT_TEXT,
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
        {
            "retailer_agreement_id": retailer_agreement_id,
            "run_id": run_id,
            "drafts": [draft],
            "extraction_errors": [error],
        }
    )

    assert len(result["staged_rule_ids"]) == 1

    with database.session() as session:
        staged = ExtractedPenaltyRuleRepository(session).list_for_retailer_agreement(retailer_agreement_id)
        assert len(staged) == 1
        assert staged[0].status == "PENDING_REVIEW"

        [attribute] = session.scalars(select(ExtractedPenaltyRuleAttribute)).all()
        assert attribute.branch_no == 0

        errors = session.scalars(select(ProcessingError)).all()
        assert len(errors) == 1
        assert errors[0].error_type == "RULE_EXTRACTION_SCREENING_FAILED"


def test_stage_rules_folds_unresolved_consistency_issues_into_review_notes(database, db_session):
    # A candidate that exhausted its retry budget still reaches stage_rules as a normal
    # draft (see _classify_and_extract), carrying its unresolved issues; stage_rules must
    # persist it as PENDING_REVIEW with those issues visible in review_notes, not drop it.
    retailer_id = _make_retailer(db_session)
    retailer_agreement_id = _make_retailer_agreement(db_session, retailer_id, "2" * 64)
    run_id = _make_agent_run(db_session)
    db_session.commit()

    draft = {
        "clause_text": RETAILER_AGREEMENT_TEXT,
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
        "attributes": [],
        "issues": ["Rule 4 (tier_application required): group_no=0 has a RATE beside a THRESHOLD."],
        "readiness_notes": [],
        "pricing_readiness": "AWAITING_DATA",
    }
    nodes = _nodes(FakeLLM(), database)

    result = nodes.stage_rules(
        {
            "retailer_agreement_id": retailer_agreement_id,
            "run_id": run_id,
            "drafts": [draft],
        }
    )

    assert len(result["staged_rule_ids"]) == 1
    with database.session() as session:
        [staged] = ExtractedPenaltyRuleRepository(session).list_for_retailer_agreement(retailer_agreement_id)
        assert staged.status == "PENDING_REVIEW"
        assert "Rule 4" in staged.review_notes


def test_stage_rules_sanitizes_nul_bytes_in_extraction_errors_before_logging(database, db_session):
    # A NUL byte can resurface from a rejected insert's DB driver error string
    # (str(exc)) or from a clause excerpt; either one must be scrubbed before it
    # reaches the Text/JSONB processing_error columns, or the whole run's commit fails.
    retailer_id = _make_retailer(db_session)
    retailer_agreement_id = _make_retailer_agreement(db_session, retailer_id, "3" * 64)
    run_id = _make_agent_run(db_session)
    db_session.commit()

    error = {
        "error_code": "RULE_EXTRACTION_SCREENING_FAILED",
        "message": "boom\x00: NUL from a rejected insert",
        "node_name": "screen_unit",
        "detail": {"excerpt": "some clause\x00text"},
    }
    nodes = _nodes(FakeLLM(), database)

    result = nodes.stage_rules(
        {
            "retailer_agreement_id": retailer_agreement_id,
            "run_id": run_id,
            "drafts": [],
            "extraction_errors": [error],
        }
    )

    assert result["staged_rule_ids"] == []
    with database.session() as session:
        [logged] = session.scalars(select(ProcessingError)).all()
        assert "\x00" not in logged.error_message
        assert "\x00" not in logged.raw_error_detail["excerpt"]


# ---------------------------------------------------------------------------
# human_review
# ---------------------------------------------------------------------------


def test_human_review_falls_through_without_interrupting_when_queue_is_empty(database, db_session):
    retailer_id = _make_retailer(db_session)
    retailer_agreement_id = _make_retailer_agreement(db_session, retailer_id, "2" * 64)
    run_id = _make_agent_run(db_session)
    db_session.commit()

    nodes = _nodes(FakeLLM(), database)

    result = nodes.human_review({"retailer_agreement_id": retailer_agreement_id, "run_id": run_id})

    assert result == {}


# ---------------------------------------------------------------------------
# apply_decisions
# ---------------------------------------------------------------------------


def test_apply_decisions_ignores_rows_still_pending_review(database, db_session):
    retailer_id = _make_retailer(db_session)
    retailer_agreement_id = _make_retailer_agreement(db_session, retailer_id, "3" * 64)
    run_id = _make_agent_run(db_session)
    ExtractedPenaltyRuleRepository(db_session).add_extracted_rule(
        retailer_agreement_id=retailer_agreement_id,
        agent_run_id=run_id,
        clause_text=RETAILER_AGREEMENT_TEXT,
        clause_fingerprint="a" * 32,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
    )
    db_session.commit()

    nodes = _nodes(FakeLLM(), database)
    result = nodes.apply_decisions({"retailer_agreement_id": retailer_agreement_id, "run_id": run_id})

    assert result["applied_rule_ids"] == []


def test_apply_decisions_returns_ids_of_rows_with_a_real_verdict(database, db_session):
    retailer_id = _make_retailer(db_session)
    retailer_agreement_id = _make_retailer_agreement(db_session, retailer_id, "4" * 64)
    run_id = _make_agent_run(db_session)
    repo = ExtractedPenaltyRuleRepository(db_session)
    rule = repo.add_extracted_rule(
        retailer_agreement_id=retailer_agreement_id,
        agent_run_id=run_id,
        clause_text=RETAILER_AGREEMENT_TEXT,
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
    result = nodes.apply_decisions({"retailer_agreement_id": retailer_agreement_id, "run_id": run_id})

    assert result["applied_rule_ids"] == [str(rule.id)]
