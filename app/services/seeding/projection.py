"""Penalty-projection seed data and scenario replay."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, TypedDict
from uuid import UUID

from app.core.exceptions import NotFoundError
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService
from app.services.penalties.projection import DELAY_VIOLATION_TYPES, SHORTAGE_VIOLATION_TYPES, PenaltyRule
from app.services.penalties.projection.service import ProjectionService
from app.services.seeding.master_data import ensure_placeholder_retailer_agreement
from app.services.seeding.scenario_data_projection import (
    AMZ_RULES,
    WMT_RULES,
    amz1_days,
    amz2_days,
    wmt2_days,
    wmt_days,
)

# Source-doc references stay out of the PenaltyRule dataclass, which is
# deliberately minimal, so they live here as seed-only metadata.
_SOURCE_DOC_REFERENCE = {
    "RULE-WMT-SHORT": "Walmart Supplier Manual v2026.1 (mock)",
    "RULE-WMT-OTIF": "Walmart Supplier Manual v2026.1 (mock)",
    "RULE-AMZ-FILL": "Amazon Vendor Central Chargeback Policy (mock)",
    "RULE-AMZ-OTIF": "Amazon Vendor Central Chargeback Policy (mock)",
}
# penalty_rule.penalty_category is NOT NULL; these worked-example rules were hand-authored
# before extraction existed and carry no genuine extracted category, so each is mapped onto
# the governed category matching its violation_type.
_PENALTY_CATEGORY_BY_VIOLATION_TYPE = {
    "SHORT_SHIP": "SHORT_SHIP",
    "FILL_RATE": "SHORT_SHIP",
    "OTIF_LATE": "OTIF_LATE",
    "ASN_LATE": "OTIF_LATE",
}


def _rule_to_seed_dict(rule: PenaltyRule, retailer_code: str) -> dict[str, Any]:
    """Convert a canonical `WMT_RULES`/`AMZ_RULES` rule into `add_rule` kwargs.

    Reusing the objects the pure-engine tests assert against keeps seed data and
    validated test numbers in step. `retailer_agreement_id` is resolved at `seed()` time.
    """
    return {
        "rule_code": rule.rule_id,
        "retailer_code": retailer_code,
        "violation_type": rule.violation_type,
        "penalty_category": _PENALTY_CATEGORY_BY_VIOLATION_TYPE[rule.violation_type],
        "calc_type": rule.calc_type.value,
        "rate": rule.rate,
        "threshold_pct": rule.threshold_pct,
        "cap_amount": rule.cap_amount,
        "source_doc_reference": _SOURCE_DOC_REFERENCE.get(rule.rule_id),
    }


_RULES = [_rule_to_seed_dict(r, "RET-WMT") for r in WMT_RULES] + [
    _rule_to_seed_dict(r, "RET-AMZ") for r in AMZ_RULES
]


def _day_before_first(days) -> date:
    """Order-date convention: one day before the scenario's first tracked projection day."""
    return days[0][0].projection_date - timedelta(days=1)


# Earliest date across all four scenarios. `seed()` shifts every literal
# Aug-2026 date in `_ORDERS` by `calendar_offset()`, so a fresh seed run
# re-anchors the fixed calendar around today instead of drifting into the past.
CALENDAR_BASE_DATE = date(2026, 8, 1)


def calendar_offset() -> timedelta:
    """Offset `seed()` applies to every `_ORDERS` date; exposed for tests asserting literal dates."""
    return date.today() - CALENDAR_BASE_DATE  # noqa: DTZ011 (wall-clock anchor by design)


class _OrderSeed(TypedDict):
    """One worked-example purchase order's seed fixture: header fields plus its single line."""

    purchase_order_number: str
    retailer_code: str
    material_code: str
    plant_code: str
    order_qty: int
    unit_price: float
    order_date: date
    requested_delivery_date: date
    required_ship_date: date
    order_status: str
    carrier_code: str


