"""Tests for `DisputeResolutionService`: state transitions, all three `analyze()`
error paths, and one full-lifecycle test (open -> analyze -> resolve with
override -> assert final state and audit fields) against the SQLite test
DB -- same "unit test doubling as the full-flow/integration check" posture
`tests/unit/services/test_po_delivery_change_request_service.py` already
uses for its own sibling lifecycle record (no live Postgres needed; see
`tests/conftest.py`'s module docstring).

Real, post-delivery facts are seeded via `delivery`/`delivery_line`
(shortage) and `shipment` (delay) -- never `order_confirmation`, the
pre-delivery promise a dispute must not use (see
`app.services.penalties.dispute.types`'s module docstring).
"""

import hashlib
from datetime import date, datetime, timedelta

import pytest

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.models import RetailerAgreement
from app.services.penalties.dispute.service import DisputeResolutionService
from app.services.penalties.projection.delay import price_delay_penalty
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.types import APPLIES_PER_DAY
from tests.conftest import make_retailer_agreement

_ORDER_QTY = 100
_UNIT_PRICE = 10.0
_REQUESTED_DELIVERY_DATE = date(2026, 6, 10)


def _build_service(repos) -> DisputeResolutionService:
    projection_service = ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
    )
    return DisputeResolutionService(
        purchase_orders=repos.purchase_orders,
        disputes=repos.disputes,
        actual_penalties=repos.actual_penalties,
        rules=repos.penalty_rules,
        projection_service=projection_service,
        retailer_agreements=repos.retailer_agreements,
    )


def _seed_order(
    repos,
    po_number: str,
    *,
    violation_type: str = "SHORT_SHIP",
    calc_type: str = "PER_UNIT",
    rate: float = 5.0,
    grace_period_days: int = 0,
    rule_effective_start: date = date(2026, 1, 1),
    rule_effective_end: date | None = None,
    applies_per: str | None = None,
):
    """Seeds a PO with one penalty rule, no fulfillment facts of its own --
    callers attach delivery/delivery_line and/or shipment rows themselves.
    Returns (purchase_order_id, line_id, retailer_id)."""
    retailer = repos.master_data.add_retailer(f"RET-{po_number}", "Dispute Test Retailer", None, "SUM")
    material = repos.master_data.add_material(f"MAT-{po_number}", None)
    plant = repos.master_data.add_plant(f"PLANT-{po_number}", None, None)
    repos.penalty_rules.add_rule(
        rule_code=f"RULE-{po_number}",
        violation_type=violation_type,
        penalty_category=violation_type,
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type=calc_type,
        rate=rate,
        grace_period_days=grace_period_days,
        effective_start_date=rule_effective_start,
        effective_end_date=rule_effective_end,
        applies_per=applies_per,
    )

    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=po_number,
        retailer_id=retailer["id"],
        order_date=date(2026, 5, 1),
        requested_delivery_date=_REQUESTED_DELIVERY_DATE,
        required_ship_date=date(2026, 6, 8),
        order_status="DELIVERED",
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=_ORDER_QTY,
        unit_price=_UNIT_PRICE,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    return purchase_order["id"], line["id"], retailer["id"]


def _seed_delivery(repos, purchase_order_id, line_id, delivered_qty: float, as_of_date: date):
    delivery = repos.fulfillment.add_delivery(
        delivery_number=f"DELIV-{purchase_order_id}",
        purchase_order_id=purchase_order_id,
        actual_delivery_date=as_of_date,
    )
    repos.fulfillment.add_delivery_line(
        delivery_id=delivery["id"],
        purchase_order_line_id=line_id,
        delivered_quantity=delivered_qty,
    )
    return delivery


def _seed_shipment(repos, purchase_order_id, actual_delivery_date: date, recorded_at: date):
    delivery_rows = repos.fulfillment.list_deliveries_for_purchase_order(purchase_order_id)
    if delivery_rows:
        delivery = delivery_rows[0]
    else:
        delivery = repos.fulfillment.add_delivery(
            delivery_number=f"DELIV-{purchase_order_id}", purchase_order_id=purchase_order_id
        )
    repos.fulfillment.add_shipment(
        shipment_number=f"SHIP-{purchase_order_id}",
        delivery_id=delivery["id"],
        recorded_at=datetime.combine(recorded_at, datetime.min.time()),
        actual_delivery_date=actual_delivery_date,
        expected_delivery_date=_REQUESTED_DELIVERY_DATE,
    )


