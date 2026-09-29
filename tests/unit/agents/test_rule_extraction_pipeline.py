"""Tests for `app.agents.penalties.rule_extraction.pipeline.classify_and_extract`.

Pure: exercised with fake `classify`/`extract_facts` callables, no database and no
LangGraph involvement at all.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.penalties.rule_extraction.context import ClauseClassificationContext, RuleFactContext
from app.agents.penalties.rule_extraction.pipeline import ClauseFeedback, classify_and_extract
from app.agents.penalties.rule_extraction.schema import PenaltyFact, PenaltyFactList, PenaltyRuleExtraction

CLAUSE_TEXT = "Retailer may assess a $50 fee per short-shipped case."


def _classification(**overrides) -> PenaltyRuleExtraction:
    fields = {
        "is_penalty_rule": True,
        "penalty_category": "SHORT_SHIP",
        "calc_type": "PER_UNIT",
        "economic_effect_type": "CHARGEBACK",
        "po_shortage_flag": False,
        "po_delay_flag": False,
        "confidence": 0.9,
        "plain_explanation": "Vendor short-ships; retailer charges a flat fee per case.",
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


def test_classify_and_extract_builds_a_draft_with_attributes_for_a_penalty_clause():
    result = classify_and_extract(
        classify=lambda context: _classification(),
        extract_facts=lambda context: PenaltyFactList(facts=[_rate_fact()]),
        section_title="Shortages",
        clause_text=CLAUSE_TEXT,
    )

    draft = result["draft"]
    assert draft["master"]["penalty_category"] == "SHORT_SHIP"
    assert len(draft["attributes"]) == 1
    assert draft["attributes"][0]["attribute_role"] == "RATE"
    assert draft["computed_summary"] == {
        "when": "As stated in the clause (no measurable trigger extracted)",
        "charge": "50",
        "how": None,
    }


def test_classify_and_extract_skips_a_non_penalty_clause_without_calling_extract_facts():
    def _fail_extract_facts(context):
        raise AssertionError("extract_facts must not be called")

    classification = _classification(
        is_penalty_rule=False,
        penalty_category="UNMAPPED",
        calc_type="UNSPECIFIED",
        economic_effect_type="OTHER",
        review_notes="just a definitions clause",
    )
    result = classify_and_extract(
        classify=lambda context: classification,
        extract_facts=_fail_extract_facts,
        section_title="Definitions",
        clause_text="Definitions.",
    )

    assert "draft" not in result
    assert "error" not in result
    assert result["skipped"] == {
        "section_title": "Definitions",
        "excerpt": "Definitions.",
        "review_notes": "just a definitions clause",
    }


def test_classify_and_extract_keeps_a_non_penalty_clause_as_a_zero_attribute_draft_when_asked():
    classification = _classification(
        is_penalty_rule=False,
        penalty_category="UNMAPPED",
        calc_type="UNSPECIFIED",
        economic_effect_type="OTHER",
        review_notes="just a definitions clause",
    )
    result = classify_and_extract(
        classify=lambda context: classification,
        extract_facts=lambda context: (_ for _ in ()).throw(AssertionError("must not be called")),
        section_title="Definitions",
        clause_text="Definitions.",
        keep_non_penalty=True,
    )

    assert "skipped" not in result
    draft = result["draft"]
    assert draft["attributes"] == []
    assert draft["master"]["is_penalty_rule"] is False


def test_classify_and_extract_reports_a_classification_failure():
    def _raise(context):
        raise RuntimeError("boom")

    result = classify_and_extract(
        classify=_raise,
        extract_facts=lambda context: PenaltyFactList(facts=[]),
        section_title=None,
        clause_text=CLAUSE_TEXT,
    )

    assert "draft" not in result
    assert result["error"]["error_code"] == "RULE_EXTRACTION_CLASSIFICATION_FAILED"
    assert "boom" in result["error"]["message"]


def test_classify_and_extract_retries_fact_extraction_until_consistent_then_stops():
    # A RATE beside a THRESHOLD with tier_application unset trips Rule 4; the 4th
    # attempt fixes it, so extract_facts must be called exactly 4 times, not more.
    unresolved = PenaltyFactList(facts=[_rate_fact(), _threshold_fact()])
    resolved = PenaltyFactList(facts=[_rate_fact(tier_application="NOT_APPLICABLE"), _threshold_fact()])
    queue = [unresolved, unresolved, unresolved, resolved]
    call_count = 0

    def _extract_facts(context):
        nonlocal call_count
        call_count += 1
        return queue.pop(0)

    result = classify_and_extract(
        classify=lambda context: _classification(),
        extract_facts=_extract_facts,
        section_title="Shortages",
        clause_text=CLAUSE_TEXT,
    )

    assert call_count == 4
    draft = result["draft"]
    assert draft["issues"] == []
    roles = {a["attribute_role"] for a in draft["attributes"]}
    assert roles == {"RATE", "THRESHOLD"}


def test_classify_and_extract_stops_at_the_first_clean_attempt():
    call_count = 0

    def _extract_facts(context):
        nonlocal call_count
        call_count += 1
        return PenaltyFactList(facts=[_rate_fact()])

    result = classify_and_extract(
        classify=lambda context: _classification(),
        extract_facts=_extract_facts,
        section_title="Shortages",
        clause_text=CLAUSE_TEXT,
    )

    assert call_count == 1
    assert result["draft"]["issues"] == []


def test_classify_and_extract_threads_feedback_into_both_stage_contexts():
    feedback = ClauseFeedback(
        reviewer_instruction="This should be a percentage of invoice value, not a flat fee.",
        current_rule={"calc_type": "PER_UNIT"},
        current_facts=[{"attribute_role": "RATE", "value": 50}],
        revision_history=[{"reviewer_instruction": "earlier note", "reviewer_reply": "earlier reply"}],
    )
    seen_classification_contexts: list[ClauseClassificationContext] = []
    seen_fact_contexts: list[RuleFactContext] = []

    def _classify(context: ClauseClassificationContext) -> PenaltyRuleExtraction:
        seen_classification_contexts.append(context)
        return _classification()

    def _extract_facts(context: RuleFactContext) -> PenaltyFactList:
        seen_fact_contexts.append(context)
        return PenaltyFactList(facts=[_rate_fact()])

    result = classify_and_extract(
        classify=_classify,
        extract_facts=_extract_facts,
        section_title="Shortages",
        clause_text=CLAUSE_TEXT,
        feedback=feedback,
    )

    assert "draft" in result
    assert len(seen_classification_contexts) == 1
    classification_context = seen_classification_contexts[0]
    assert classification_context.reviewer_instruction == feedback.reviewer_instruction
    assert classification_context.current_rule == feedback.current_rule
    assert classification_context.revision_history == feedback.revision_history

    assert len(seen_fact_contexts) == 1
    fact_context = seen_fact_contexts[0]
    assert fact_context.reviewer_instruction == feedback.reviewer_instruction
    assert fact_context.current_facts == feedback.current_facts


def test_penalty_rule_extraction_requires_plain_explanation():
    fields = {
        "is_penalty_rule": True,
        "penalty_category": "SHORT_SHIP",
        "calc_type": "PER_UNIT",
        "economic_effect_type": "CHARGEBACK",
        "po_shortage_flag": False,
        "po_delay_flag": False,
        "confidence": 0.9,
    }
    with pytest.raises(ValidationError, match="plain_explanation"):
        PenaltyRuleExtraction(**fields)
