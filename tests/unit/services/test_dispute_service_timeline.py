"""Dispute analysis against a PO's fulfillment-timeline plans.

Covers the cases the single-plan dispute tests cannot: a PO split across several plans
(shipments), a cancelled plan that must not count, and an ASN_LATE charge, which is
judged on the ASN date against goods issue rather than on the delivery date.
"""

from datetime import date

from scripts.seed.seed_milestone_types import seed_milestone_types
from tests.unit.services.test_dispute_service import _build_service, _seed_order


def _seed_timeline_plan(
    repos, po_id, line_id, plan_number: str, *, shipped_quantity: float, milestone_actuals: dict
) -> dict:
    """One plan of a PO whose one line shipped `shipped_quantity`, with `{code: actual_date}` DONE."""
    plan = repos.fulfillment_timeline.create_plan(
        plan_number=plan_number, purchase_order_id=po_id, freight_term="PREPAID"
    )
    plan_line = repos.fulfillment_timeline.add_plan_line(
        plan["id"], line_id, planned_quantity=shipped_quantity
    )
    repos.fulfillment_timeline.set_plan_line_quantities(plan_line["id"], shipped_quantity=shipped_quantity)
    for code, actual in milestone_actuals.items():
        repos.fulfillment_timeline.upsert_milestone(
            plan["id"], code, baseline_date=actual, planned_date=actual, actual_date=actual, status="DONE"
        )
    return plan


def _open_and_analyze(repos, po_id, violation_type: str, amount: float, number: str) -> dict:
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number=number,
        purchase_order_id=po_id,
        violation_type=violation_type,
        actual_penalty_amount=amount,
        invoice_or_deduction_date=date(2026, 6, 20),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", amount)
    return service.analyze(dispute["id"])


def test_analyze_sums_shipped_quantity_across_split_plans(repos, db_session):
    seed_milestone_types(db_session)
    po_id, line_id, _retailer_id = _seed_order(repos, "ORD-TL-SPLIT", violation_type="SHORT_SHIP", rate=5.0)
    # Two shipments of one 100-unit PO that together deliver everything.
    _seed_timeline_plan(
        repos,
        po_id,
        line_id,
        "PLAN-SPLIT-A",
        shipped_quantity=60.0,
        milestone_actuals={"DELIVERED": date(2026, 6, 9)},
    )
    _seed_timeline_plan(
        repos,
        po_id,
        line_id,
        "PLAN-SPLIT-B",
        shipped_quantity=40.0,
        milestone_actuals={"DELIVERED": date(2026, 6, 10)},
    )

    analyzed = _open_and_analyze(repos, po_id, "SHORT_SHIP", 200.0, "AP-TL-SPLIT")

    # Judged on the PO's total delivery (100 of 100), not on the first plan's 60, which
    # would read as a 40-unit shortfall worth $200.
    assert analyzed["analysis_breakdown"]["facts"]["delivered_qty"] == 100.0
    assert analyzed["computed_amount"] == 0.0
    assert analyzed["verdict"] == "NO_PAY"
    assert analyzed["analysis_breakdown"]["actual_delivery_date"] == "2026-06-10"


def test_analyze_ignores_cancelled_plans(repos, db_session):
    seed_milestone_types(db_session)
    po_id, line_id, _retailer_id = _seed_order(repos, "ORD-TL-CANC", violation_type="SHORT_SHIP", rate=5.0)
    _seed_timeline_plan(
        repos,
        po_id,
        line_id,
        "PLAN-CANC-A",
        shipped_quantity=90.0,
        milestone_actuals={"DELIVERED": date(2026, 6, 9)},
    )
    cancelled = _seed_timeline_plan(
        repos, po_id, line_id, "PLAN-CANC-B", shipped_quantity=500.0, milestone_actuals={}
    )
    repos.fulfillment_timeline.set_plan_status(cancelled["id"], "CANCELLED")

    analyzed = _open_and_analyze(repos, po_id, "SHORT_SHIP", 50.0, "AP-TL-CANC")

    assert analyzed["analysis_breakdown"]["facts"]["delivered_qty"] == 90.0
    assert analyzed["computed_amount"] == 50.0  # 10 units short x $5


