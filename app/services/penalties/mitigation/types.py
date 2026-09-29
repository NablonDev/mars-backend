"""Enums and data structures for the mitigation-ranking stage."""

from dataclasses import dataclass
from enum import Enum


class ShortageCause(Enum):
    """Root cause of a projected production shortfall, driving which mitigation options apply."""

    LABOR_CAPACITY = "LABOR_CAPACITY"
    RAW_MATERIAL = "RAW_MATERIAL"
    UNKNOWN = "UNKNOWN"


@dataclass
class MitigationInputs:
    """Cause and cost assumptions for one order.

    Mutable, current-best-guess data rather than a historized fact; see
    `app/repositories/penalties/mitigation.py`.
    """

    order_id: str
    shortage_cause: ShortageCause = ShortageCause.UNKNOWN
    shortage_cause_confirmed: bool = False
    capacity_boost_cost_per_unit: float | None = None
    capacity_boost_max_units_per_day: float | None = None
    capacity_boost_data_confirmed: bool = False
    express_carrier_cost: float | None = None
    express_carrier_transit_days: int | None = None
    express_carrier_data_confirmed: bool = False
    split_shipment_handling_cost: float = 0.0


@dataclass
class MitigationOption:
    """One ranked candidate response to a projected shortage, with its net cost/benefit."""

    action: str  # "ACCEPT" | "SPEED_UP_PRODUCTION" | "SPLIT_SHIPMENT" | "FASTER_CARRIER"
    projected_penalty_after: float
    action_cost: float
    net_saving: float
    risk_level: str  # "LOW" | "MEDIUM" | "HIGH"
    confidence: str  # "CONFIRMED" | "ESTIMATED"
    rationale: str
