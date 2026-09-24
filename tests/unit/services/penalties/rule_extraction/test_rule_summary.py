"""Tests for `app.services.penalties.rule_extraction.rule_summary.describe_rule`.

Pure and deterministic: no LLM call, no database, exercised against literal fact rows
in both the pipeline's normalized shape and the DB-shaped (`extra`-nested) shape.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.penalties.rule_extraction.rule_summary import describe_rule


def test_otif_greater_of_two_alternative_rates():
    facts = [
        {
            "branch_no": 1,
            "attribute_role": "THRESHOLD",
            "metric_code": "OTIF_PCT",
            "operator": "LT",
            "value": 98,
            "value_unit": "PERCENT",
            "source_text": (
                "Vendor's aggregate monthly On-Time delivery compliance falls below "
                "ninety-eight percent (98.0%)"
            ),
            "trigger_logic": "OR",
        },
        {
            "branch_no": 1,
            "attribute_role": "RATE",
            "value": 3,
            "value_unit": "PERCENT",
            "basis_type": "INVOICE_VALUE",
            "applies_per": "PO",
            "combinator": "MAX",
        },
        {
            "branch_no": 2,
            "attribute_role": "RATE",
            "value": 500,
            "value_unit": "USD",
            "basis_type": "NONE",
            "applies_per": "TRUCKLOAD",
            "combinator": "MAX",
        },
    ]

    result = describe_rule(
        calc_type="FORMULA_OTHER",
        consequence_type=None,
        settlement_method="INVOICE_DEDUCTION",
        facts=facts,
    )

    assert result["when"] == (
        "Vendor's aggregate monthly On-Time delivery compliance falls below "
        "ninety-eight percent (98.0%) [OTIF_PCT < 98%]"
    )
    assert result["charge"] == "greater of 3% of invoice value per PO or $500 per truckload"
    assert result["how"] == "Collected via invoice deduction"


def test_branches_render_in_numeric_order_regardless_of_input_order():
    # branch 2's RATE row comes first in the input; the charge must still list branch 1
    # first, ordered by branch_no rather than first-appearance order.
    facts = [
        {
            "branch_no": 2,
            "attribute_role": "RATE",
            "value": 500,
            "value_unit": "USD",
            "basis_type": "NONE",
            "applies_per": "TRUCKLOAD",
            "combinator": "MAX",
        },
        {
            "branch_no": 1,
            "attribute_role": "RATE",
            "value": 3,
            "value_unit": "PERCENT",
            "basis_type": "INVOICE_VALUE",
            "applies_per": "PO",
            "combinator": "MAX",
        },
    ]

    result = describe_rule(
        calc_type="FORMULA_OTHER",
        consequence_type=None,
        settlement_method=None,
        facts=facts,
    )

    assert result["charge"] == "greater of 3% of invoice value per PO or $500 per truckload"


def test_per_pallet_flat_rate_with_hour_threshold():
    facts = [
        {
            "branch_no": 0,
            "attribute_role": "RATE",
            "value": 75,
            "value_unit": "USD",
            "basis_type": "NONE",
            "applies_per": "PALLET",
        },
        {
            "branch_no": 0,
            "attribute_role": "THRESHOLD",
            "value": 24,
            "value_unit": "HOURS",
            "operator": "GT",
            "metric_code": "DELIVERY_WINDOW_VIOLATION",
            "source_text": "past twenty-four (24) hours",
        },
    ]

    result = describe_rule(calc_type="PER_UNIT", consequence_type=None, settlement_method=None, facts=facts)

    assert result["charge"] == "$75 per pallet"
    assert result["when"] == "past twenty-four (24) hours [DELIVERY_WINDOW_VIOLATION > 24 hours]"
    assert result["how"] is None


def test_non_monetary_remedy_with_no_facts():
    result = describe_rule(
        calc_type="NON_MONETARY",
        consequence_type="SHORTFALL_QUANTITY_CANCELLED",
        settlement_method=None,
        facts=[],
    )

    assert result["charge"] == "Not charged — shortfall quantity cancelled"
    assert result["when"] == "As stated in the clause (no measurable trigger extracted)"


def test_rate_with_value_missing_renders_value_status():
    facts = [
        {
            "branch_no": 0,
            "attribute_role": "RATE",
            "value": None,
            "value_unit": None,
            "basis_type": "NONE",
            "value_status": "EXTERNAL_REFERENCE",
        }
    ]

    result = describe_rule(
        calc_type="FORMULA_OTHER", consequence_type=None, settlement_method=None, facts=facts
    )

    assert result["charge"] == "amount not stated (external reference)"


def test_db_shaped_rows_with_combinator_nested_under_extra():
    facts = [
        {
            "branch_no": 1,
            "attribute_role": "RATE",
            "value": Decimal("2.500000"),
            "value_unit": "USD",
            "basis_type": "NONE",
            "applies_per": "CASE",
            "extra": {"combinator": "MAX"},
        },
        {
            "branch_no": 2,
            "attribute_role": "RATE",
            "value": Decimal("4.000000"),
            "value_unit": "PERCENT",
            "basis_type": "SHORTFALL_VALUE",
            "extra": {"combinator": "MAX"},
        },
    ]

    result = describe_rule(
        calc_type="FORMULA_OTHER", consequence_type=None, settlement_method=None, facts=facts
    )

    assert result["charge"] == "greater of $2.50 per case or 4% of shortfall value"


def test_rate_with_cap_suffix():
    facts = [
        {
            "branch_no": 0,
            "attribute_role": "RATE",
            "value": 2,
            "value_unit": "PERCENT",
            "basis_type": "PO_VALUE",
        },
        {"branch_no": 0, "attribute_role": "CAP", "value": 10000, "value_unit": "USD"},
    ]

    result = describe_rule(
        calc_type="FORMULA_OTHER", consequence_type=None, settlement_method=None, facts=facts
    )

    assert result["charge"] == "2% of PO value, capped at $10,000"


def test_long_source_text_is_truncated_to_157_chars_plus_ellipsis():
    long_text = "x" * 200
    facts = [
        {
            "branch_no": 0,
            "attribute_role": "THRESHOLD",
            "source_text": long_text,
            "operator": None,
            "value": None,
        }
    ]

    result = describe_rule(
        calc_type="FORMULA_OTHER", consequence_type=None, settlement_method=None, facts=facts
    )

    assert result["when"] == "x" * 157 + "…"
