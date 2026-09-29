"""Shared scenario definitions for the four worked examples, so tests and seed data read from one source of truth."""

from datetime import date
from typing import TypedDict

from app.services.penalties.projection import (
    AppointmentStatus,
    CalcType,
    OrderSnapshot,
    PenaltyRule,
    ProductionStatus,
)


class _BaseSnapshotFields(TypedDict):
    """The OrderSnapshot fields shared by every day in one worked scenario."""

    order_id: str
    order_qty: int
    unit_price: float
    requested_delivery_date: date
    required_ship_date: date
    carrier_reliability_score: float
    expected_transit_days: int


WMT_RULES = [
    PenaltyRule(
        "RULE-WMT-SHORT", "SHORT_SHIP", CalcType.PER_UNIT, rate=2.0, threshold_pct=0.02, cap_amount=5000
    ),
    PenaltyRule("RULE-WMT-OTIF", "OTIF_LATE", CalcType.PERCENT_OF_PO, rate=0.03, cap_amount=5000),
]
AMZ_RULES = [
    PenaltyRule(
        "RULE-AMZ-FILL", "FILL_RATE", CalcType.PERCENT_OF_PO, rate=0.02, threshold_pct=0.03, cap_amount=3000
    ),
    PenaltyRule("RULE-AMZ-OTIF", "OTIF_LATE", CalcType.FLAT_FEE, rate=500.0, cap_amount=500),
]

# ---------------------------------------------------------------------
# WMT-100234: 10-day process, order Aug1, required ship Aug9, delivery Aug11
# ---------------------------------------------------------------------
_req_delivery = date(2026, 8, 11)
_req_ship = date(2026, 8, 9)
_base: _BaseSnapshotFields = {
    "order_id": "WMT-100234",
    "order_qty": 2000,
    "unit_price": 18.0,
    "requested_delivery_date": _req_delivery,
    "required_ship_date": _req_ship,
    "carrier_reliability_score": 92.0,
    "expected_transit_days": 2,
}

wmt_days = [
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 2),
            confirmed_qty=2000,
            production_status=ProductionStatus.ON_TRACK,
            **_base,
        ),
        "baseline: full confirmation, production on track",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 3),
            confirmed_qty=2000,
            production_status=ProductionStatus.ON_TRACK,
            **_base,
        ),
        "flat, nothing new",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 4),
            confirmed_qty=2000,
            production_status=ProductionStatus.AT_RISK,
            **_base,
        ),
        "raw material shortage flagged, production AT_RISK",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 5),
            confirmed_qty=1850,
            production_status=ProductionStatus.AT_RISK,
            **_base,
        ),
        "SAP confirms a real cut: 1,850 of 2,000",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 6),
            confirmed_qty=1850,
            production_status=ProductionStatus.AT_RISK,
            **_base,
        ),
        "flat, no new information",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 7),
            confirmed_qty=1925,
            production_status=ProductionStatus.AT_RISK,
            **_base,
        ),
        "partial recovery: 1,925 of 2,000",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 8),
            confirmed_qty=2000,
            production_status=ProductionStatus.ON_TRACK,
            **_base,
        ),
        "full recovery: 2,000 confirmed again",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 9),
            confirmed_qty=2000,
            production_status=ProductionStatus.ON_TRACK,
            appointment_status=AppointmentStatus.MISSED,
            **_base,
        ),
        "ship date; carrier misses the dock appointment",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 10),
            confirmed_qty=2000,
            production_status=ProductionStatus.ON_TRACK,
            appointment_status=AppointmentStatus.MISSED,
            actual_ship_date=date(2026, 8, 10),
            **_base,
        ),
        "shipment departs, confirmed 1 day late",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 11),
            confirmed_qty=2000,
            production_status=ProductionStatus.ON_TRACK,
            appointment_status=AppointmentStatus.MISSED,
            actual_ship_date=date(2026, 8, 10),
            **_base,
        ),
        "in transit, flat, awaiting arrival",
    ),
]

# ---------------------------------------------------------------------
# WMT-100511: 10-day process, order Aug5, required ship Aug13, delivery Aug15
# ---------------------------------------------------------------------
_req_delivery2 = date(2026, 8, 15)
_req_ship2 = date(2026, 8, 13)
_base2: _BaseSnapshotFields = {
    "order_id": "WMT-100511",
    "order_qty": 1500,
    "unit_price": 18.0,
    "requested_delivery_date": _req_delivery2,
    "required_ship_date": _req_ship2,
    "carrier_reliability_score": 92.0,
    "expected_transit_days": 2,
}

wmt2_days = [
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 6),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            **_base2,
        ),
        "baseline: fine",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 7),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            **_base2,
        ),
        "flat, nothing new",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 8),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            appointment_status=AppointmentStatus.RESCHEDULED,
            expected_ship_date=date(2026, 8, 14),
            **_base2,
        ),
        "DC reschedules dock appointment a day later",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 9),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            appointment_status=AppointmentStatus.RESCHEDULED,
            expected_ship_date=date(2026, 8, 14),
            **_base2,
        ),
        "reschedule still in effect, stage tightens",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 10),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            appointment_status=AppointmentStatus.SCHEDULED,
            **_base2,
        ),
        "Mars escalates, recovers the original slot",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 11),
            confirmed_qty=1440,
            production_status=ProductionStatus.AT_RISK,
            **_base2,
        ),
        "demand exception cuts allocation to 1,440",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 12),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            **_base2,
        ),
        "same-day extra production run fully recovers",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 13),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            actual_ship_date=date(2026, 8, 13),
            **_base2,
        ),
        "ship date; departs exactly on schedule",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 14),
            confirmed_qty=1500,
            production_status=ProductionStatus.ON_TRACK,
            actual_ship_date=date(2026, 8, 13),
            **_base2,
        ),
        "in transit, clean",
    ),
]