def test_analyze_prices_asn_late_from_asn_and_goods_issue_dates(repos, db_session):
    seed_milestone_types(db_session)
    po_id, line_id, _retailer_id = _seed_order(
        repos, "ORD-TL-ASN", violation_type="ASN_LATE", calc_type="FLAT_FEE", rate=250.0, grace_period_days=1
    )
    # Delivered on time, but the ASN went out 4 days after goods issue (1 day of grace).
    _seed_timeline_plan(
        repos,
        po_id,
        line_id,
        "PLAN-ASN",
        shipped_quantity=100.0,
        milestone_actuals={
            "GOODS_ISSUED": date(2026, 6, 5),
            "ASN_SENT": date(2026, 6, 9),
            "DELIVERED": date(2026, 6, 10),
        },
    )

    analyzed = _open_and_analyze(repos, po_id, "ASN_LATE", 250.0, "AP-TL-ASN")

    assert analyzed["analysis_breakdown"]["violation_family"] == "DELAY"
    assert analyzed["computed_amount"] == 250.0
    assert analyzed["verdict"] == "PAY_FULL"


def _charge_status(repos, po_id, number: str) -> str:
    charge = next(
        c
        for c in repos.actual_penalties.list_for_purchase_order(po_id)
        if c["actual_penalty_number"] == number
    )
    return charge["dispute_status"]


def test_charge_status_follows_the_dispute_lifecycle(repos, db_session):
    seed_milestone_types(db_session)
    po_id, line_id, _retailer_id = _seed_order(repos, "ORD-TL-STATUS", violation_type="SHORT_SHIP", rate=5.0)
    _seed_timeline_plan(
        repos,
        po_id,
        line_id,
        "PLAN-STATUS",
        shipped_quantity=100.0,
        milestone_actuals={"DELIVERED": date(2026, 6, 9)},
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-STATUS",
        purchase_order_id=po_id,
        violation_type="SHORT_SHIP",
        actual_penalty_amount=200.0,
        invoice_or_deduction_date=date(2026, 6, 20),
    )
    assert _charge_status(repos, po_id, "AP-STATUS") == "NONE"
    service = _build_service(repos)

    dispute = service.open_dispute(actual_penalty["id"], "QTY_CONFIRMED", 200.0)
    assert _charge_status(repos, po_id, "AP-STATUS") == "DISPUTED"

    service.analyze(dispute["id"])
    assert _charge_status(repos, po_id, "AP-STATUS") == "DISPUTED"  # analysis alone decides nothing

    service.resolve(dispute["id"], resolved_by="ops")  # engine verdict NO_PAY: delivered in full
    assert _charge_status(repos, po_id, "AP-STATUS") == "WAIVED"


def test_override_verdict_sets_the_charge_status(repos, db_session):
    seed_milestone_types(db_session)
    po_id, line_id, _retailer_id = _seed_order(repos, "ORD-TL-OVR", violation_type="SHORT_SHIP", rate=5.0)
    _seed_timeline_plan(
        repos,
        po_id,
        line_id,
        "PLAN-OVR",
        shipped_quantity=100.0,
        milestone_actuals={"DELIVERED": date(2026, 6, 9)},
    )
    actual_penalty = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-OVR",
        purchase_order_id=po_id,
        violation_type="SHORT_SHIP",
        actual_penalty_amount=200.0,
        invoice_or_deduction_date=date(2026, 6, 20),
    )
    service = _build_service(repos)
    dispute = service.open_dispute(actual_penalty["id"], "QTY_CONFIRMED", 200.0)
    service.analyze(dispute["id"])

    service.resolve(
        dispute["id"], resolved_by="ops", override_verdict="PAY_FULL", override_reason="Agreed with retailer"
    )

    assert _charge_status(repos, po_id, "AP-OVR") == "UPHELD"
