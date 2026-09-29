"""Typed contract for the data handed to the penalty-projection-summary LLM call."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class TierBand(BaseModel):
    """One rate tier of a tiered penalty rule, keyed by band boundaries. `band_max=None` means unbounded."""

    band_min: float
    band_max: float | None
    rate: float


class ActiveRule(BaseModel):
    """A penalty rule matched by the deterministic engine for this order."""

    rule_id: str
    violation_type: str
    calc_type: str
    rate: float | None = None
    threshold_pct: float = 0.0
    cap_amount: float | None = None
    tiers: list[TierBand] | None = None


class ViolationEntry(BaseModel):
    """One rule violation the engine detected (or projected) on a given day."""

    violation_type: str
    rule_id: str
    probability: float
    penalty_amount: float | None = None
    expected_penalty_amount: float


class DailyHistoryEntry(BaseModel):
    """One day's inputs AND outputs."""

    entry_date: date

    # Inputs
    confirmed_qty: int
    production_status: str
    appointment_status: str
    actual_ship_date: date | None = None
    expected_ship_date_override: date | None = None
    demand_exception_flagged: bool = False
    days_to_delivery: int

    # Outputs
    shortage_probability: float
    delay_probability: float
    violations: list[ViolationEntry]
    total_expected_penalty_amount: float


class OrderContext(BaseModel):
    """Order-identifying and scheduling fields the projection was computed against."""

    order_id: str
    order_status: str
    retailer_name: str
    sku_description: str
    order_qty: int
    unit_price: float
    required_ship_date: date
    requested_delivery_date: date
    carrier_id: str | None = None
    carrier_name: str | None = None


class ActualOutcome(BaseModel):
    """Only present when order_status == DELIVERED."""

    violation_type: str
    actual_penalty_amount: float
    invoice_or_deduction_date: date


class PenaltyProjectionSummaryContext(BaseModel):
    """Everything the model needs to narrate the engine's penalty projection."""

    order: OrderContext
    current_projection_date: date  # which daily_history entry is "today"
    stacking_mode: str
    active_rules: list[ActiveRule]
    daily_history: list[DailyHistoryEntry]  # ordered oldest to newest, bounded to <= current_projection_date

    # Computed explicitly by the caller
    shared_production_line: bool = False
    other_open_orders_same_sku_location: list[str] = Field(default_factory=list)

    # Only populated when order.order_status == "DELIVERED"
    actual_outcomes: list[ActualOutcome] | None = None