def _seed_shortage_dispute_scenario(
    repos,
    po_number: str = "ORD-DSP",
    *,
    delivered_qty: float = 90.0,
    charge_date: date = date(2026, 6, 12),
    claimed_amount: float = 80.0,
    rate: float = 5.0,
    actual_penalty_amount: float | None = None,
):
    """Real, final shortfall = order_qty - delivered_qty = 10 units; at
    rate=5.0/unit that computes to $50 -- callers vary `claimed_amount` to
    land in whichever verdict branch they're testing. `actual_penalty_amount`
    defaults to `claimed_amount` (existing callers rely on the two being
    identical); pass it explicitly to seed an `actual_penalty` charge that
    differs from the dispute's own `claimed_amount`. Returns
    (purchase_order_id, actual_penalty_id)."""
    purchase_order_id, line_id, _retailer_id = _seed_order(
        repos, po_number, violation_type="SHORT_SHIP", rate=rate
    )
    _seed_delivery(repos, purchase_order_id, line_id, delivered_qty, charge_date)

    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number=f"AP-{po_number}",
        purchase_order_id=purchase_order_id,
        violation_type="SHORT_SHIP",
        actual_penalty_amount=(
            actual_penalty_amount if actual_penalty_amount is not None else claimed_amount
        ),
        invoice_or_deduction_date=charge_date,
    )
    return purchase_order_id, actual_penalty["id"]


def _seed_retailer_agreement_with_window(
    repos, db_session, retailer_id, dispute_window_days: int = 30
) -> dict:
    """Create a retailer agreement effective in the past, with `dispute_window_days` set,
    for the given retailer. `add_retailer_agreement` doesn't take `dispute_window_days`
    directly (it's set post-insert here, against the same session `repos` shares), since
    wiring it through the repository's write path is outside this task's scope."""
    agreement = repos.retailer_agreements.add_retailer_agreement(
        retailer_id=retailer_id,
        contract_code=f"TEST-WINDOW-{retailer_id}",
        title="Test retailer agreement with dispute window",
        document_sha256=hashlib.sha256(f"test-window-agreement:{retailer_id}".encode()).hexdigest(),
        effective_date=date(2025, 1, 1),
    )
    row = db_session.get(RetailerAgreement, agreement["id"])
    row.dispute_window_days = dispute_window_days
    db_session.flush()
    agreement["dispute_window_days"] = dispute_window_days
    return agreement


# ---------------------------------------------------------------------------
# open_dispute
# ---------------------------------------------------------------------------


def test_open_dispute_success(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)

    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0, notes="looks high")

    assert dispute["dispute_status"] == "OPEN"
    assert dispute["reason_code"] == "AMOUNT_INCORRECT"
    assert dispute["claimed_amount"] == 80.0
    assert dispute["verdict"] is None
    assert dispute["dispute_number"].startswith("DSP-")


def test_open_dispute_unknown_actual_penalty_raises_not_found(repos):
    from uuid import uuid4

    service = _build_service(repos)
    with pytest.raises(NotFoundError):
        service.open_dispute(uuid4(), "OTHER", 10.0)


def test_open_dispute_conflicts_with_existing_active_dispute(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)
    service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)

    with pytest.raises(ConflictError):
        service.open_dispute(actual_penalty_id, "OTHER", 80.0)


def test_open_dispute_allowed_again_once_prior_is_terminal(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)
    first = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)
    service.analyze(first["id"])
    service.resolve(first["id"], resolved_by="ops@mars.test")

    second = service.open_dispute(actual_penalty_id, "OTHER", 90.0)
    assert second["id"] != first["id"]
    assert second["dispute_status"] == "OPEN"


def test_open_dispute_sets_response_due_date_from_default_window(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)

    dispute = service.open_dispute(actual_penalty_id, "NOT_LATE", 100.0, now_date=date(2026, 1, 1))

    assert dispute["response_due_date"] == date(2026, 1, 1) + timedelta(days=90)


def test_open_dispute_uses_retailer_agreement_dispute_window_when_set(repos, db_session):
    purchase_order_id, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    retailer_id = repos.purchase_orders.require_purchase_order(purchase_order_id)["retailer_id"]
    retailer_agreement = _seed_retailer_agreement_with_window(
        repos, db_session, retailer_id, dispute_window_days=45
    )
    service = _build_service(repos)

    dispute = service.open_dispute(actual_penalty_id, "NOT_LATE", 100.0, now_date=date(2026, 1, 1))

    assert dispute["response_due_date"] == date(2026, 1, 1) + timedelta(
        days=retailer_agreement["dispute_window_days"]
    )


