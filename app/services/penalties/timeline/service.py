"""Orchestrates fulfillment-timeline projection runs: supply, projection, pricing, mitigation, persist.

Entry points: run_for_all_open (the daily batch), run_for_plan (one plan, on demand).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from app.core.exceptions import NotFoundError
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.services.penalties.timeline.alerts import AlertState, CurrentRisk, plan_alert_changes
from app.services.penalties.timeline.assessment import assess_plan
from app.services.penalties.timeline.mitigation import evaluate_mitigations
from app.services.penalties.timeline.pricing import PricingBasis
from app.services.penalties.timeline.risk_rows import RiskRowAssembler
from app.services.penalties.timeline.supply import PlanSupplyOutcome
from app.services.penalties.timeline.supply_inputs import SupplyInputBuilder
from app.services.penalties.timeline.types import MilestoneDefinition, MilestoneState, PlanTimelineInput
from app.utils.clock import utc_now, utc_today

_OPEN_RISK_STATUSES = ("PROJECTED_BREACH", "BREACHED")

_STATUS_RANK = {"ON_TRACK": 0, "SLIPPING": 1, "PROJECTED_BREACH": 2, "BREACHED": 3}


@dataclass
class PlanProjectionOutcome:
    plan_id: UUID
    skipped: bool
    status: str | None = None
    total_projected_penalty: float = 0.0
    risk_count: int = 0
    option_count: int = 0


@dataclass
class TimelineRunSummary:
    plans_evaluated: int = 0
    plans_skipped: int = 0
    skipped_plan_ids: list[UUID] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    total_projected_penalty: float = 0.0


@dataclass
class TimelineProjectionService:
    """Orchestrates fulfillment-timeline runs for one plan or every open plan.

    Two entry points: run_for_all_open (once per (material, plant) pool across
    every open plan) and run_for_plan (supply computed for just that plan's own
    pools, but still against every open plan's competing demand). Both delegate
    the pure projection/pricing math to `assess_plan` (via `RiskRowAssembler`),
    so a plan's own run and its mitigation repricer can never diverge. Commits
    once per plan, right after persisting its rows, so a later plan's failure
    can never roll back an earlier plan's already-written rows.
    """

    timeline: FulfillmentTimelineRepository
    risks: FulfillmentRiskRepository
    alerts: TimelineAlertRepository
    purchase_orders: PurchaseOrderRepository
    rules: PenaltyRuleRepository
    master_data: MasterDataRepository

    def __post_init__(self) -> None:
        self._supply = SupplyInputBuilder(timeline=self.timeline, purchase_orders=self.purchase_orders)
        self._rows = RiskRowAssembler(timeline=self.timeline)

    def run_for_all_open(self, projection_date: date | None = None) -> TimelineRunSummary:
        """Project every OPEN/SHIPPED fulfillment plan for one run date, persist, and summarize.

        A plan's rows are committed as soon as they're persisted; if a later
        plan's processing raises, that exception propagates once every prior
        plan's rows are already durably committed.
        """
        projection_date = projection_date or utc_today()
        definitions = self.timeline.list_milestone_definitions()
        open_plans = self.timeline.list_open_plans()

        pools: set[tuple[UUID, UUID]] = set()
        for plan in open_plans:
            pools |= self._supply.pools_for_plan(plan)
        outcomes, cause_source_types = self._supply.compute_supply_outcomes(pools, projection_date)

        summary = TimelineRunSummary()
        for plan in open_plans:
            outcome = outcomes.get(str(plan["id"]))
            plan_outcome = self._run_one_plan(plan, definitions, outcome, cause_source_types, projection_date)
            self._fold_into_summary(summary, plan_outcome)
        return summary

    def run_for_plan(self, plan_id: UUID, projection_date: date | None = None) -> PlanProjectionOutcome:
        """Project one fulfillment plan for one run date, persist, and return its outcome."""
        plan = self.timeline.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(
                code="FULFILLMENT_PLAN_NOT_FOUND", message=f"No fulfillment plan found with plan_id={plan_id}"
            )

        projection_date = projection_date or utc_today()
        definitions = self.timeline.list_milestone_definitions()
        pools = self._supply.pools_for_plan(plan)
        outcomes, cause_source_types = self._supply.compute_supply_outcomes(pools, projection_date)
        outcome = outcomes.get(str(plan_id))
        return self._run_one_plan(plan, definitions, outcome, cause_source_types, projection_date)

    def _fold_into_summary(self, summary: TimelineRunSummary, outcome: PlanProjectionOutcome) -> None:
        """Accumulate one plan's outcome into the running batch summary."""
        if outcome.skipped:
            summary.plans_skipped += 1
            summary.skipped_plan_ids.append(outcome.plan_id)
            return
        summary.plans_evaluated += 1
        summary.total_projected_penalty = round(
            summary.total_projected_penalty + outcome.total_projected_penalty, 2
        )
        assert outcome.status is not None
        summary.status_counts[outcome.status] = summary.status_counts.get(outcome.status, 0) + 1

    def _run_one_plan(
        self,
        plan: dict,
        definitions: Sequence[MilestoneDefinition],
        outcome: PlanSupplyOutcome | None,
        cause_source_types: dict[str, str],
        projection_date: date,
    ) -> PlanProjectionOutcome:
        """Project, price, persist, and summarize one plan; skipped when its PO has no delivery window."""
        purchase_order = self.purchase_orders.require_purchase_order(plan["purchase_order_id"])
        if purchase_order["window_start"] is None or purchase_order["window_end"] is None:
            return PlanProjectionOutcome(plan_id=plan["id"], skipped=True)

        milestone_rows = self.timeline.list_milestones(plan["id"])
        milestones_by_code = {m["code"]: m for m in milestone_rows}
        milestones = tuple(
            MilestoneState(
                code=m["code"],
                baseline_date=m["baseline_date"],
                planned_date=m["planned_date"],
                actual_date=m["actual_date"],
            )
            for m in milestone_rows
        )
        material_available = milestones_by_code.get("MATERIAL_AVAILABLE") or {}
        material_available_done = material_available.get("actual_date") is not None

        not_before: dict[str, date] = {}
        supply_not_before = outcome.material_available_not_before if outcome is not None else None
        if supply_not_before is not None and not material_available_done:
            not_before["MATERIAL_AVAILABLE"] = supply_not_before

        plan_input = PlanTimelineInput(
            plan_id=str(plan["id"]),
            freight_term=plan["freight_term"],
            planned_transit_days=plan["planned_transit_days"],
            window_start=purchase_order["window_start"],
            window_end=purchase_order["window_end"],
            cancel_date=purchase_order["cancel_date"],
            milestones=milestones,
            as_of=projection_date,
            not_before=not_before,
        )

        enriched_lines = self._supply.enrich_lines(plan["id"])
        goods_issued_done = (milestones_by_code.get("GOODS_ISSUED") or {}).get("actual_date") is not None
        if goods_issued_done:
            shortfall_quantity = max(
                0.0,
                sum(
                    line["planned_quantity"] - line["shipped_quantity"]
                    for line in enriched_lines
                    if line["shipped_quantity"] is not None
                ),
            )
            shortfall_status = "BREACHED" if shortfall_quantity > 0 else None
            cause_code = None
        else:
            shortfall_quantity = outcome.shortfall_quantity if outcome is not None else 0.0
            shortfall_status = "PROJECTED_BREACH" if shortfall_quantity > 0 else None
            cause_code = outcome.cause_code if outcome is not None else None

        basis = self._pricing_basis(enriched_lines)
        rules = self.rules.list_rules_for_retailer_effective_on(
            purchase_order["retailer_id"], projection_date
        )
        stacking_mode = self.master_data.get_stacking_mode(purchase_order["retailer_id"])

        assessment = assess_plan(
            definitions, plan_input, shortfall_quantity, shortfall_status, basis, rules, stacking_mode
        )

        rule_codes = self.rules.get_rule_codes([UUID(rule.rule_id) for rule in rules])
        cause_source_id = outcome.cause_source_id if outcome is not None else None
        risk_rows = self._rows.build_risk_rows(
            plan["id"],
            purchase_order["id"],
            plan_input,
            assessment,
            shortfall_quantity,
            cause_code,
            cause_source_id,
            cause_source_types,
            basis,
            stacking_mode,
            rule_codes,
            outcome,
        )
        total_penalty = round(sum(row["projected_penalty_amount"] for row in risk_rows), 2)
        breach_amounts = [
            row["projected_penalty_amount"] for row in risk_rows if row["status"] == "PROJECTED_BREACH"
        ]

        option_rows: list[dict] = []
        if breach_amounts:
            situation = self._rows.build_situation(
                plan["id"],
                plan_input,
                definitions,
                assessment,
                breach_amounts,
                cause_code,
                cause_source_id,
                cause_source_types,
                shortfall_quantity,
                outcome,
                stacking_mode,
            )
            reprice = self._rows.make_reprice(definitions, basis, rules, stacking_mode)
            option_rows = self._rows.option_rows(
                purchase_order["id"], evaluate_mitigations(situation, reprice)
            )

        self.risks.replace_for_plan_date(plan["id"], projection_date, risk_rows, option_rows)
        self._sync_alerts(plan["id"], purchase_order["id"], risk_rows, projection_date)
        self.risks.commit()

        return PlanProjectionOutcome(
            plan_id=plan["id"],
            skipped=False,
            status=self._worst_status(risk_rows),
            total_projected_penalty=total_penalty,
            risk_count=len(risk_rows),
            option_count=len(option_rows),
        )

    def _sync_alerts(
        self, plan_id: UUID, purchase_order_id: UUID, risk_rows: list[dict], projection_date: date
    ) -> None:
        """Create/update/close this plan's tracked alerts against this run's PROJECTED_BREACH/BREACHED rows.

        SLIPPING (and ON_TRACK) rows never reach `plan_alert_changes` -- only
        a row already priced as a breach can start or keep tracking an alert.
        """
        current_risks = [
            CurrentRisk(
                risk_type=row["risk_type"],
                status=row["status"],
                penalty_amount=row["projected_penalty_amount"],
                days_off=row["days_off"],
                shortfall_quantity=row["shortfall_quantity"],
            )
            for row in risk_rows
            if row["status"] in _OPEN_RISK_STATUSES
        ]
        tracking = [
            AlertState(
                alert_id=row["alert_id"],
                risk_type=row["risk_type"],
                status=row["status"],
                last_seen_date=row["last_seen_date"],
                prev_penalty_amount=row["prev_penalty_amount"],
                last_penalty_amount=row["last_penalty_amount"],
                prev_days_off=row["prev_days_off"],
                last_days_off=row["last_days_off"],
                prev_shortfall_quantity=row["prev_shortfall_quantity"],
                last_shortfall_quantity=row["last_shortfall_quantity"],
            )
            for row in self.alerts.list_tracking_for_plan(plan_id)
        ]
        changes = plan_alert_changes(projection_date, current_risks, tracking, now=utc_now())
        self.alerts.apply_changes(plan_id, purchase_order_id, changes)

    def _pricing_basis(self, enriched_lines: list[dict]) -> PricingBasis:
        """Quantity-weighted unit price/cost across a plan's lines.

        `unit_cost` is None unless every line has a known `standard_cost`.
        """
        quantity = sum(line["planned_quantity"] for line in enriched_lines)
        if quantity <= 0:
            return PricingBasis(quantity=0.0, unit_cost=None, unit_price=0.0)

        unit_price = sum(line["planned_quantity"] * line["unit_price"] for line in enriched_lines) / quantity

        cost_pairs: list[tuple[float, float]] = []
        for line in enriched_lines:
            material_master = (
                self.timeline.get_material_master(line["material_id"], line["plant_id"])
                if line["material_id"] is not None and line["plant_id"] is not None
                else None
            )
            if material_master is None or material_master["standard_cost"] is None:
                cost_pairs = []
                break
            cost_pairs.append((line["planned_quantity"], material_master["standard_cost"]))

        unit_cost = sum(q * c for q, c in cost_pairs) / quantity if cost_pairs else None
        return PricingBasis(quantity=quantity, unit_cost=unit_cost, unit_price=unit_price)

    def _worst_status(self, risk_rows: list[dict]) -> str:
        """The plan's overall status this run: the highest-severity status among its risk rows."""
        if not risk_rows:
            return "ON_TRACK"
        return max((row["status"] for row in risk_rows), key=lambda status: _STATUS_RANK.get(status, 0))