_ORDERS: list[_OrderSeed] = [
    {
        "purchase_order_number": "WMT-100234",
        "retailer_code": "RET-WMT",
        "material_code": "MAT-100234",
        "plant_code": "LOC-ATL",
        "order_qty": 2000,
        "unit_price": 18.0,
        "order_date": _day_before_first(wmt_days),
        "requested_delivery_date": wmt_days[0][0].requested_delivery_date,
        "required_ship_date": wmt_days[0][0].required_ship_date,
        "order_status": "OPEN",
        "carrier_code": "CAR-SWIFT",
    },
    {
        "purchase_order_number": "WMT-100511",
        "retailer_code": "RET-WMT",
        "material_code": "MAT-100511",
        "plant_code": "LOC-ATL",
        "order_qty": 1500,
        "unit_price": 18.0,
        "order_date": _day_before_first(wmt2_days),
        "requested_delivery_date": wmt2_days[0][0].requested_delivery_date,
        "required_ship_date": wmt2_days[0][0].required_ship_date,
        "order_status": "OPEN",
        "carrier_code": "CAR-SWIFT",
    },
    {
        "purchase_order_number": "AMZ-778501",
        "retailer_code": "RET-AMZ",
        "material_code": "MAT-100587",
        "plant_code": "LOC-COL",
        "order_qty": 1200,
        "unit_price": 14.0,
        "order_date": _day_before_first(amz1_days),
        "requested_delivery_date": amz1_days[0][0].requested_delivery_date,
        "required_ship_date": amz1_days[0][0].required_ship_date,
        "order_status": "OPEN",
        "carrier_code": "CAR-JBHUNT",
    },
    {
        "purchase_order_number": "AMZ-780112",
        "retailer_code": "RET-AMZ",
        "material_code": "MAT-100587",
        "plant_code": "LOC-COL",
        "order_qty": 900,
        "unit_price": 14.0,
        "order_date": _day_before_first(amz2_days),
        "requested_delivery_date": amz2_days[0][0].requested_delivery_date,
        "required_ship_date": amz2_days[0][0].required_ship_date,
        "order_status": "OPEN",
        "carrier_code": "CAR-JBHUNT",
    },
]
_SCENARIOS = [
    ("WMT-100234", wmt_days),
    ("WMT-100511", wmt2_days),
    ("AMZ-778501", amz1_days),
    ("AMZ-780112", amz2_days),
]

# Original (unshifted) order_date per order, exactly what `_ORDERS` was built
# from. `simulate_daily_run()` diffs this against the persisted order_date to
# recover the offset `seed()` applied, without calling date.today() again.
_ORIGINAL_ORDER_DATE: dict[str, date] = {
    purchase_order_number: _day_before_first(days) for purchase_order_number, days in _SCENARIOS
}


class _NegotiationCreateSpec(TypedDict):
    """When and how a PO delivery-change request is fired during an order's replay.

    `trigger_date` must match a `projection_date` already present in that order's
    own scenario days.
    """

    trigger_date: date
    reason_code: str
    proposed_delivery_date: date
    notes: str | None
    now: datetime


class _NegotiationRespondSpec(TypedDict):
    """The retailer's response, fired the day after `create`."""

    trigger_date: date
    decision: str
    countered_delivery_date: date | None
    now: datetime


class _NegotiationScenario(TypedDict, total=False):
    """One order's PO delivery-change-request outcome, as up to three optional beats.

    Exactly one of `respond`/`expire_as_of` is set per scenario.
    """

    create: _NegotiationCreateSpec
    respond: _NegotiationRespondSpec
    # Fired once the order's day-loop has finished rather than on a specific day:
    # AMZ-780112 is deliberately never responded to, so it must be swept instead.
    expire_as_of: datetime


