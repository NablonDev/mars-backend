"""Tests for `app.services.penalties.rule_extraction.fact_processing`.

Zero DB dependency: pure dict transforms.
"""

from app.services.penalties.rule_extraction.fact_processing import normalize_facts


def test_normalize_facts_produces_a_stable_canonical_order_across_shuffled_inputs():
    threshold = {
        "group_no": 1,
        "attribute_role": "THRESHOLD",
        "metric_code": "SHORTFALL_PCT",
        "operator": "GTE",
        "value": 5,
    }
    rate = {"group_no": 1, "attribute_role": "RATE", "value": 2, "value_unit": "PERCENT"}
    cap = {"group_no": 0, "attribute_role": "CAP", "value": 1000, "value_unit": "USD"}

    first = normalize_facts([rate, threshold, cap])
    second = normalize_facts([cap, threshold, rate])

    assert first == second
    assert [f["attribute_role"] for f in first] == ["CAP", "THRESHOLD", "RATE"]


def test_normalize_facts_deduplicates_identical_facts_keeping_the_longer_raw_text():
    short = {"group_no": 1, "attribute_role": "RATE", "value": 1, "value_unit": "PERCENT", "raw_text": "1%"}
    long = {
        "group_no": 1,
        "attribute_role": "RATE",
        "value": 1,
        "value_unit": "PERCENT",
        "raw_text": "a fee of 1% per occurrence",
    }

    normalized = normalize_facts([short, long])

    assert len(normalized) == 1
    assert normalized[0]["raw_text"] == "a fee of 1% per occurrence"


def test_normalize_facts_carries_needs_review_onto_the_surviving_duplicate():
    plain = {"group_no": 1, "attribute_role": "CAP", "value": 100, "value_unit": "USD"}
    flagged = {
        "group_no": 1,
        "attribute_role": "CAP",
        "value": 100,
        "value_unit": "USD",
        "needs_review": True,
        "review_notes": "ambiguous cap wording",
    }

    normalized = normalize_facts([plain, flagged])

    assert len(normalized) == 1
    assert normalized[0]["needs_review"] is True
    assert normalized[0]["review_notes"] == "ambiguous cap wording"


def test_normalize_facts_keeps_facts_with_different_identities_distinct():
    rate_branch_one = {"group_no": 1, "attribute_role": "RATE", "value": 1, "value_unit": "PERCENT"}
    rate_branch_two = {"group_no": 2, "attribute_role": "RATE", "value": 2, "value_unit": "PERCENT"}

    normalized = normalize_facts([rate_branch_one, rate_branch_two])

    assert len(normalized) == 2
