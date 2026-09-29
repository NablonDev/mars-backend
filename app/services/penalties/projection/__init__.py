"""Deterministic engine for calculating shortage, delay, and projected penalties.

Holds the pure calculation surface (`types.py`, `engine.py`, `shortage.py`,
`delay.py`, none of which may import SQLAlchemy or FastAPI) alongside the
I/O-performing orchestration built on it (`service.py`, `summary_service.py`).

The `.summary_service` import must stay last. That module imports
`DELAY_VIOLATION_TYPES` and `SHORTAGE_VIOLATION_TYPES` from this package at
module level, and those names resolve against this partially-initialized
package only because the imports above have already bound them.
"""

from app.services.penalties.projection.delay import (  # noqa: I001  (deliberate order; see module docstring)
    compute_days_late,
    compute_delay_probability,
    price_delay_penalty,
    resolve_expected_ship_date,
)
from app.services.penalties.projection.engine import ProjectionEngine
from app.services.penalties.projection.shortage import (
    SHORTAGE_LOCKED_IN_PROBABILITY,
    compute_shortage_probability,
    price_shortage_penalty,
    price_tiered,
    shortfall_units_for_pricing,
)
from app.services.penalties.projection.commitment import (
    compute_commitment_shortfall_probability,
    price_volume_shortfall,
    project_full_window_total,
    resolve_measurement_window,
)
from app.services.penalties.projection.types import (
    APPLIES_PER_DAY,
    BASIS_ORDER_VALUE,
    BASIS_PO_VALUE,
    BASIS_SHORTFALL_UNITS,
    BASIS_SHORTFALL_VALUE,
    DELAY_VIOLATION_TYPES,
    ENGINE_FAMILY_DELAY,
    ENGINE_FAMILY_SHORTAGE,
    ENGINE_FAMILY_VOLUME_COMMITMENT,
    SHORTAGE_VIOLATION_TYPES,
    AppointmentStatus,
    CalcType,
    CommitmentProjection,
    CommitmentSnapshot,
    OrderSnapshot,
    PenaltyRule,
    PenaltyRuleTier,
    ProductionStatus,
    ProjectionResult,
    SkippedRuleProjection,
    ViolationProjection,
)
from app.services.penalties.projection.summary_service import (
    PenaltyProjectionSummaryOutputWithReuse,
    ProjectionSummaryService,
    SummaryJob,
)

__all__ = [
    "APPLIES_PER_DAY",
    "BASIS_ORDER_VALUE",
    "BASIS_PO_VALUE",
    "BASIS_SHORTFALL_UNITS",
    "BASIS_SHORTFALL_VALUE",
    "DELAY_VIOLATION_TYPES",
    "ENGINE_FAMILY_DELAY",
    "ENGINE_FAMILY_SHORTAGE",
    "ENGINE_FAMILY_VOLUME_COMMITMENT",
    "SHORTAGE_LOCKED_IN_PROBABILITY",
    "SHORTAGE_VIOLATION_TYPES",
    "AppointmentStatus",
    "CalcType",
    "CommitmentProjection",
    "CommitmentSnapshot",
    "OrderSnapshot",
    "PenaltyProjectionSummaryOutputWithReuse",
    "PenaltyRule",
    "PenaltyRuleTier",
    "ProductionStatus",
    "ProjectionEngine",
    "ProjectionResult",
    "ProjectionSummaryService",
    "SkippedRuleProjection",
    "SummaryJob",
    "ViolationProjection",
    "compute_commitment_shortfall_probability",
    "compute_days_late",
    "compute_delay_probability",
    "compute_shortage_probability",
    "price_delay_penalty",
    "price_shortage_penalty",
    "price_tiered",
    "price_volume_shortfall",
    "project_full_window_total",
    "resolve_expected_ship_date",
    "resolve_measurement_window",
    "shortfall_units_for_pricing",
]