# ---------------------------------------------------------------------
# AMZ-778501: 12-day process, order Aug2, required ship Aug12, delivery Aug14
# ---------------------------------------------------------------------
_req_delivery3 = date(2026, 8, 14)
_req_ship3 = date(2026, 8, 12)
_base3: _BaseSnapshotFields = {
    "order_id": "AMZ-778501",
    "order_qty": 1200,
    "unit_price": 14.0,
    "requested_delivery_date": _req_delivery3,
    "required_ship_date": _req_ship3,
    "carrier_reliability_score": 78.0,
    "expected_transit_days": 2,
}

amz1_days = [
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 3),
            confirmed_qty=1200,
            production_status=ProductionStatus.ON_TRACK,
            **_base3,
        ),
        "baseline: full confirmation, on track",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 4),
            confirmed_qty=1200,
            production_status=ProductionStatus.ON_TRACK,
            demand_exception_flagged=True,
            **_base3,
        ),
        "demand exception flagged (regional spike), no cut yet",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 5),
            confirmed_qty=1200,
            production_status=ProductionStatus.AT_RISK,
            **_base3,
        ),
        "production flips AT_RISK, still no SAP cut",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 6),
            confirmed_qty=1150,
            production_status=ProductionStatus.AT_RISK,
            **_base3,
        ),
        "ATP reruns: first real cut, 1,150 of 1,200",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 7),
            confirmed_qty=1080,
            production_status=ProductionStatus.BEHIND,
            **_base3,
        ),
        "worsens to 1,080; status escalates to BEHIND",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 8),
            confirmed_qty=1080,
            production_status=ProductionStatus.BEHIND,
            **_base3,
        ),
        "plateau, no new information",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 9),
            confirmed_qty=1080,
            production_status=ProductionStatus.BEHIND,
            **_base3,
        ),
        "plateau, no new information",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 10),
            confirmed_qty=1080,
            production_status=ProductionStatus.BEHIND,
            **_base3,
        ),
        "plateau, no new information",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 11),
            confirmed_qty=1080,
            production_status=ProductionStatus.BEHIND,
            **_base3,
        ),
        "days-to-delivery band crosses; probability rises with zero new information",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 12),
            confirmed_qty=1080,
            production_status=ProductionStatus.BEHIND,
            actual_ship_date=date(2026, 8, 12),
            **_base3,
        ),
        "ship date; Mars ships 1,080 as-is, on schedule",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 13),
            confirmed_qty=1080,
            production_status=ProductionStatus.BEHIND,
            actual_ship_date=date(2026, 8, 12),
            **_base3,
        ),
        "in transit, shortfall locked in",
    ),
]

# ---------------------------------------------------------------------
# AMZ-780112: 8-day process, order Aug10, required ship Aug16, delivery Aug18
# ---------------------------------------------------------------------
_req_delivery4 = date(2026, 8, 18)
_req_ship4 = date(2026, 8, 16)
_base4: _BaseSnapshotFields = {
    "order_id": "AMZ-780112",
    "order_qty": 900,
    "unit_price": 14.0,
    "requested_delivery_date": _req_delivery4,
    "required_ship_date": _req_ship4,
    "carrier_reliability_score": 78.0,
    "expected_transit_days": 2,
}

amz2_days = [
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 11),
            confirmed_qty=900,
            production_status=ProductionStatus.ON_TRACK,
            **_base4,
        ),
        "baseline: full confirmation, on track",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 12),
            confirmed_qty=820,
            production_status=ProductionStatus.AT_RISK,
            **_base4,
        ),
        "competing allocation issue cuts to 820 of 900",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 13),
            confirmed_qty=900,
            production_status=ProductionStatus.ON_TRACK,
            **_base4,
        ),
        "overnight production run fully recovers",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 14),
            confirmed_qty=900,
            production_status=ProductionStatus.BEHIND,
            **_base4,
        ),
        "QA hold flagged on the finished batch",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 15),
            confirmed_qty=900,
            production_status=ProductionStatus.BEHIND,
            **_base4,
        ),
        "QA hold persists; days-band also tightens",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 16),
            confirmed_qty=900,
            production_status=ProductionStatus.ON_TRACK,
            actual_ship_date=date(2026, 8, 16),
            **_base4,
        ),
        "ship date; QA clears just in time, ships complete",
    ),
    (
        OrderSnapshot(
            projection_date=date(2026, 8, 17),
            confirmed_qty=900,
            production_status=ProductionStatus.ON_TRACK,
            actual_ship_date=date(2026, 8, 16),
            **_base4,
        ),
        "in transit, clean",
    ),
]