# ---------------------------------------------------------------------------
# analyze -- shortage
# ---------------------------------------------------------------------------


def test_analyze_pay_partial_when_retailer_overcharged(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos, claimed_amount=80.0)
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)

    analyzed = service.analyze(dispute["id"])

    assert analyzed["dispute_status"] == "ANALYZED"
    assert analyzed["computed_amount"] == 50.0  # 10 shortfall units x $5/unit
    assert analyzed["delta_amount"] == 30.0
    assert analyzed["verdict"] == "PAY_PARTIAL"
    assert analyzed["rule_id"] is not None
    assert analyzed["analyzed_at"] is not None
    assert analyzed["analysis_breakdown"]["violation_family"] == "SHORTAGE"
    assert analyzed["analysis_breakdown"]["facts"]["delivered_qty"] == 90.0
    assert analyzed["analysis_breakdown"]["facts"]["shortfall_units"] == 10.0


def test_analyze_uses_dispute_claimed_amount_not_actual_penalty_amount(repos):
    """Regression for the bug where `analyze()` recomputed against
    `actual_penalty["actual_penalty_amount"]` instead of the dispute's own
    `claimed_amount` -- `open_dispute()` lets the two diverge (the
    disputer's recorded claim need not match the original charge), and the
    verdict must be adjudicated against what the disputer actually claimed.

    `actual_penalty_amount=20.0` would round-trip to PAY_FULL (delta -30)
    if the bug regressed; the dispute's real `claimed_amount=80.0` must
    instead produce PAY_PARTIAL (delta +30) against the $50 computed
    shortfall charge."""
    _, actual_penalty_id = _seed_shortage_dispute_scenario(
        repos, claimed_amount=80.0, actual_penalty_amount=20.0
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)

    analyzed = service.analyze(dispute["id"])

    assert analyzed["computed_amount"] == 50.0  # 10 shortfall units x $5/unit
    assert analyzed["delta_amount"] == 30.0
    assert analyzed["verdict"] == "PAY_PARTIAL"
    assert analyzed["analysis_breakdown"]["claimed_amount"] == 80.0


def test_analyze_no_pay_when_no_real_shortfall(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos, delivered_qty=100.0, claimed_amount=200.0)
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "QTY_CONFIRMED", 200.0)

    analyzed = service.analyze(dispute["id"])

    assert analyzed["verdict"] == "NO_PAY"
    assert analyzed["computed_amount"] == 0.0


def test_analyze_pay_full_undercharge_records_negative_delta(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos, delivered_qty=90.0, claimed_amount=20.0)
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 20.0)

    analyzed = service.analyze(dispute["id"])

    assert analyzed["verdict"] == "PAY_FULL"
    assert analyzed["computed_amount"] == 50.0
    assert analyzed["delta_amount"] == -30.0


def test_analyze_raises_no_matching_rule_when_no_rule_effective_on_charge_date(repos):
    _purchase_order_id, actual_penalty_id = _seed_shortage_dispute_scenario(
        repos, charge_date=date(2025, 1, 1)
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)

    with pytest.raises(BusinessRuleError) as exc_info:
        service.analyze(dispute["id"])
    assert exc_info.value.code == "NO_MATCHING_RULE_FOR_DISPUTE"

    # No partial write -- the dispute is left completely untouched, still OPEN.
    unchanged = service.get(dispute["id"])
    assert unchanged["dispute_status"] == "OPEN"
    assert unchanged["computed_amount"] is None


