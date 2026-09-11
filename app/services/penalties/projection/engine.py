"""Orchestrates shortage and delay calculations into an order projection."""

from app.services.penalties.projection.delay import (
    compute_days_late,
    compute_delay_probability,
    price_delay_penalty,
)
from app.services.penalties.projection.shortage import (
    compute_shortage_probability,
    price_shortage_penalty,
    shortfall_units_for_pricing,
)
from app.services.penalties.projection.types import (
    DELAY_VIOLATION_TYPES,
    SHORTAGE_VIOLATION_TYPES,
    OrderSnapshot,
    PenaltyRule,
    ProjectionResult,
    ViolationProjection,
)


class ProjectionEngine:
    """Stateless entry point for the penalty-projection domain.

    Combines the shortage and delay calculation models into one order-level
    projection. Holds no constructor state: every call brings its own
    snapshot, rule set, and stacking mode.
    """

    def project(
        self, snapshot: OrderSnapshot, rules: list[PenaltyRule], stacking_mode: str = "SUM"
    ) -> ProjectionResult:
        """Project shortage and delay penalties from an order snapshot and rule set.

        Each rule's expected penalty is its probability times its priced amount;
        `stacking_mode` combines those additively (SUM) or keeps the most severe
        (MAX). Raises ValueError on an unmapped violation_type or an unknown
        stacking mode.
        """
        shortage_prob = compute_shortage_probability(snapshot)
        delay_prob = compute_delay_probability(snapshot)
        shortfall_units = shortfall_units_for_pricing(snapshot)
        days_late = compute_days_late(snapshot)

        violations: list[ViolationProjection] = []

        for rule in rules:
            if rule.violation_type in SHORTAGE_VIOLATION_TYPES:
                probability = shortage_prob
                penalty_amount = price_shortage_penalty(
                    rule, snapshot.order_qty, snapshot.unit_price, shortfall_units
                )
            elif rule.violation_type in DELAY_VIOLATION_TYPES:
                probability = delay_prob
                penalty_amount = price_delay_penalty(rule, snapshot.order_qty, snapshot.unit_price, days_late)
            else:
                raise ValueError(
                    f"Rule {rule.rule_id} has violation_type '{rule.violation_type}' "
                    "not mapped to either SHORTAGE_VIOLATION_TYPES or DELAY_VIOLATION_TYPES"
                )

            expected = probability * penalty_amount
            violations.append(
                ViolationProjection(
                    violation_type=rule.violation_type,
                    rule_id=rule.rule_id,
                    probability=round(probability, 4),
                    penalty_amount=round(penalty_amount, 2),
                    expected_penalty_amount=round(expected, 2),
                )
            )

        if stacking_mode == "MAX":
            total = max((v.expected_penalty_amount for v in violations), default=0.0)
        elif stacking_mode == "SUM":
            total = sum(v.expected_penalty_amount for v in violations)
        else:
            raise ValueError("stacking_mode must be 'SUM' or 'MAX'")

        days_to_delivery = (snapshot.requested_delivery_date - snapshot.projection_date).days

        return ProjectionResult(
            order_id=snapshot.order_id,
            projection_date=snapshot.projection_date,
            days_to_delivery=days_to_delivery,
            shortage_probability=round(shortage_prob, 4),
            delay_probability=round(delay_prob, 4),
            violations=violations,
            total_expected_penalty_amount=round(total, 2),
            stacking_mode=stacking_mode,
        )
