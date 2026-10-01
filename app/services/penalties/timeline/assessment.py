"""Pure assessment: project a plan's timeline, derive its risks, and price them.

Wraps `projection.project_timeline` and `pricing.price_risk` behind one call so
the main service projection and the mitigation repricer price a modified plan
through exactly the same path and can never diverge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.penalties.projection.types import PenaltyRule
from app.services.penalties.timeline.pricing import PricedRisk, PricingBasis, price_risk
from app.services.penalties.timeline.projection import project_timeline
from app.services.penalties.timeline.types import MilestoneDefinition, PlanTimelineInput, PlanTimelineResult

SHORT_RISK_TYPE = "SHORT"


@dataclass(frozen=True)
class PlanAssessment:
    result: PlanTimelineResult
    priced_risks: tuple[PricedRisk, ...]


def assess_plan(
    definitions: Sequence[MilestoneDefinition],
    plan: PlanTimelineInput,
    shortfall_quantity: float,
    shortfall_status: str | None,
    basis: PricingBasis,
    rules: Sequence[PenaltyRule],
    stacking_mode: str,
) -> PlanAssessment:
    """Project `plan`'s timeline and price every timing risk, plus SHORT if applicable.

    `shortfall_status` is `None` when there is no shortfall to price (a fully
    covered plan); otherwise `"BREACHED"` (GOODS_ISSUED already done and short)
    or `"PROJECTED_BREACH"` (projected from the supply position) selects the
    status the SHORT risk is priced under.

    A `NOT_DELIVERED` risk already prices the plan's full quantity through the
    same shortage rules, so a SHORT risk is not priced on top of it: the
    retailer cannot charge both a non-delivery and a short-ship for the same
    undelivered goods.
    """
    result = project_timeline(definitions, plan)
    priced = [
        price_risk(risk.risk_type, risk.status, risk.days_off, None, basis, rules, stacking_mode)
        for risk in result.timing_risks
    ]
    not_delivered = any(risk.risk_type == "NOT_DELIVERED" for risk in result.timing_risks)
    if shortfall_status is not None and not not_delivered:
        priced.append(
            price_risk(
                SHORT_RISK_TYPE, shortfall_status, None, shortfall_quantity, basis, rules, stacking_mode
            )
        )
    return PlanAssessment(result=result, priced_risks=tuple(priced))


def stack_amounts(amounts: Sequence[float], stacking_mode: str) -> float:
    """Combine a set of already-priced amounts per `stacking_mode` ("SUM" or "MAX")."""
    if stacking_mode not in ("SUM", "MAX"):
        raise ValueError(f"Unrecognized stacking_mode={stacking_mode!r}; expected 'SUM' or 'MAX'")
    values = list(amounts)
    if not values:
        return 0.0
    return sum(values) if stacking_mode == "SUM" else max(values)
