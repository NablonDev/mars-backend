"""Enums and data structures for the penalty-projection engine.

The engine's contract is one scalar `order_qty`/`unit_price` per snapshot
rather than a per-line list, and `order_id` carries a stringified
purchase-order UUID rather than a business key.

`PenaltyRule` and `PenaltyRuleTier` are the pure, framework-free counterparts
of the ORM models of the same name in `app.models.penalties.rule`, told apart
by import path rather than by renaming one side. `MitigationOption` follows
the same pattern (see `app/repositories/penalties/mitigation.py`).
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from uuid import UUID


class ProductionStatus(Enum):
    """Manufacturing progress toward the confirmed order quantity, feeding shortage risk."""

    ON_TRACK = "ON_TRACK"
    AT_RISK = "AT_RISK"
    BEHIND = "BEHIND"


class AppointmentStatus(Enum):
    """State of the retailer delivery appointment, feeding delay risk."""

    SCHEDULED = "SCHEDULED"
    RESCHEDULED = "RESCHEDULED"
    MISSED = "MISSED"
    COMPLETED = "COMPLETED"


class CalcType(Enum):
    """How a `PenaltyRule` converts a violation into a monetary amount."""

    PER_UNIT = "PER_UNIT"
    PERCENT_OF_PO = "PERCENT_OF_PO"
    FLAT_FEE = "FLAT_FEE"
    TIERED = "TIERED"


@dataclass
class PenaltyRuleTier:
    """Defines a tier rate for the range [band_min, band_max)."""

    band_min: float
    band_max: float
    rate: float


# Which violation types are priced off the shortage model vs. the delay
# model. Extend this if a retailer introduces a new violation category.
SHORTAGE_VIOLATION_TYPES = {"SHORT_SHIP", "FILL_RATE"}
DELAY_VIOLATION_TYPES = {"OTIF_LATE", "ASN_LATE"}
# ASN_LATE uses the delay model as an approximation because the engine
# does not yet have a dedicated ASN-submission-timing input.

# What a PERCENT_OF_PO rate multiplies against. `None` (and any basis_type the
# engine doesn't recognize yet, e.g. the extraction vocabulary's PO_VALUE,
# UNIT_COST, SHORTFALL_UNITS) falls back to the legacy full-order-value basis.
BASIS_COST_OF_GOODS = "COST_OF_GOODS"
BASIS_SHORTFALL_VALUE = "SHORTFALL_VALUE"

# `applies_per` value the engine understands as day-count accrual. Any other
# value, including None, prices as a single flat application.
APPLIES_PER_DAY = "DAY"


@dataclass
class PenaltyRule:
    """A retailer's penalty terms for one violation type, pure and framework-free.

    `__post_init__` guards the two mistakes that would silently corrupt every
    downstream pricing calculation: `threshold_pct` loaded as a whole-number
    percent instead of a fraction, and a TIERED rule missing its `tiers` list,
    the only source of rates for that calc_type.
    """

    rule_id: str
    violation_type: str  # e.g. "SHORT_SHIP", "OTIF_LATE", "FILL_RATE"
    calc_type: CalcType
    rate: float = 0.0  # meaning depends on calc_type; unused when tiers is set
    threshold_pct: float = 0.0  # FRACTION: 0.02 means 2%, never a whole-number percent
    cap_amount: float | None = None
    tiers: list[PenaltyRuleTier] | None = None  # required when calc_type == TIERED
    basis_type: str | None = None  # what a PERCENT_OF_PO rate multiplies; see BASIS_* above
    applies_per: str | None = None  # APPLIES_PER_DAY accrues per day late; anything else is flat

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold_pct <= 1.0:
            raise ValueError(
                f"Rule {self.rule_id}: threshold_pct={self.threshold_pct!r} is out of range. "
                "This field is a fraction (0.02 for 2%), not a whole-number percent (2.0). "
                "A value outside [0, 1] almost always means the wrong unit was loaded."
            )
        if self.calc_type == CalcType.TIERED and not self.tiers:
            raise ValueError(f"Rule {self.rule_id}: calc_type=TIERED requires tiers to be set")


@dataclass
class OrderSnapshot:
    """Order state captured as of the projection date."""

    order_id: str
    projection_date: date
    order_qty: int
    unit_price: float
    requested_delivery_date: date
    required_ship_date: date

    confirmed_qty: int  # latest SAP ATP / Cut Order Report figure
    production_status: ProductionStatus
    demand_exception_flagged: bool = False  # early warning, before any confirmed cut

    expected_ship_date: date | None = None  # explicit rescheduled ship date, if known.
    actual_ship_date: date | None = None  # set once physically departed
    appointment_status: AppointmentStatus = AppointmentStatus.SCHEDULED
    carrier_reliability_score: float = 90.0
    expected_transit_days: int = 2


@dataclass
class ViolationProjection:
    """One projected violation and its priced/probability-weighted penalty."""

    violation_type: str
    rule_id: str
    probability: float
    penalty_amount: float
    expected_penalty_amount: float
    # Surrogate id of the persisted `penalties.penalty_projection` row. None for
    # a violation not round-tripped through `save_result`, such as the
    # engine-input-only reconstruction in `mitigation.service`.
    id: UUID | None = None


@dataclass
class ProjectionResult:
    """Full projection output for one order: per-violation detail plus the stacked total."""

    order_id: str
    projection_date: date
    days_to_delivery: int
    shortage_probability: float
    delay_probability: float
    violations: list[ViolationProjection]
    total_expected_penalty_amount: float
    stacking_mode: str
