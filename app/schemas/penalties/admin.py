"""API schemas for the demo seed-data and daily-scenario-replay admin endpoints.

Field names mirror `PenaltySeedingService`'s own dict output.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class SeedDataResponse(BaseModel):
    """Response shape for the demo seed-data endpoint: rows inserted per table."""

    retailers: int
    materials: int
    skus: int
    plants: int
    carriers: int
    rules: int
    orders: int
    mitigation_inputs: int
    dispute_rules: int
    dispute_orders: int
    dispute_actual_penalties: int


class ScenarioDayResult(BaseModel):
    """One simulated day's projection result for a purchase order."""

    projection_date: date
    note: str
    # Raw, if-realized amounts (not probability-weighted): the primary figure a narrative pairs
    # with the probability below. Per docs/API.md, raw and expected amounts must always be shown
    # as separate numbers, never collapsed into one.
    shortage_penalty_amount: float
    delay_penalty_amount: float
    # Blended (probability x raw) risk-adjusted figures: a secondary, clearly labeled supporting
    # number, never shown as the only one.
    shortage_expected_penalty_amount: float
    delay_expected_penalty_amount: float
    total_expected_penalty_amount: float
    shortage_probability: float
    delay_probability: float


class ScenarioSummary(BaseModel):
    """Per-purchase-order rollup of `ScenarioDayResult`s for the daily-scenario-replay response."""

    purchase_order_id: str
    days: list[ScenarioDayResult]
    negotiation: dict | None = None


class SimulateDailyRunResponse(BaseModel):
    """Response shape for the daily-scenario-replay admin endpoint."""

    scenarios: list[ScenarioSummary]
