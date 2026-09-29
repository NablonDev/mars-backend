"""Typed contract for the data handed to the penalty-mitigation-summary LLM call."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class OrderContext(BaseModel):
    """Order-identifying and scheduling fields the mitigation options were ranked against."""

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


class MitigationOptionContext(BaseModel):
    """One already-ranked, already-computed mitigation option.

    Ground truth handed to the model, never something it derives itself.
    """

    action: Literal["ACCEPT", "SPEED_UP_PRODUCTION", "SPLIT_SHIPMENT", "FASTER_CARRIER"]
    projected_penalty_after: float
    action_cost: float
    net_saving: float
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    confidence: Literal["CONFIRMED", "ESTIMATED"]
    rationale: str


class ActualOutcome(BaseModel):
    """Only present when order.order_status == DELIVERED."""

    violation_type: str
    actual_penalty_amount: float
    invoice_or_deduction_date: date


class PenaltyMitigationSummaryContext(BaseModel):
    """Everything the model needs to narrate the engine's already-ranked mitigation options."""

    order: OrderContext
    current_projection_date: date  # the day the mitigation_options below were ranked for
    # The ACCEPT baseline, stated explicitly so the model never has to work out which
    # mitigation_options entry is the baseline.
    current_total_expected_penalty_amount: float
    stacking_mode: str

    # Already ranked by the pure engine (net_saving descending); never re-rank, and
    # never recompute a number from this list.
    mitigation_options: list[MitigationOptionContext]

    # Only populated when order.order_status == "DELIVERED"
    actual_outcomes: list[ActualOutcome] | None = Field(default=None)