# One outcome per order, covering all four retailer-response paths. Every
# `now`/`as_of` is anchored inside the fictional Aug-2026 calendar, never
# wall-clock, so timestamps stay chronologically correct against the replay.
_NEGOTIATION_SCENARIOS: dict[str, _NegotiationScenario] = {
    # ACCEPTED: SAP confirms the real production cut (1,850/2,000) on Aug 5;
    # Walmart accepts a 3-day extension the next day. current_delivery_date
    # shifts Aug 11 -> Aug 14, current_required_ship_date Aug 9 -> Aug 12 by the
    # same delta, so the Aug 9 appointment-miss day is re-projected against the
    # new required ship date rather than the original one.
    "WMT-100234": {
        "create": {
            "trigger_date": date(2026, 8, 5),
            "reason_code": "SHORTAGE",
            "proposed_delivery_date": date(2026, 8, 14),
            "notes": "SAP confirms a real cut to 1,850/2,000; requesting 3 extra days.",
            "now": datetime(2026, 8, 5, 9, 0),  # noqa: DTZ001 (naive by design)
        },
        "respond": {
            "trigger_date": date(2026, 8, 6),
            "decision": "ACCEPTED",
            "countered_delivery_date": None,
            "now": datetime(2026, 8, 6, 9, 0),  # noqa: DTZ001 (naive by design)
        },
    },
    # COUNTERED: DC reschedules the dock appointment a day later on Aug 8;
    # Walmart counters the requested Aug 19 with Aug 17 the next day
    # (strictly between baseline Aug 15 and proposed Aug 19).
    "WMT-100511": {
        "create": {
            "trigger_date": date(2026, 8, 8),
            "reason_code": "DELAY",
            "proposed_delivery_date": date(2026, 8, 19),
            "notes": "Dock appointment rescheduled a day later; requesting buffer.",
            "now": datetime(2026, 8, 8, 9, 0),  # noqa: DTZ001 (naive by design)
        },
        "respond": {
            "trigger_date": date(2026, 8, 9),
            "decision": "COUNTERED",
            "countered_delivery_date": date(2026, 8, 17),
            "now": datetime(2026, 8, 9, 9, 0),  # noqa: DTZ001 (naive by design)
        },
    },
    # REJECTED: the confirmed cut worsens to 1,080/1,200 and production
    # escalates to BEHIND on Aug 7, with no recovery for the rest of the
    # scenario; Amazon rejects the next day. No shadow tracking: the daily
    # projection continues against the original (unchanged) date.
    "AMZ-778501": {
        "create": {
            "trigger_date": date(2026, 8, 7),
            "reason_code": "SHORTAGE",
            "proposed_delivery_date": date(2026, 8, 18),
            "notes": "Confirmed cut worsens to 1,080/1,200; production status BEHIND.",
            "now": datetime(2026, 8, 7, 9, 0),  # noqa: DTZ001 (naive by design)
        },
        "respond": {
            "trigger_date": date(2026, 8, 8),
            "decision": "REJECTED",
            "countered_delivery_date": None,
            "now": datetime(2026, 8, 8, 9, 0),  # noqa: DTZ001 (naive by design)
        },
    },
    # EXPIRED/timeout: a QA hold is flagged on Aug 14, exactly 2 days before the
    # Aug 16 required ship date, right at the min_lead_days=2 boundary.
    # reason_code=OTHER, since a quality hold is neither a quantity shortage nor a
    # transit delay. Never responded to; Amazon's 24h SLA expires it at Aug 15
    # 09:00, so the recovery sweep runs at the end of the day-loop instead.
    "AMZ-780112": {
        "create": {
            "trigger_date": date(2026, 8, 14),
            "reason_code": "OTHER",
            "proposed_delivery_date": date(2026, 8, 21),
            "notes": "QA hold on finished batch; requesting buffer in case release slips",
            "now": datetime(2026, 8, 14, 9, 0),  # noqa: DTZ001 (naive by design)
        },
        "expire_as_of": datetime(2026, 8, 17, 9, 0),  # noqa: DTZ001 (naive by design)
    },
}


def _carrier_by_code(master_data: MasterDataRepository, carrier_code: str) -> dict:
    """Look up a carrier by code, scanning `list_carriers()`.

    `MasterDataRepository` exposes no code-based getter, and this demo-scale scan
    is the seed data's own concern rather than a repository method.
    """
    carrier = next((c for c in master_data.list_carriers() if c["carrier_code"] == carrier_code), None)
    if carrier is None:
        raise ValueError(f"Unknown carrier_code={carrier_code!r}. Call seed_master_data() first.")
    return carrier


