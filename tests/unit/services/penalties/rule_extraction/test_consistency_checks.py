"""Tests for `app.services.penalties.rule_extraction.consistency_checks`.

Zero DB dependency: pure list-of-dict checks.
"""

from app.services.penalties.rule_extraction.consistency_checks import consistency_issues


def test_flags_a_tier_band_gap_and_a_rate_missing_tier_application_for_a_tiered_rule():
    facts = [
        {
            "group_no": 1,
            "attribute_role": "THRESHOLD",
            "metric_code": "SHORTFALL_PCT",
            "operator": "LTE",
            "value": 10,
        },
        # Shares group_no=1 with the THRESHOLD above but never states tier_application.
        {"group_no": 1, "attribute_role": "RATE", "value": 1, "value_unit": "PERCENT"},
        {
            "group_no": 2,
            "attribute_role": "THRESHOLD",
            "metric_code": "SHORTFALL_PCT",
            "operator": "GTE",
            "value": 20,
        },
    ]

    issues = consistency_issues(facts, calc_type="TIERED")

    assert any("Rule 4" in issue for issue in issues)
    assert any("Rule 5" in issue for issue in issues)


def test_tier_band_gap_is_not_checked_outside_a_tiered_calc_type():
    # The publisher only ever builds tier bands for calc_type=TIERED, so Rule 5 is
    # scoped to match: the same gap on a non-TIERED rule is not a tier ladder at all.
    facts = [
        {
            "group_no": 1,
            "attribute_role": "THRESHOLD",
            "metric_code": "SHORTFALL_PCT",
            "operator": "LTE",
            "value": 10,
        },
        {
            "group_no": 2,
            "attribute_role": "THRESHOLD",
            "metric_code": "SHORTFALL_PCT",
            "operator": "GTE",
            "value": 20,
        },
    ]

    issues = consistency_issues(facts, calc_type="FORMULA_OTHER")

    assert not any("Rule 5" in issue for issue in issues)


def test_flags_pure_cap_misfiling():
    facts = [{"group_no": 0, "attribute_role": "CAP", "value": 1000}]

    issues = consistency_issues(facts, calc_type="PER_UNIT")

    assert any("Rule 1" in issue for issue in issues)


def test_flags_conflicting_trigger_logic_in_one_branch():
    facts = [
        {"group_no": 1, "attribute_role": "THRESHOLD", "trigger_logic": "AND"},
        {"group_no": 1, "attribute_role": "THRESHOLD", "trigger_logic": "OR"},
    ]

    issues = consistency_issues(facts, calc_type="PER_UNIT")

    assert any("Rule 13" in issue for issue in issues)


def test_flags_a_fractional_rate_with_no_rounding_rule():
    facts = [{"group_no": 1, "attribute_role": "RATE", "applies_per": "WEEK"}]

    issues = consistency_issues(facts, calc_type="PER_UNIT")

    assert any("Rule 14" in issue for issue in issues)


def test_a_group0_rounding_rule_satisfies_every_branchs_fractional_rate():
    facts = [
        {"group_no": 0, "attribute_role": "ROUNDING_RULE"},
        {"group_no": 1, "attribute_role": "RATE", "applies_per": "WEEK"},
    ]

    issues = consistency_issues(facts, calc_type="PER_UNIT")

    assert not any("Rule 14" in issue for issue in issues)


def test_flags_a_combinator_group_mixing_a_selection_with_a_tally():
    facts = [
        {"group_no": 1, "attribute_role": "RATE", "combinator": "MAX"},
        {"group_no": 1, "attribute_role": "RATE", "combinator": "SUM"},
    ]

    issues = consistency_issues(facts, calc_type="PER_UNIT")

    assert any("Rule 9" in issue for issue in issues)


def test_no_issues_for_a_clean_single_branch_rule():
    facts = [
        {"group_no": 0, "attribute_role": "RATE", "value": 50, "value_unit": "USD"},
    ]

    assert consistency_issues(facts, calc_type="PER_UNIT") == []
