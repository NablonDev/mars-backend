"""Unit tests for FineProjectionSummaryService's content fingerprint -- a pure,
DB-free function over the engine's OUTPUTS for the "current" day plus
material facts, deliberately excluding as_of_date/current_projection_date,
any generated-at timestamp, and days_to_delivery. See
app/agents/penalties/projection/prompts/v1.py and .PROGRESS.md for why.

Was `tests/unit/services/test_fine_projection_summary_fingerprint.py`
(`fine`/`fines` -> `penalty`/`penalties` rename)."""

from datetime import date
from typing import Any

from app.agents.penalties.projection import (
    ActiveRule,
    DailyHistoryEntry,
    OrderContext,
    PenaltyProjectionSummaryContext,
    ViolationEntry,
)
from app.services.penalties.projection.summary_service import _compute_content_fingerprint, _fmt_number


def _order(**overrides: Any) -> OrderContext:
    fields: dict[str, Any] = {
        "order_id": "ORD-FP",
        "order_status": "OPEN",
        "retailer_name": "Retailer FP",
        "sku_description": "Widget",
        "order_qty": 1000,
        "unit_price": 10.0,
        "required_ship_date": date(2026, 8, 8),
        "requested_delivery_date": date(2026, 8, 10),
    }
    fields.update(overrides)
    return OrderContext(**fields)


def _violation(**overrides: Any) -> ViolationEntry:
    fields: dict[str, Any] = {
        "violation_type": "OTIF_LATE",
        "rule_id": "RULE-FP-FLAT",
        "probability": 0.05,
        "expected_penalty_amount": 2.5,
    }
    fields.update(overrides)
    return ViolationEntry(**fields)


def _entry(**overrides: Any) -> DailyHistoryEntry:
    fields: dict[str, Any] = {
        "entry_date": date(2026, 8, 5),
        "confirmed_qty": 1000,
        "production_status": "ON_TRACK",
        "appointment_status": "SCHEDULED",
        "actual_ship_date": None,
        "expected_ship_date_override": None,
        "demand_exception_flagged": False,
        "days_to_delivery": 5,
        "shortage_probability": 0.05,
        "delay_probability": 0.05,
        "violations": [_violation()],
        "total_expected_penalty_amount": 2.5,
    }
    fields.update(overrides)
    return DailyHistoryEntry(**fields)


def _rule(**overrides: Any) -> ActiveRule:
    fields: dict[str, Any] = {
        "rule_id": "RULE-FP-FLAT",
        "violation_type": "OTIF_LATE",
        "calc_type": "FLAT_FEE",
        "rate": 50.0,
    }
    fields.update(overrides)
    return ActiveRule(**fields)


def _context(
    *,
    current_projection_date: date = date(2026, 8, 5),
    daily_history: list[DailyHistoryEntry] | None = None,
    active_rules: list[ActiveRule] | None = None,
    order: OrderContext | None = None,
    stacking_mode: str = "SUM",
) -> PenaltyProjectionSummaryContext:
    return PenaltyProjectionSummaryContext(
        order=order or _order(),
        current_projection_date=current_projection_date,
        stacking_mode=stacking_mode,
        active_rules=active_rules if active_rules is not None else [_rule()],
        daily_history=daily_history if daily_history is not None else [_entry()],
    )


def test_identical_contexts_hash_the_same():
    assert _compute_content_fingerprint(_context()) == _compute_content_fingerprint(_context())


def test_changed_as_of_date_alone_hashes_identically():
    """The key property: current_projection_date is not part of the
    payload, so requesting a different as_of_date over the exact same
    projection outputs must not change the fingerprint."""
    fp_day1 = _compute_content_fingerprint(_context(current_projection_date=date(2026, 8, 5)))
    fp_day2 = _compute_content_fingerprint(_context(current_projection_date=date(2026, 8, 9)))

    assert fp_day1 == fp_day2


def test_changed_probability_changes_the_hash():
    baseline = _compute_content_fingerprint(_context())
    changed = _compute_content_fingerprint(
        _context(daily_history=[_entry(violations=[_violation(probability=0.15)])])
    )

    assert baseline != changed


def test_changed_expected_penalty_amount_changes_the_hash():
    baseline = _compute_content_fingerprint(_context())
    changed = _compute_content_fingerprint(
        _context(
            daily_history=[
                _entry(
                    violations=[_violation(expected_penalty_amount=7.5)],
                    total_expected_penalty_amount=7.5,
                )
            ]
        )
    )

    assert baseline != changed


def test_changed_days_to_delivery_alone_does_not_change_the_hash():
    """days_to_delivery is explicitly excluded -- it's banded completely
    differently for shortage vs delay probability (see v1.py), so it must
    never gate reuse on its own."""
    baseline = _compute_content_fingerprint(_context())
    changed = _compute_content_fingerprint(_context(daily_history=[_entry(days_to_delivery=0)]))

    assert baseline == changed


def test_changed_stacking_mode_changes_the_hash():
    baseline = _compute_content_fingerprint(_context())
    changed = _compute_content_fingerprint(_context(stacking_mode="MAX"))

    assert baseline != changed


def test_changed_order_status_changes_the_hash():
    baseline = _compute_content_fingerprint(_context())
    changed = _compute_content_fingerprint(_context(order=_order(order_status="DELIVERED")))

    assert baseline != changed


def test_changed_active_rule_ids_changes_the_hash():
    baseline = _compute_content_fingerprint(_context())
    changed = _compute_content_fingerprint(
        _context(active_rules=[_rule(rule_id="RULE-OTHER")], daily_history=[_entry(violations=[])])
    )

    assert baseline != changed


def test_changed_shipment_state_changes_the_hash():
    baseline = _compute_content_fingerprint(_context())
    changed = _compute_content_fingerprint(
        _context(daily_history=[_entry(actual_ship_date=date(2026, 8, 7))])
    )

    assert baseline != changed


def test_empty_daily_history_does_not_crash():
    """Defensive: the pure function should degrade gracefully rather than
    raising if it's ever handed a context with no history entries."""
    fingerprint = _compute_content_fingerprint(_context(daily_history=[]))

    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64


def test_fmt_number_is_stable_across_int_vs_float_and_float_rounding_noise():
    assert _fmt_number(10) == _fmt_number(10.0) == _fmt_number(10.00)
    # A trailing floating-point representation artifact must still collapse
    # to the same formatted string as the "clean" value.
    assert _fmt_number(2.5000000000000004) == _fmt_number(2.5)