def test_analyze_raises_insufficient_data_when_no_delivery_recorded(repos):
    purchase_order_id, _line_id, _retailer_id = _seed_order(
        repos, "ORD-DSP-NODATA", violation_type="SHORT_SHIP"
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-NODATA",
        purchase_order_id=purchase_order_id,
        violation_type="SHORT_SHIP",
        actual_penalty_amount=50.0,
        invoice_or_deduction_date=date(2026, 6, 12),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", 50.0)

    with pytest.raises(BusinessRuleError) as exc_info:
        service.analyze(dispute["id"])
    assert exc_info.value.code == "INSUFFICIENT_DATA_FOR_DISPUTE"

    unchanged = service.get(dispute["id"])
    assert unchanged["dispute_status"] == "OPEN"


def test_analyze_refuses_once_terminal(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)
    service.analyze(dispute["id"])
    service.resolve(dispute["id"], resolved_by="ops@mars.test")

    with pytest.raises(ValidationError):
        service.analyze(dispute["id"])


# ---------------------------------------------------------------------------
# analyze -- delay + grace period + TIERED-delay unsupported
# ---------------------------------------------------------------------------


def test_analyze_delay_pay_full_undercharge(repos):
    purchase_order_id, _line_id, _retailer_id = _seed_order(
        repos, "ORD-DSP-DELAY", violation_type="OTIF_LATE", calc_type="PER_UNIT", rate=3.0
    )
    _seed_shipment(
        repos, purchase_order_id, actual_delivery_date=date(2026, 6, 15), recorded_at=date(2026, 6, 15)
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-DELAY",
        purchase_order_id=purchase_order_id,
        violation_type="OTIF_LATE",
        actual_penalty_amount=200.0,
        invoice_or_deduction_date=date(2026, 6, 20),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", 200.0)

    analyzed = service.analyze(dispute["id"])

    assert analyzed["verdict"] == "PAY_FULL"
    assert analyzed["computed_amount"] == 300.0  # 100 units x $3/unit, no grace
    assert analyzed["delta_amount"] == -100.0
    assert analyzed["analysis_breakdown"]["violation_family"] == "DELAY"
    assert analyzed["analysis_breakdown"]["facts"]["is_late"] is True


def test_analyze_delay_no_pay_when_within_grace_period(repos):
    purchase_order_id, _line_id, _retailer_id = _seed_order(
        repos,
        "ORD-DSP-GRACE",
        violation_type="OTIF_LATE",
        calc_type="FLAT_FEE",
        rate=750.0,
        grace_period_days=3,
    )
    # 2 days late, within the 3-day grace period.
    _seed_shipment(
        repos, purchase_order_id, actual_delivery_date=date(2026, 6, 12), recorded_at=date(2026, 6, 12)
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-GRACE",
        purchase_order_id=purchase_order_id,
        violation_type="OTIF_LATE",
        actual_penalty_amount=750.0,
        invoice_or_deduction_date=date(2026, 6, 15),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "NOT_LATE", 750.0)

    analyzed = service.analyze(dispute["id"])

    assert analyzed["verdict"] == "NO_PAY"
    assert analyzed["computed_amount"] == 0.0
    assert analyzed["analysis_breakdown"]["facts"]["is_late"] is False


def test_analyze_delay_applies_per_day_accrues_same_as_projection(repos):
    """Regression: `_to_rule_value` used to drop `applies_per`/`basis_type`
    from the rule row, so a `DAY`-accrued rule recomputed in dispute as a
    single flat application instead of accruing per day late, same as
    `ProjectionService`'s `price_delay_penalty` call would for identical
    facts. 5 days late x $3/unit x 100 units accrues to $1500; a flat
    application would wrongly land on $300."""
    purchase_order_id, _line_id, retailer_id = _seed_order(
        repos,
        "ORD-DSP-PERDAY",
        violation_type="OTIF_LATE",
        calc_type="PER_UNIT",
        rate=3.0,
        applies_per=APPLIES_PER_DAY,
    )
    # 5 days late: requested 2026-06-10, delivered 2026-06-15, no grace period.
    _seed_shipment(
        repos, purchase_order_id, actual_delivery_date=date(2026, 6, 15), recorded_at=date(2026, 6, 15)
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-PERDAY",
        purchase_order_id=purchase_order_id,
        violation_type="OTIF_LATE",
        actual_penalty_amount=1500.0,
        invoice_or_deduction_date=date(2026, 6, 20),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", 1500.0)

    analyzed = service.analyze(dispute["id"])

    rule_value = repos.penalty_rules.list_rules_for_retailer(retailer_id)[0]
    expected_amount = price_delay_penalty(rule_value, _ORDER_QTY, _UNIT_PRICE, days_late=5)
    assert expected_amount == 1500.0  # sanity: matches what Projection would price
    assert analyzed["computed_amount"] == expected_amount
    assert analyzed["verdict"] == "PAY_FULL"


def test_analyze_prices_tiered_delay_rule_via_days_late_bands(repos):
    """A TIERED delay rule bands directly on days_late (tier_basis=DAYS_LATE): [0, 5) at
    1%, [5, None) at 3% of order value. Required 2026-06-10, actual 2026-06-20, no grace
    period -> 10 days late, landing in the open top band: 0.03 x (100 x $10.00) = $30,
    against a $100 claim -> PAY_PARTIAL."""
    retailer = repos.master_data.add_retailer("RET-DSP-TIERDELAY", "Dispute Test Retailer", None, "SUM")
    material = repos.master_data.add_material("MAT-DSP-TIERDELAY", None)
    plant = repos.master_data.add_plant("PLANT-DSP-TIERDELAY", None, None)
    repos.penalty_rules.add_rule(
        rule_code="RULE-DSP-TIERDELAY",
        violation_type="OTIF_LATE",
        penalty_category="OTIF_LATE",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="TIERED",
        rate=0.0,
        effective_start_date=date(2026, 1, 1),
        tiers=[
            {"band_min": 0.0, "band_max": 5.0, "rate": 0.01, "tier_basis": "DAYS_LATE"},
            {"band_min": 5.0, "band_max": None, "rate": 0.03, "tier_basis": "DAYS_LATE"},
        ],
    )
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-DSP-TIERDELAY",
        retailer_id=retailer["id"],
        order_date=date(2026, 5, 1),
        requested_delivery_date=_REQUESTED_DELIVERY_DATE,
        required_ship_date=date(2026, 6, 8),
        order_status="DELIVERED",
    )
    purchase_order_id = purchase_order["id"]
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order_id,
        line_number="10",
        ordered_quantity=_ORDER_QTY,
        unit_price=_UNIT_PRICE,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    _seed_shipment(
        repos, purchase_order_id, actual_delivery_date=date(2026, 6, 20), recorded_at=date(2026, 6, 20)
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-TIERDELAY",
        purchase_order_id=purchase_order_id,
        violation_type="OTIF_LATE",
        actual_penalty_amount=100.0,
        invoice_or_deduction_date=date(2026, 6, 25),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", 100.0)

    analyzed = service.analyze(dispute["id"])

    assert analyzed["computed_amount"] == 30.0
    assert analyzed["verdict"] == "PAY_PARTIAL"


# ---------------------------------------------------------------------------
# resolve / override
# ---------------------------------------------------------------------------


def test_resolve_requires_analyzed_status(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)

    with pytest.raises(ValidationError):
        service.resolve(dispute["id"], resolved_by="ops@mars.test")


def test_resolve_accepts_engine_verdict(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)
    service.analyze(dispute["id"])

    resolved = service.resolve(dispute["id"], resolved_by="ops@mars.test")

    assert resolved["dispute_status"] == "RESOLVED"
    assert resolved["resolved_by"] == "ops@mars.test"
    assert resolved["resolved_at"] is not None
    assert resolved["override_verdict"] is None


def test_resolve_override_requires_reason(repos):
    _, actual_penalty_id = _seed_shortage_dispute_scenario(repos)
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty_id, "AMOUNT_INCORRECT", 80.0)
    service.analyze(dispute["id"])

    with pytest.raises(ValidationError):
        service.resolve(dispute["id"], resolved_by="ops@mars.test", override_verdict="PAY_FULL")


def test_full_lifecycle_open_analyze_resolve_with_override(repos):
    """Open -> analyze -> resolve with override -> assert final state and
    audit fields (the required end-to-end flow)."""
    purchase_order_id, actual_penalty_id = _seed_shortage_dispute_scenario(repos, claimed_amount=80.0)
    service = _build_service(repos)

    dispute = service.open_dispute(
        actual_penalty_id, "AMOUNT_INCORRECT", 80.0, notes="retailer overbilled us"
    )
    assert dispute["dispute_status"] == "OPEN"

    analyzed = service.analyze(dispute["id"])
    assert analyzed["dispute_status"] == "ANALYZED"
    assert analyzed["verdict"] == "PAY_PARTIAL"
    assert analyzed["computed_amount"] == 50.0
    assert analyzed["delta_amount"] == 30.0

    overridden = service.resolve(
        dispute["id"],
        resolved_by="ops-lead@mars.test",
        override_verdict="PAY_FULL",
        override_reason="Ops discretion: retailer relationship priority outweighs the $30 dispute.",
    )

    assert overridden["dispute_status"] == "OVERRIDDEN"
    assert overridden["override_verdict"] == "PAY_FULL"
    assert overridden["override_reason"]
    assert overridden["resolved_by"] == "ops-lead@mars.test"
    assert overridden["resolved_at"] is not None
    # The deterministic engine's own verdict/amounts are preserved
    # unchanged as the audit trail -- override records a human decision on
    # top of it, it does not erase it.
    assert overridden["verdict"] == "PAY_PARTIAL"
    assert overridden["computed_amount"] == 50.0

    final = service.get(dispute["id"])
    assert final["dispute_status"] == "OVERRIDDEN"
    assert final["purchase_order_id"] == purchase_order_id

    listed = service.list_for_purchase_order(purchase_order_id)
    assert [d["id"] for d in listed] == [dispute["id"]]


# ---------------------------------------------------------------------------
# claim_facts (QUALITY dispatch, write-once, VOLUME_COMMITMENT authority split)
# ---------------------------------------------------------------------------


def _seed_quality_rule_and_po(repos, po_number: str = "ORD-DSP-QUALITY", rate: float = 4.0):
    """Seeds a PO with one QUALITY-family rule; no delivery/shipment facts (unneeded: QUALITY
    dispatch reads claim_facts and the PO's own order value, never delivered_qty/
    actual_delivery_date). Returns purchase_order_id."""
    retailer = repos.master_data.add_retailer(f"RET-{po_number}", "Dispute Test Retailer", None, "SUM")
    material = repos.master_data.add_material(f"MAT-{po_number}", None)
    plant = repos.master_data.add_plant(f"PLANT-{po_number}", None, None)
    repos.penalty_rules.add_rule(
        rule_code=f"RULE-{po_number}",
        violation_type="QUALITY_DEFECT",
        penalty_category="QUALITY_DEFECT_CHARGEBACK",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="PER_UNIT",
        rate=rate,
        engine_family="QUALITY",
        effective_start_date=date(2026, 1, 1),
    )
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=po_number,
        retailer_id=retailer["id"],
        order_date=date(2026, 5, 1),
        requested_delivery_date=_REQUESTED_DELIVERY_DATE,
        required_ship_date=date(2026, 6, 8),
        order_status="DELIVERED",
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=_ORDER_QTY,
        unit_price=_UNIT_PRICE,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    return purchase_order["id"]


def test_analyze_quality_defect_chargeback_recomputes_from_claim_defect_units(repos):
    """Acceptance criterion: a QUALITY_DEFECT_CHARGEBACK claim with defect_units and a
    PER_UNIT rule recomputes to defect_units * rate and classifies correctly against the
    claimed amount."""
    purchase_order_id = _seed_quality_rule_and_po(repos, rate=4.0)
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-QUALITY-OK",
        purchase_order_id=purchase_order_id,
        violation_type="QUALITY_DEFECT",
        actual_penalty_amount=48.0,
        invoice_or_deduction_date=date(2026, 6, 12),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(
        actual_penalty["id"], "AMOUNT_INCORRECT", 48.0, claim_facts={"defect_units": 12}
    )

    analyzed = service.analyze(dispute["id"])

    assert analyzed["computed_amount"] == 48.0  # 12 defect_units x $4/unit
    assert analyzed["verdict"] == "PAY_FULL"
    assert analyzed["analysis_breakdown"]["violation_family"] == "QUALITY"
    assert analyzed["analysis_breakdown"]["claim_supplied_keys"] == ["defect_units"]
    assert analyzed["analysis_breakdown"]["facts"]["defect_units"] == 12


def test_analyze_quality_missing_defect_units_raises_insufficient_data_with_no_write(repos):
    """A claim missing a required fact for its family raises
    InsufficientDataForDisputeError and leaves the dispute OPEN, with no write."""
    purchase_order_id = _seed_quality_rule_and_po(repos, rate=4.0)
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-QUALITY-MISSING",
        purchase_order_id=purchase_order_id,
        violation_type="QUALITY_DEFECT",
        actual_penalty_amount=48.0,
        invoice_or_deduction_date=date(2026, 6, 12),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", 48.0)  # no claim_facts

    with pytest.raises(BusinessRuleError) as exc_info:
        service.analyze(dispute["id"])
    assert exc_info.value.code == "INSUFFICIENT_DATA_FOR_DISPUTE"

    unchanged = service.get(dispute["id"])
    assert unchanged["dispute_status"] == "OPEN"
    assert unchanged["computed_amount"] is None
    assert unchanged["analysis_breakdown"] is None


def test_set_claim_facts_is_write_once(repos):
    """Any code path writing claim_facts a second time on the same row is refused, not just
    the open_dispute() call path."""
    purchase_order_id = _seed_quality_rule_and_po(repos)
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-QUALITY-WO",
        purchase_order_id=purchase_order_id,
        violation_type="QUALITY_DEFECT",
        actual_penalty_amount=48.0,
        invoice_or_deduction_date=date(2026, 6, 12),
    )
    repos.actual_penalties.set_claim_facts(actual_penalty["id"], {"defect_units": 12})

    with pytest.raises(ValueError):
        repos.actual_penalties.set_claim_facts(actual_penalty["id"], {"defect_units": 99})


def test_open_dispute_rejects_claim_facts_already_set(repos):
    purchase_order_id = _seed_quality_rule_and_po(repos)
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-QUALITY-DUP",
        purchase_order_id=purchase_order_id,
        violation_type="QUALITY_DEFECT",
        actual_penalty_amount=48.0,
        invoice_or_deduction_date=date(2026, 6, 12),
    )
    repos.actual_penalties.set_claim_facts(actual_penalty["id"], {"defect_units": 12})
    service = _build_service(repos)

    with pytest.raises(ConflictError):
        service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", 48.0, claim_facts={"defect_units": 99})


