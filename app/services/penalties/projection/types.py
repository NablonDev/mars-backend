"""Enums and data structures for the penalty-projection engine.

The engine's contract is one scalar `order_qty`/`unit_price` per snapshot
rather than a per-line list, and `order_id` carries a stringified
purchase-order UUID rather than a business key.

`PenaltyRule` and `PenaltyRuleTier` are the pure, framework-free counterparts
of the ORM models of the same name in `app.models.penalties.rule`, told apart
by import path rather than by renaming one side. `MitigationOption` follows
the same pattern (see `app/repositories/penalties/mitigation.py`).
"""

from dataclasses import dataclass, field
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


# `PenaltyRuleTier.tier_application` values `price_tiered` branches on; see
# `shortage.py::price_tiered`/`_price_tiered_marginal`.
TIER_APPLICATION_CLIFF = "CLIFF"
TIER_APPLICATION_MARGINAL = "MARGINAL"


@dataclass
class PenaltyRuleTier:
    """Defines a tier rate for the range [band_min, band_max). `band_max=None` means unbounded.

    `tier_application` (CLIFF/MARGINAL/NOT_APPLICABLE) selects how `shortage.price_tiered`
    combines bands; `tier_basis` names what the band measures (e.g. SHORTFALL_PCT,
    DAYS_LATE). Both default to the migration's own backfill values for these columns, so a
    rule built before either column existed prices exactly as it did before.
    """

    band_min: float
    band_max: float | None
    rate: float
    tier_application: str = "CLIFF"
    tier_basis: str = "SHORTFALL_PCT"


# Which violation types are priced off the shortage model vs. the delay
# model. Extend this if a retailer introduces a new violation category.
SHORTAGE_VIOLATION_TYPES = {"SHORT_SHIP", "FILL_RATE"}
DELAY_VIOLATION_TYPES = {"OTIF_LATE", "ASN_LATE"}
# ASN_LATE uses the delay model as an approximation because the engine
# does not yet have a dedicated ASN-submission-timing input.

# `engine_family` values the engine itself branches on: SHORTAGE/DELAY select
# ProjectionEngine.project's per-PO path (still keyed off violation_type, not this
# field); VOLUME_COMMITMENT selects the separate per-agreement path in commitment.py.
# Mirrors `app.services.penalties.rule_extraction.vocabulary.ENGINE_FAMILIES`.
ENGINE_FAMILY_SHORTAGE = "SHORTAGE"
ENGINE_FAMILY_DELAY = "DELAY"
ENGINE_FAMILY_VOLUME_COMMITMENT = "VOLUME_COMMITMENT"

# What a PERCENT_OF_PO rate multiplies against. Only BASIS_SHORTFALL_VALUE and
# BASIS_SHORTFALL_UNITS change the math (see shortage.py's _price_shortfall_value_basis/
# _price_shortfall_units_basis). This constant, `None`, and any other basis_type all price
# off the same thing: the PO's own order_qty * unit_price. The value stays "COST_OF_GOODS"
# because that string is already persisted on published `penalty_rule` rows; the name is
# the PO's own ordered value, not a real cost figure. BASIS_PO_VALUE is the same
# order_qty * unit_price basis under its other governed spelling: both are branched
# explicitly in shortage.py rather than left to fall through to the same `else`.
BASIS_ORDER_VALUE = "COST_OF_GOODS"
BASIS_PO_VALUE = "PO_VALUE"
BASIS_SHORTFALL_VALUE = "SHORTFALL_VALUE"
BASIS_SHORTFALL_UNITS = "SHORTFALL_UNITS"

# `applies_per` value the engine understands as day-count accrual. Any other
# value, including None, prices as a single flat application unless it's one of the
# fractional-period values below.
APPLIES_PER_DAY = "DAY"
# Fractional-period accrual: `price_delay_penalty` converts days_late into a period
# count via `rule.rounding_convention` (see delay.py's `_accrual_periods`).
APPLIES_PER_WEEK = "WEEK"
APPLIES_PER_MONTH = "MONTH"
APPLIES_PER_QUARTER = "QUARTER"
APPLIES_PER_YEAR = "YEAR"
# Counts once per late-delivery instance; for a single-PO projection that is this
# very call, so it prices as a flat single application, same as the default case.
APPLIES_PER_OCCURRENCE = "OCCURRENCE"
# Non-duration `applies_per` values that define the counting granularity of
# order_qty/rate (charge per case rather than per each unit) rather than a second
# multiplier layered on top of PER_UNIT's rate; see delay.py::price_delay_penalty.
APPLIES_PER_COUNTING_GRANULARITY = {"UNIT", "CASE", "PALLET", "SHIPMENT", "DELIVERY"}


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
    currency_code: str = "USD"
    grace_period_days: int = 0
    # Which pricing engine this rule belongs to (see ENGINE_FAMILY_* above); None for a
    # rule published before this field existed or seeded directly.
    engine_family: str | None = None
    # Required for applies_per in {WEEK, MONTH, QUARTER, YEAR}; see delay.py's
    # _accrual_periods. None for a DAY/OCCURRENCE/counting-granularity rule, which needs no
    # fractional-period rounding.
    rounding_convention: str | None = None
    # ROLLING/FIXED_CALENDAR/CONTRACT_YEAR/ANNIVERSARY; only read by commitment.py's
    # window resolution, for an engine_family=VOLUME_COMMITMENT rule.
    measurement_window_type: str | None = None
    measurement_window_length: int | None = None
    measurement_window_unit: str | None = None
    # The contract-period purchase target this rule measures a shortfall against.
    # Meaningful only for engine_family=VOLUME_COMMITMENT; exactly one of the two is set,
    # matching whichever of quantity or value the rule's metric_code measures. Not an
    # extraction-populated field yet (no StagedFact attribute_role maps to it): a
    # commitment rule must have this set by hand or a follow-up publisher change before
    # `commitment.price_volume_shortfall` can price it.
    commitment_quantity: float | None = None
    commitment_value: float | None = None

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
class CommitmentSnapshot:
    """Contract-period purchase-volume state for one retailer agreement, as of a date.

    Parallel to `OrderSnapshot`, not an extension of it: this measures a whole
    measurement window's purchases against a commitment, not one purchase order.
    Exactly one of the quantity/value pair is populated on both `committed_*` and
    `actual_to_date_*`, matching whichever the rule's metric measures.
    """

    retailer_agreement_id: str
    window_start_date: date
    window_end_date: date
    as_of_date: date
    committed_quantity: float | None = None
    committed_value: float | None = None
    actual_to_date_quantity: float | None = None
    actual_to_date_value: float | None = None


@dataclass
class CommitmentProjection:
    """One projected volume-commitment shortfall for one rule, priced and probability-weighted."""

    rule_id: str
    retailer_agreement_id: str
    shortfall_probability: float
    projected_shortfall_quantity: float | None
    projected_shortfall_value: float | None
    penalty_amount: float
    expected_penalty_amount: float


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
class SkippedRuleProjection:
    """One rule this run's engine cannot price, recorded rather than raised or dropped.

    Persisted as a zero-amount `penalty_projection` row carrying `skip_reason`, so a
    retailer's exposure never silently loses a mis-tagged or not-yet-priceable rule
    without a trace.
    """

    violation_type: str
    rule_id: str
    skip_reason: str


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
    skipped: list[SkippedRuleProjection] = field(default_factory=list)
