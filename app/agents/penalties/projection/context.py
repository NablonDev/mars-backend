"""Typed contract for the data handed to the penalty-projection-summary LLM call."""

from __future__ import annotations

from datetime import date
from typing import Any

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


# ---------------------------------------------------------------------------
# Event-Driven Fulfillment Timeline Context Models (v3)
# ---------------------------------------------------------------------------


class TimelineLineContext(BaseModel):
    """One line item on the fulfillment plan."""

    material_code: str | None = None
    material_description: str | None = None
    planned_quantity: float
    confirmed_quantity: float | None = None
    shortfall_quantity: float = 0.0
    unit_price: float | None = None


class TimelineMilestoneContext(BaseModel):
    """One dated milestone on the fulfillment timeline."""

    code: str
    name: str
    sequence_no: int
    owner_team: str | None = None
    is_measurement_point: bool = False
    baseline_date: date | None = None
    planned_date: date | None = None
    projected_date: date | None = None
    actual_date: date | None = None
    slip_days: int = 0


class TimelineEventContext(BaseModel):
    """One append-only event recorded on the plan."""

    event_at: str
    event_type: str
    milestone_code: str | None = None
    reason_code: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    source: str


class TimelineRiskContext(BaseModel):
    """One deterministic risk evaluated for the plan."""

    projection_date: date | None = None
    risk_type: str  # LATE, SHORT, NOT_DELIVERED, EARLY, ASN_LATE
    status: str  # SLIPPING, PROJECTED_BREACH, BREACHED
    days_off: int | None = None
    shortfall_quantity: float | None = None
    projected_penalty_amount: float
    currency_code: str = "USD"
    driver_milestone_code: str | None = None
    driver_reason_code: str | None = None
    calculation_detail: dict[str, Any] | None = None


class TimelineOptionContext(BaseModel):
    """One candidate mitigation option evaluated by the engine."""

    action_code: str
    owner_team: str | None = None
    feasible: bool = True
    infeasible_reason: str | None = None
    action_cost: float = 0.0
    penalty_before: float = 0.0
    penalty_after: float = 0.0
    net_saving: float = 0.0
    act_by_date: date | None = None
    confidence: str | None = "HIGH"
    rank_no: int | None = 1
    addresses_risk_types: list[str] = Field(default_factory=list)


class TimelineAlertContext(BaseModel):
    """One tracked alert for the plan."""

    alert_id: str
    risk_type: str
    status: str
    first_seen_date: date
    last_seen_date: date


class TimelineProjectionSummaryContext(BaseModel):
    """Authoritative, grounded context for event-driven fulfillment timeline penalty & mitigation LLM reasoning (v3)."""

    plan_id: str
    plan_number: str
    purchase_order_id: str
    purchase_order_number: str
    retailer_po_number: str | None = None
    retailer_name: str
    order_status: str
    freight_term: str
    window_start: date | None = None
    window_end: date | None = None
    cancel_date: date | None = None
    carrier_name: str | None = None
    warehouse_name: str | None = None
    safety_buffer_days: int = 0
    lines: list[TimelineLineContext] = Field(default_factory=list)
    milestones: list[TimelineMilestoneContext] = Field(default_factory=list)
    events: list[TimelineEventContext] = Field(default_factory=list)
    latest_risks: list[TimelineRiskContext] = Field(default_factory=list)
    options: list[TimelineOptionContext] = Field(default_factory=list)
    active_alert: TimelineAlertContext | None = None
    selected_risk_type: str | None = None