def test_analyze_volume_commitment_ignores_claim_supplied_purchase_total(repos):
    """Acceptance criterion: a volume-commitment dispute recomputes from
    PurchaseOrderRepository's own purchase-history aggregation, ignoring any purchase
    total supplied in claim_facts even if one is present and different -- proven here via
    a raw dict passed straight to the service, bypassing the ClaimFacts schema's
    extra="forbid" guard entirely, so the service/engine layer's own enforcement of
    decision #2/4f is what is actually under test."""
    retailer = repos.master_data.add_retailer("RET-DSP-VC", "Dispute Test Retailer", None, "SUM")
    agreement = repos.retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code="TEST-DSP-VC",
        title="Volume commitment test agreement",
        document_sha256="deadbeef" * 8,
        effective_date=date(2026, 1, 1),
    )
    repos.penalty_rules.add_rule(
        rule_code="RULE-DSP-VC",
        violation_type="VOLUME_SHORTFALL",
        penalty_category="MINIMUM_VOLUME_SHORTFALL",
        retailer_agreement_id=agreement["id"],
        calc_type="PER_UNIT",
        rate=2.0,
        engine_family="VOLUME_COMMITMENT",
        effective_start_date=date(2026, 1, 1),
        measurement_window_type="ROLLING",
        measurement_window_length=1,
        measurement_window_unit="YEARS",
        commitment_quantity=1000.0,
    )
    material = repos.master_data.add_material("MAT-DSP-VC", None)
    plant = repos.master_data.add_plant("PLANT-DSP-VC", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-DSP-VC",
        retailer_id=retailer["id"],
        order_date=date(2026, 3, 1),
        requested_delivery_date=date(2026, 3, 10),
        required_ship_date=date(2026, 3, 8),
        order_status="DELIVERED",
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=700,
        unit_price=_UNIT_PRICE,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-ORD-DSP-VC",
        purchase_order_id=purchase_order["id"],
        violation_type="VOLUME_SHORTFALL",
        actual_penalty_amount=600.0,
        invoice_or_deduction_date=date(2026, 6, 1),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(
        actual_penalty["id"],
        "AMOUNT_INCORRECT",
        600.0,
        claim_facts={"actual_purchase_quantity": 999999.0},
    )

    analyzed = service.analyze(dispute["id"])

    # committed 1000 - actual 700 (this PO's own order_qty, the only purchase in the
    # rolling window) = 300 shortfall units x $2/unit = $600, never the claim's 999999.
    assert analyzed["computed_amount"] == 600.0
    assert analyzed["verdict"] == "PAY_FULL"
    assert analyzed["analysis_breakdown"]["violation_family"] == "VOLUME_COMMITMENT"
    assert analyzed["analysis_breakdown"]["claim_supplied_keys"] == []
    assert analyzed["analysis_breakdown"]["facts"]["actual_purchase_quantity"] == 700.0
    assert analyzed["analysis_breakdown"]["facts"]["committed_quantity"] == 1000.0