def seed(
    rules: PenaltyRuleRepository,
    purchase_orders: PurchaseOrderRepository,
    fulfillment: FulfillmentRepository,
    master_data: MasterDataRepository,
    retailer_agreements: RetailerAgreementRepository,
) -> dict[str, int]:
    """Seed rules and worked-example orders, skipping any that already exist."""
    counts = {"rules": 0, "orders": 0}

    existing_rules = {r["rule_code"] for r in rules.list_rules()}
    for rule_dict in _RULES:
        if rule_dict["rule_code"] not in existing_rules:
            retailer_code = rule_dict["retailer_code"]
            retailer = master_data.get_retailer_by_code(retailer_code)
            assert retailer is not None, (
                f"Unknown retailer_code={retailer_code!r}. Call seed_master_data() first."
            )
            retailer_agreement_id = ensure_placeholder_retailer_agreement(retailer_agreements, retailer)
            fields = {k: v for k, v in rule_dict.items() if k != "retailer_code"}
            rules.add_rule(retailer_agreement_id=retailer_agreement_id, **fields)
            counts["rules"] += 1

    offset = calendar_offset()
    for o in _ORDERS:
        if purchase_orders.get_by_number(o["purchase_order_number"]) is not None:
            continue

        retailer = master_data.get_retailer_by_code(o["retailer_code"])
        material = master_data.get_material_by_code(o["material_code"])
        plant = master_data.get_plant_by_code(o["plant_code"])
        assert retailer is not None, (
            f"Unknown retailer_code={o['retailer_code']!r}. Call seed_master_data() first."
        )
        assert material is not None, (
            f"Unknown material_code={o['material_code']!r}. Call seed_master_data() first."
        )
        assert plant is not None, f"Unknown plant_code={o['plant_code']!r}. Call seed_master_data() first."

        purchase_order = purchase_orders.create_purchase_order(
            purchase_order_number=o["purchase_order_number"],
            retailer_id=retailer["id"],
            order_date=o["order_date"] + offset,
            requested_delivery_date=o["requested_delivery_date"] + offset,
            required_ship_date=o["required_ship_date"] + offset,
            order_status=o["order_status"],
        )
        purchase_orders.add_line(
            purchase_order_id=purchase_order["id"],
            line_number="10",
            ordered_quantity=o["order_qty"],
            unit_price=o["unit_price"],
            material_id=material["id"],
            plant_id=plant["id"],
        )
        # One delivery header per PO; every day's shipment update attaches to it.
        # Kept minimal: neither engine reads ship_from_plant_id or
        # ship_to_location_id.
        fulfillment.add_delivery(
            delivery_number=f"DELIV-{o['purchase_order_number']}",
            purchase_order_id=purchase_order["id"],
        )
        counts["orders"] += 1

    return counts


