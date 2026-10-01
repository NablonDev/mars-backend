"""Assembles fulfillment_risk / fulfillment_mitigation_option rows from a plan's assessment.

Collaborator used by `TimelineProjectionService`: turns one plan's
`assessment.PlanAssessment` into persistable risk rows, builds the
`PlanSituation` mitigation evaluates against and its repricer, and maps
evaluated mitigation options into persistable rows. The driver resolver here
is shared by timing risks, the SHORT risk, and `PlanSituation`, so a
MATERIAL_AVAILABLE driver with a known supply cause reports that cause
consistently everywhere rather than only for SHORT.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.services.penalties.projection.types import PenaltyRule
from app.services.penalties.timeline.assessment import PlanAssessment, assess_plan, stack_amounts
from app.services.penalties.timeline.mitigation import EvaluatedOption, PlanSituation, Repricer
from app.services.penalties.timeline.pricing import PricedRisk, PricingBasis, RuleCharge
from app.services.penalties.timeline.supply import PlanSupplyOutcome
from app.services.penalties.timeline.types import (
    MilestoneDefinition,
    PlanTimelineInput,
    PlanTimelineResult,
    ProjectedMilestone,
    TimingRisk,
)

_SHORT_TOLERANCE = 1e-6

# Stamped on every risk row's `calculation_detail`. Bump it whenever projection, supply,
# pricing, or mitigation semantics change, so a number that moves between runs can be told
# apart from one that moved because the engine did.
ENGINE_VERSION = "2"

# risk_type values a row's supply block can apply to outright; every other risk_type still
# gets one if its driver_milestone_code is MATERIAL_AVAILABLE (see `_supply_detail`).
_SUPPLY_RISK_TYPES = {"SHORT", "NOT_DELIVERED"}


def _iso(value: date | None) -> str | None:
    """`date.isoformat()`, or `None` for a `None` date -- shared by every `calculation_detail` field."""
    return value.isoformat() if value is not None else None


def _round_money(value: float | None) -> float | None:
    """Round a money field to 2dp for `calculation_detail`, or `None` for a `None` value."""
    return round(value, 2) if value is not None else None


@dataclass
class RiskRowAssembler:
    timeline: FulfillmentTimelineRepository

    def build_risk_rows(
        self,
        plan_id: UUID,
        purchase_order_id: UUID,
        plan_input: PlanTimelineInput,
        assessment: PlanAssessment,
        shortfall_quantity: float,
        cause_code: str | None,
        cause_source_id: str | None,
        cause_source_types: dict[str, str],
        basis: PricingBasis,
        stacking_mode: str,
        rule_codes: dict[str, str],
        outcome: PlanSupplyOutcome | None,
    ) -> list[dict]:
        """Build one row per priced risk, plus a zero-penalty SLIPPING row when applicable.

        The SLIPPING row is added only when the plan is slipping and has no
        timing risk of any kind (a slip fully absorbed by slack). No rows at
        all when the plan has neither a timing risk nor a shortfall.
        `basis`/`stacking_mode`/`rule_codes`/`outcome` feed every row's
        `calculation_detail`, the persisted explanation of how its penalty
        (or lack of one) was computed.
        """
        result = assessment.result
        projected_milestones = [self._milestone_dict(m) for m in result.milestones]
        timing_priced = assessment.priced_risks[: len(result.timing_risks)]
        short_priced = assessment.priced_risks[len(result.timing_risks) :]

        rows = [
            self._timing_row(
                plan_id,
                purchase_order_id,
                plan_input,
                result,
                timing_risk,
                priced,
                cause_code,
                cause_source_id,
                cause_source_types,
                projected_milestones,
                basis,
                stacking_mode,
                rule_codes,
                outcome,
            )
            for timing_risk, priced in zip(result.timing_risks, timing_priced, strict=True)
        ]
        if short_priced:
            rows.append(
                self._short_row(
                    plan_id,
                    purchase_order_id,
                    plan_input,
                    result,
                    short_priced[0],
                    shortfall_quantity,
                    cause_code,
                    cause_source_id,
                    cause_source_types,
                    projected_milestones,
                    basis,
                    stacking_mode,
                    rule_codes,
                    outcome,
                )
            )

        if result.slipping and not result.timing_risks:
            rows.append(
                self._slipping_row(
                    plan_id,
                    purchase_order_id,
                    plan_input,
                    result,
                    projected_milestones,
                    basis,
                    stacking_mode,
                    rule_codes,
                    outcome,
                    cause_source_types,
                )
            )

        return rows

    def build_situation(
        self,
        plan_id: UUID,
        plan_input: PlanTimelineInput,
        definitions: Sequence[MilestoneDefinition],
        assessment: PlanAssessment,
        breach_amounts: list[float],
        cause_code: str | None,
        cause_source_id: str | None,
        cause_source_types: dict[str, str],
        shortfall_quantity: float,
        outcome: PlanSupplyOutcome | None,
        stacking_mode: str,
    ) -> PlanSituation:
        """Assemble the `PlanSituation` mitigation evaluates against, from this run's assessment.

        Only called when `breach_amounts` is non-empty, so a PROJECTED_BREACH
        priced risk is always present among `assessment.priced_risks`.
        """
        result = assessment.result
        risk_types = {p.risk_type for p in assessment.priced_risks}
        if result.slipping and "LATE" not in risk_types:
            risk_types.add("LATE")

        breach_risk = self._costliest_breach(assessment.priced_risks)
        driver_code: str | None
        if breach_risk.risk_type == "SHORT":
            driver_code = "MATERIAL_AVAILABLE"
        else:
            driver_code = next(
                t.driver_milestone_code for t in result.timing_risks if t.risk_type == breach_risk.risk_type
            )

        driver_reason, _ = self._resolve_driver(
            plan_id, driver_code, cause_code, cause_source_id, cause_source_types
        )
        wait_not_before = (
            outcome.full_cover_date if breach_risk.risk_type == "SHORT" and outcome is not None else None
        )

        return PlanSituation(
            plan=plan_input,
            definitions=tuple(definitions),
            baseline_result=result,
            risk_types=frozenset(risk_types),
            driver_code=driver_code,
            driver_reason=driver_reason,
            penalty_before=stack_amounts(breach_amounts, stacking_mode),
            shortfall_quantity=shortfall_quantity,
            wait_not_before=wait_not_before,
        )

    def make_reprice(
        self,
        definitions: Sequence[MilestoneDefinition],
        basis: PricingBasis,
        rules: Sequence[PenaltyRule],
        stacking_mode: str,
    ) -> Repricer:
        """Repricer callback for `evaluate_mitigations`: re-runs `assess_plan` on the modified plan."""

        def reprice(modified_plan: PlanTimelineInput, modified_shortfall: float) -> float:
            shortfall_status = "PROJECTED_BREACH" if modified_shortfall > _SHORT_TOLERANCE else None
            reassessed = assess_plan(
                definitions, modified_plan, modified_shortfall, shortfall_status, basis, rules, stacking_mode
            )
            breach_amounts = [
                p.penalty_amount for p in reassessed.priced_risks if p.status == "PROJECTED_BREACH"
            ]
            return stack_amounts(breach_amounts, stacking_mode)

        return reprice

    def option_rows(self, purchase_order_id: UUID, options: Sequence[EvaluatedOption]) -> list[dict]:
        """Map every evaluated mitigation option into a persistable `fulfillment_mitigation_option` row."""
        return [self._option_row(purchase_order_id, option) for option in options]

    def _milestone_dict(self, milestone: ProjectedMilestone) -> dict:
        """One `ProjectedMilestone` as a JSON-safe dict, for `fulfillment_risk.projected_milestones`."""
        return {
            "code": milestone.code,
            "baseline_date": milestone.baseline_date.isoformat() if milestone.baseline_date else None,
            "planned_date": milestone.planned_date.isoformat() if milestone.planned_date else None,
            "projected_date": milestone.projected_date.isoformat(),
            "actual_date": milestone.actual_date.isoformat() if milestone.actual_date else None,
            "slip_days": milestone.slip_days,
        }

    def _costliest_breach(self, priced_risks: Sequence[PricedRisk]) -> PricedRisk:
        """The `PROJECTED_BREACH` risk mitigation matches its root cause against.

        Mitigations address one root cause per plan: the risk with the
        largest penalty amount, not risk-type or insertion order. Ties break
        by risk type -- LATE/NOT_DELIVERED/EARLY/ASN_LATE before SHORT, then
        alphabetically -- so the choice is deterministic.
        """
        breach_risks = [p for p in priced_risks if p.status == "PROJECTED_BREACH"]

        def sort_key(risk: PricedRisk) -> tuple[float, int, str]:
            group_rank = 1 if risk.risk_type == "SHORT" else 0
            return (-risk.penalty_amount, group_rank, risk.risk_type)

        return min(breach_risks, key=sort_key)

    def _resolve_driver_for_milestone(
        self, plan_id: UUID, driver_milestone_code: str | None
    ) -> tuple[str | None, UUID | None]:
        """Resolve a driver milestone's latest *reason-carrying* event, or (None, None).

        A reason-less event (a plain `COMPLETED` fact) is skipped: the driver's
        reason must reflect the disruption that caused the slip, not the
        milestone's own eventual completion.
        """
        if driver_milestone_code is None:
            return None, None
        event = self.timeline.latest_reasoned_event_for_milestone(plan_id, driver_milestone_code)
        if event is None:
            return None, None
        return event["reason_code"], event["id"]

    def _resolve_driver(
        self,
        plan_id: UUID,
        driver_milestone_code: str | None,
        cause_code: str | None,
        cause_source_id: str | None,
        cause_source_types: dict[str, str],
    ) -> tuple[str | None, UUID | None]:
        """Resolve a driver's reason/event, shared by timing risks, SHORT, and `PlanSituation`.

        When the driver is MATERIAL_AVAILABLE and a supply cause is known, the
        supply cause code and the latest event on its cause source win over
        the milestone's own recorded event.
        """
        if driver_milestone_code == "MATERIAL_AVAILABLE" and cause_code is not None:
            driver_event_id = None
            subject_type = cause_source_types.get(cause_source_id) if cause_source_id is not None else None
            if subject_type is not None:
                event = self.timeline.latest_event_for_subject(subject_type, UUID(cause_source_id))
                driver_event_id = event["id"] if event else None
            return cause_code, driver_event_id
        return self._resolve_driver_for_milestone(plan_id, driver_milestone_code)

    def _calculation_detail(
        self,
        plan_input: PlanTimelineInput,
        result: PlanTimelineResult,
        priced: PricedRisk | None,
        basis: PricingBasis,
        stacking_mode: str,
        rule_codes: dict[str, str],
        outcome: PlanSupplyOutcome | None,
        cause_source_types: dict[str, str],
        risk_type: str,
        driver_milestone_code: str | None,
    ) -> dict:
        """Build the `calculation_detail` JSON persisted on every risk row, for UI explainability.

        `priced` is `None` for the SLIPPING row (no rule ever priced it): it
        still gets a `calculation_detail` with an empty `pricing.rules` list
        rather than none at all, so the UI never has to special-case it.
        `risk_type`/`driver_milestone_code` gate `supply`: see `_supply_detail`.
        """
        rule_breakdown = priced.rule_breakdown if priced is not None else ()
        return {
            "engine_version": ENGINE_VERSION,
            "data_freshness": self._data_freshness(plan_input),
            "measured": {
                "milestone_code": result.measured_milestone_code,
                "projected_date": _iso(result.projected_measured_date),
                "window_start": _iso(plan_input.window_start),
                "window_end": _iso(plan_input.window_end),
                "cancel_date": _iso(plan_input.cancel_date),
                "slack_days": result.slack_days,
            },
            "pricing": {
                "stacking_mode": stacking_mode,
                # Why this breach priced to $0 (NO_APPLICABLE_RULE or WITHIN_GRACE_OR_THRESHOLD), else None.
                "zero_reason": priced.zero_reason if priced is not None else None,
                "unit_cost": _round_money(basis.unit_cost),
                "unit_price": _round_money(basis.unit_price),
                # `quantity` here is `basis.quantity` (unrounded); each rule's own
                # `quantity` below is the rounded order quantity actually priced against
                # (see `pricing._resolve_context`'s `order_qty`), so the two can differ.
                "quantity": basis.quantity,
                "rules": [self._rule_charge_dict(charge, rule_codes) for charge in rule_breakdown],
            },
            "supply": self._supply_detail(outcome, cause_source_types, risk_type, driver_milestone_code),
        }

    def _data_freshness(self, plan_input: PlanTimelineInput) -> dict:
        """Milestones whose planned date has passed with no actual recorded, as of the run date.

        The projection assumes each such milestone completes today. That is a guess, not a
        fact, so the numbers downstream of it are only as fresh as the upstream feed; this
        makes the gap visible instead of letting a silent feed outage read as a live plan.
        """
        overdue = []
        for state in plan_input.milestones:
            due = state.planned_date or state.baseline_date
            if state.actual_date is None and due is not None and due < plan_input.as_of:
                overdue.append(
                    {
                        "code": state.code,
                        "due_date": due.isoformat(),
                        "days_overdue": (plan_input.as_of - due).days,
                    }
                )
        return {"overdue_without_actual": overdue}

    def _rule_charge_dict(self, charge: RuleCharge, rule_codes: dict[str, str]) -> dict:
        """One `RuleCharge` as a JSON-safe dict, `rule_code` filled in from the resolved map."""
        return {
            "rule_id": charge.rule_id,
            "rule_code": charge.rule_code or rule_codes.get(charge.rule_id),
            "violation_type": charge.violation_type,
            "calc_type": charge.calc_type,
            "rate": charge.rate,
            "basis_type": charge.basis_type,
            "unit_amount": _round_money(charge.unit_amount),
            "quantity": charge.quantity,
            "shortfall_quantity": charge.shortfall_quantity,
            "days_off": charge.days_off,
            "grace_period_days": charge.grace_period_days,
            "chargeable_days": charge.chargeable_days,
            "amount": charge.amount,
            "threshold_pct": charge.threshold_pct,
        }

    def _supply_detail(
        self,
        outcome: PlanSupplyOutcome | None,
        cause_source_types: dict[str, str],
        risk_type: str,
        driver_milestone_code: str | None,
    ) -> dict | None:
        """The `calculation_detail.supply` block: present only for a row supply actually explains.

        Gated on the row itself, not just the pool's outcome: a SHORT/NOT_DELIVERED
        row, or any row whose own `driver_milestone_code` is MATERIAL_AVAILABLE. A
        picking- or carrier-driven LATE row never gets one, even when its (material,
        plant) pool's supply outcome happens to carry a `material_available_not_before`
        from unrelated cross-plan demand contention -- that's the pool's story, not this
        row's, and showing it here would misattribute the penalty's cause.
        """
        if outcome is None:
            return None
        is_supply_row = risk_type in _SUPPLY_RISK_TYPES or driver_milestone_code == "MATERIAL_AVAILABLE"
        if not is_supply_row:
            return None
        had_shortfall = outcome.shortfall_quantity > _SHORT_TOLERANCE
        had_supply_delay = outcome.material_available_not_before is not None
        if not had_shortfall and not had_supply_delay:
            return None
        cause_source_type = (
            cause_source_types.get(outcome.cause_source_id) if outcome.cause_source_id is not None else None
        )
        return {
            "on_hand": outcome.on_hand,
            "required_quantity": outcome.required_quantity,
            "on_time_quantity": outcome.on_time_quantity,
            "shortfall_quantity": outcome.shortfall_quantity,
            "full_cover_date": _iso(outcome.full_cover_date),
            "cause_code": outcome.cause_code,
            "cause_source_type": cause_source_type,
            "cause_source_id": outcome.cause_source_id,
            "cause_available_date": _iso(outcome.cause_available_date),
            "cause_baseline_date": _iso(outcome.cause_baseline_date),
        }

    def _timing_row(
        self,
        plan_id: UUID,
        purchase_order_id: UUID,
        plan_input: PlanTimelineInput,
        result: PlanTimelineResult,
        timing_risk: TimingRisk,
        priced: PricedRisk,
        cause_code: str | None,
        cause_source_id: str | None,
        cause_source_types: dict[str, str],
        projected_milestones: list[dict],
        basis: PricingBasis,
        stacking_mode: str,
        rule_codes: dict[str, str],
        outcome: PlanSupplyOutcome | None,
    ) -> dict:
        """Build a `fulfillment_risk` row for one LATE/EARLY/NOT_DELIVERED/ASN_LATE timing risk."""
        driver_reason, driver_event_id = self._resolve_driver(
            plan_id, timing_risk.driver_milestone_code, cause_code, cause_source_id, cause_source_types
        )
        return {
            "purchase_order_id": purchase_order_id,
            "risk_type": priced.risk_type,
            "status": priced.status,
            "measured_milestone_code": result.measured_milestone_code,
            "projected_measured_date": result.projected_measured_date,
            "window_start": plan_input.window_start,
            "window_end": plan_input.window_end,
            "days_off": timing_risk.days_off,
            "shortfall_quantity": None,
            "driver_milestone_code": timing_risk.driver_milestone_code,
            "driver_reason_code": driver_reason,
            "driver_event_id": driver_event_id,
            "projected_penalty_amount": priced.penalty_amount,
            "currency_code": "USD",
            "priced_rule_ids": list(priced.priced_rule_ids),
            "projected_milestones": projected_milestones,
            "calculation_detail": self._calculation_detail(
                plan_input,
                result,
                priced,
                basis,
                stacking_mode,
                rule_codes,
                outcome,
                cause_source_types,
                priced.risk_type,
                timing_risk.driver_milestone_code,
            ),
        }

    def _short_row(
        self,
        plan_id: UUID,
        purchase_order_id: UUID,
        plan_input: PlanTimelineInput,
        result: PlanTimelineResult,
        priced: PricedRisk,
        shortfall_quantity: float,
        cause_code: str | None,
        cause_source_id: str | None,
        cause_source_types: dict[str, str],
        projected_milestones: list[dict],
        basis: PricingBasis,
        stacking_mode: str,
        rule_codes: dict[str, str],
        outcome: PlanSupplyOutcome | None,
    ) -> dict:
        """Build the `fulfillment_risk` row for a SHORT (quantity shortfall) risk."""
        driver_reason, driver_event_id = self._resolve_driver(
            plan_id, "MATERIAL_AVAILABLE", cause_code, cause_source_id, cause_source_types
        )
        return {
            "purchase_order_id": purchase_order_id,
            "risk_type": priced.risk_type,
            "status": priced.status,
            "measured_milestone_code": result.measured_milestone_code,
            "projected_measured_date": result.projected_measured_date,
            "window_start": plan_input.window_start,
            "window_end": plan_input.window_end,
            "days_off": None,
            "shortfall_quantity": shortfall_quantity,
            "driver_milestone_code": "MATERIAL_AVAILABLE",
            "driver_reason_code": driver_reason,
            "driver_event_id": driver_event_id,
            "projected_penalty_amount": priced.penalty_amount,
            "currency_code": "USD",
            "priced_rule_ids": list(priced.priced_rule_ids),
            "projected_milestones": projected_milestones,
            "calculation_detail": self._calculation_detail(
                plan_input,
                result,
                priced,
                basis,
                stacking_mode,
                rule_codes,
                outcome,
                cause_source_types,
                priced.risk_type,
                "MATERIAL_AVAILABLE",
            ),
        }

    def _slipping_row(
        self,
        plan_id: UUID,
        purchase_order_id: UUID,
        plan_input: PlanTimelineInput,
        result: PlanTimelineResult,
        projected_milestones: list[dict],
        basis: PricingBasis,
        stacking_mode: str,
        rule_codes: dict[str, str],
        outcome: PlanSupplyOutcome | None,
        cause_source_types: dict[str, str],
    ) -> dict:
        """Build the dashboard-only zero-penalty LATE/SLIPPING row for a slip absorbed by slack."""
        driver_reason, driver_event_id = self._resolve_driver_for_milestone(plan_id, result.slip_driver_code)
        return {
            "purchase_order_id": purchase_order_id,
            "risk_type": "LATE",
            "status": "SLIPPING",
            "measured_milestone_code": result.measured_milestone_code,
            "projected_measured_date": result.projected_measured_date,
            "window_start": plan_input.window_start,
            "window_end": plan_input.window_end,
            "days_off": None,
            "shortfall_quantity": None,
            "driver_milestone_code": result.slip_driver_code,
            "driver_reason_code": driver_reason,
            "driver_event_id": driver_event_id,
            "projected_penalty_amount": 0.0,
            "currency_code": "USD",
            "priced_rule_ids": [],
            "projected_milestones": projected_milestones,
            "calculation_detail": self._calculation_detail(
                plan_input,
                result,
                None,
                basis,
                stacking_mode,
                rule_codes,
                outcome,
                cause_source_types,
                "LATE",
                result.slip_driver_code,
            ),
        }

    def _option_row(self, purchase_order_id: UUID, option: EvaluatedOption) -> dict:
        """Build one `fulfillment_mitigation_option` row from an evaluated mitigation option."""
        return {
            "purchase_order_id": purchase_order_id,
            "action_code": option.action_code,
            "owner_team": option.owner_team,
            "feasible": option.feasible,
            "infeasible_reason": option.infeasible_reason,
            "act_by_date": option.act_by_date,
            "penalty_before": option.penalty_before,
            "penalty_after": option.penalty_after,
            "action_cost": option.action_cost,
            "net_saving": option.net_saving,
            "confidence": option.confidence,
            "rank_no": option.rank_no,
            "addresses_risk_types": list(option.addresses_risk_types),
            "rationale": option.rationale,
        }
