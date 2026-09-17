"""Tests for `app.services.penalties.rule_extraction.fact_processing`.

Zero DB dependency: each of the five `pricing_readiness` values is reachable.
"""

from app.services.penalties.rule_extraction.fact_processing import evaluate_readiness


def test_ready_for_a_complete_rate():
    readiness, notes = evaluate_readiness("PER_UNIT", [{"attribute_role": "RATE", "value": 50.0}])

    assert readiness == "READY"
    assert notes == []


def test_unsupported_shape_when_calc_type_is_missing():
    readiness, notes = evaluate_readiness(None, [])

    assert readiness == "UNSUPPORTED_SHAPE"
    assert notes


def test_not_a_charge_for_a_pure_limit():
    readiness, _notes = evaluate_readiness("LIMIT_ONLY", [])

    assert readiness == "NOT_A_CHARGE"


def test_awaiting_data_for_a_missing_rate():
    readiness, notes = evaluate_readiness("PER_UNIT", [])

    assert readiness == "AWAITING_DATA"
    assert notes


def test_needs_external_figure_for_an_external_reference():
    facts = [{"attribute_role": "RATE", "value_status": "EXTERNAL_REFERENCE"}]

    readiness, _notes = evaluate_readiness("PER_UNIT", facts)

    assert readiness == "NEEDS_EXTERNAL_FIGURE"