def simulate_daily_run(
    purchase_orders: PurchaseOrderRepository,
    fulfillment: FulfillmentRepository,
    master_data: MasterDataRepository,
    projection_service: ProjectionService,
    delivery_change_service: PoDeliveryChangeRequestService,
) -> list[dict]:
    """Walk all four scenarios day by day, writing facts and running projections.

    Marks each order DELIVERED after its final day. One delivery-change-request
    outcome is interleaved per order, at the days fixed by
    `_NEGOTIATION_SCENARIOS`, covering all four retailer-response paths. Each of
    those calls re-triggers `ProjectionService.run_for_purchase_order` itself, so
    no extra projection call is made here.
    """
    summaries = []
    for purchase_order_number, days in _SCENARIOS:
        purchase_order = purchase_orders.get_by_number(purchase_order_number)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=(
                    f"No purchase order found with purchase_order_number={purchase_order_number!r}. "
                    "Call seed_master_data() first."
                ),
            )

        purchase_order_id: UUID = purchase_order["id"]
        lines = purchase_orders.list_lines(purchase_order_id)
        line = lines[0]
        delivery = fulfillment.list_deliveries_for_purchase_order(purchase_order_id)[0]

        carrier_code = next(
            o["carrier_code"] for o in _ORDERS if o["purchase_order_number"] == purchase_order_number
        )
        carrier = _carrier_by_code(master_data, carrier_code)

        # Derived from what is persisted rather than a fresh date.today() call:
        # seed() and this call can land on different real days, so the offset must
        # be recovered from the order itself for the two to agree.
        offset = purchase_order["order_date"] - _ORIGINAL_ORDER_DATE[purchase_order_number]

        negotiation_scenario = _NEGOTIATION_SCENARIOS.get(purchase_order_number)
        negotiation_id: UUID | None = None
        negotiation_result: dict[str, Any] | None = None
        # Idempotent, same convention as seed(): a re-run must not
        # double-create a request or error retrying a terminal one. If this
        # order already has any negotiation history, skip re-firing and
        # surface the prior outcome instead.
        existing_negotiation_history = (
            delivery_change_service.list_history(purchase_order_id)
            if negotiation_scenario is not None
            else []
        )
        if existing_negotiation_history:
            negotiation_scenario = None
            negotiation_result = existing_negotiation_history[-1]

        daily_results = []
        for snapshot, note in days:
            # Every date flowing from scenario_data_projection.py into a repository
            # write or the projection call is shifted here, at the point of use.
            # The negotiation trigger-date comparisons below deliberately keep
            # comparing the original, unshifted snapshot.projection_date.
            shifted_date = snapshot.projection_date + offset
            confirmation = fulfillment.add_order_confirmation(
                confirmation_number=f"CONF-{purchase_order_number}-{shifted_date.isoformat()}",
                purchase_order_id=purchase_order_id,
                confirmation_date=datetime.combine(shifted_date, datetime.min.time()),
            )
            fulfillment.add_order_confirmation_line(
                order_confirmation_id=confirmation["id"],
                purchase_order_line_id=line["id"],
                confirmed_quantity=snapshot.confirmed_qty,
            )
            fulfillment.add_production_schedule(
                material_id=line["material_id"],
                plant_id=line["plant_id"],
                status=snapshot.production_status.value,
                status_at=datetime.combine(shifted_date, datetime.min.time()),
            )
            if snapshot.demand_exception_flagged:
                fulfillment.add_demand_exception(
                    exception_id=f"EXC-{purchase_order_number}-{shifted_date.isoformat()}",
                    purchase_order_line_id=line["id"],
                    flagged_date=shifted_date,
                )
            fulfillment.add_shipment(
                shipment_number=f"SHIP-{purchase_order_number}-{shifted_date.isoformat()}",
                delivery_id=delivery["id"],
                recorded_at=datetime.combine(shifted_date, datetime.min.time()),
                carrier_id=carrier["id"],
                expected_ship_date=(
                    snapshot.expected_ship_date + offset if snapshot.expected_ship_date else None
                ),
                actual_ship_date=(snapshot.actual_ship_date + offset if snapshot.actual_ship_date else None),
                appointment_status=snapshot.appointment_status.value,
                expected_transit_days=snapshot.expected_transit_days,
            )

            result = projection_service.run_for_purchase_order(purchase_order_id, shifted_date)
            # Per-model dollar breakdown, summed across however many shortage- or
            # delay-priced violations this retailer's rule set produces. Both the
            # raw (if-realized) and blended (probability-weighted) sums are
            # reported: a display surface must never show only the blended figure.
            shortage_penalty_amount = sum(
                v.penalty_amount for v in result.violations if v.violation_type in SHORTAGE_VIOLATION_TYPES
            )
            delay_penalty_amount = sum(
                v.penalty_amount for v in result.violations if v.violation_type in DELAY_VIOLATION_TYPES
            )
            shortage_expected_penalty_amount = sum(
                v.expected_penalty_amount
                for v in result.violations
                if v.violation_type in SHORTAGE_VIOLATION_TYPES
            )
            delay_expected_penalty_amount = sum(
                v.expected_penalty_amount
                for v in result.violations
                if v.violation_type in DELAY_VIOLATION_TYPES
            )
            daily_results.append(
                {
                    "projection_date": result.projection_date,
                    "note": note,
                    "shortage_penalty_amount": round(shortage_penalty_amount, 2),
                    "delay_penalty_amount": round(delay_penalty_amount, 2),
                    "shortage_expected_penalty_amount": round(shortage_expected_penalty_amount, 2),
                    "delay_expected_penalty_amount": round(delay_expected_penalty_amount, 2),
                    "total_expected_penalty_amount": result.total_expected_penalty_amount,
                    "shortage_probability": result.shortage_probability,
                    "delay_probability": result.delay_probability,
                }
            )

            if negotiation_scenario is not None:
                create_spec = negotiation_scenario.get("create")
                if (
                    create_spec is not None
                    and negotiation_id is None
                    and snapshot.projection_date == create_spec["trigger_date"]
                ):
                    created = delivery_change_service.create_request(
                        purchase_order_id=purchase_order_id,
                        reason_code=create_spec["reason_code"],
                        proposed_delivery_date=create_spec["proposed_delivery_date"] + offset,
                        notes=create_spec["notes"],
                        now=create_spec["now"] + offset,
                    )
                    negotiation_id = created["id"]

                respond_spec = negotiation_scenario.get("respond")
                if (
                    respond_spec is not None
                    and negotiation_id is not None
                    and negotiation_result is None
                    and snapshot.projection_date == respond_spec["trigger_date"]
                ):
                    negotiation_result = delivery_change_service.record_response(
                        delivery_change_request_id=negotiation_id,
                        decision=respond_spec["decision"],
                        countered_delivery_date=(
                            respond_spec["countered_delivery_date"] + offset
                            if respond_spec["countered_delivery_date"] is not None
                            else None
                        ),
                        now=respond_spec["now"] + offset,
                    )

        expire_as_of = None if negotiation_scenario is None else negotiation_scenario.get("expire_as_of")
        if expire_as_of is not None:
            expired = delivery_change_service.expire_stale(as_of=expire_as_of + offset)
            negotiation_result = next((row for row in expired if row["id"] == negotiation_id), None)

        purchase_orders.set_order_status(purchase_order_id, "DELIVERED")
        summary: dict[str, Any] = {"purchase_order_id": str(purchase_order_id), "days": daily_results}
        if negotiation_result is not None:
            summary["negotiation"] = negotiation_result
        summaries.append(summary)

    return summaries
