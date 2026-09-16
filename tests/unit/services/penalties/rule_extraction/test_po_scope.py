"""Tests for `app.services.penalties.rule_extraction.clause_matching`.

Zero DB dependency: pure category-table lookups plus boolean flooring.
"""

from app.services.penalties.rule_extraction.clause_matching import decide_po_scope


def test_trigger_category_is_in_scope_and_floors_its_governed_flag():
    # SHORT_SHIP's governed default is po_shortage_flag=True; the classifier under-called
    # it, and the floor corrects that rather than trusting the classifier's False.
    decision = decide_po_scope("SHORT_SHIP", po_shortage_flag=False, po_delay_flag=False)

    assert decision.in_scope is True
    assert decision.shortage is True
    assert decision.delay is False


def test_flags_above_the_governed_default_are_trusted_not_clipped():
    # OTIF_LATE defaults both flags True; the classifier agreeing changes nothing here,
    # but a classifier-raised flag on a category whose default is False must still win.
    decision = decide_po_scope("QUALITY_DEFECT_CHARGEBACK", po_shortage_flag=False, po_delay_flag=True)

    assert decision.delay is True


def test_quality_family_category_is_in_scope_even_with_both_po_flags_true():
    # Phase 1 de-restriction: QUALITY is a real, dispute-priceable engine_family, so
    # in_scope ("some engine can price this") is True regardless of the PO flags.
    decision = decide_po_scope("QUALITY_DEFECT_CHARGEBACK", po_shortage_flag=True, po_delay_flag=True)

    assert decision.in_scope is True
    assert decision.engine_family == "QUALITY"


def test_bounds_po_category_is_in_scope_with_both_flags_false():
    # AGGREGATE_LIABILITY_CAP never triggers a charge, it bounds one, so its governed
    # flags are both False, but the rule still belongs in a PO-scoped run.
    decision = decide_po_scope("AGGREGATE_LIABILITY_CAP", po_shortage_flag=False, po_delay_flag=False)

    assert decision.in_scope is True
    assert decision.shortage is False
    assert decision.delay is False
    assert decision.engine_family == "LIABILITY_CAP"


def test_unmapped_category_is_out_of_scope():
    decision = decide_po_scope("UNMAPPED", po_shortage_flag=False, po_delay_flag=False)

    assert decision.in_scope is False
    assert decision.engine_family == "UNPRICEABLE"
