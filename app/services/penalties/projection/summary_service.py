"""Penalty-projection-summary service.

Delegates the shared get_or_schedule, reuse, and lifecycle machinery to
`SummaryServiceBase`, supplying only the projection-specific parts: which
repositories back the projection history, which agent and prompt to use, and
how the mandatory context and content fingerprint are assembled.

Reads and writes the merged `penalties.penalty_summary` table under
`summary_type=SummaryType.PROJECTION` (see `_summary_base.py`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.tools import BaseTool

from app.agents.penalties._summary_output import PenaltySummaryOutputBase
from app.agents.penalties.projection import (
    ActiveRule,
    ActualOutcome,
    DailyHistoryEntry,
    OrderContext,
    PenaltyProjectionSummaryContext,
    PenaltyProjectionSummaryOutput,
    TierBand,
    ViolationEntry,
    build_penalty_projection_summary_tools,
)
from app.agents.penalties.projection.agent import PenaltyProjectionAgent
from app.agents.penalties.projection.prompts.v1 import PROMPT_VERSION, SYSTEM_PROMPT
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.exceptions import BusinessRuleError, NotFoundError, ValidationError
from app.models.enums import JobTaskType, SummaryType
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties._summary_base import (  # noqa: F401  (SummaryJob re-exported via __init__)
    SummaryJob,
    SummaryServiceBase,
)
from app.services.penalties.projection import DELAY_VIOLATION_TYPES, SHORTAGE_VIOLATION_TYPES
from app.services.penalties.projection.service import ProjectionService
from app.utils.clock import utc_today

if TYPE_CHECKING:
    from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
    from app.repositories.penalties.rule import PenaltyRuleRepository

logger = logging.getLogger(__name__)

_UPSTREAM_FAILURE_MESSAGE = "Penalty projection summary generation failed upstream"


class PenaltyProjectionSummaryOutputWithReuse(PenaltyProjectionSummaryOutput):
    """`PenaltyProjectionSummaryOutput` with fields describing narrative reuse."""

    is_reused: bool = False
    generated_for_date: date | None = None
    unchanged_since: date | None = None
    unchanged_for_days: int | None = None


class ProjectionSummaryService(
    SummaryServiceBase[PenaltyProjectionSummaryContext, PenaltyProjectionSummaryOutputWithReuse]
):
    """Projection-specific half of the shared summary-service machinery.

    Supplies the extra domain repositories, the agent and prompt, and how the
    mandatory context, fingerprint, and tool set are built.
    `SummaryServiceBase` owns everything else.
    """

    summary_type = SummaryType.PROJECTION
    summary_domain = "projection"
    agent_code = "penalty_projection_summary"
    agent_name = "Penalty Projection Summary"
    prompt_version = PROMPT_VERSION
    system_prompt = SYSTEM_PROMPT
    job_task_type = JobTaskType.PROJECTION_SUMMARY_REGEN
    upstream_failure_message = _UPSTREAM_FAILURE_MESSAGE

    def __init__(
        self,
        *,
        purchase_orders: PurchaseOrderRepository,
        summaries: PenaltySummaryRepository,
        agent_registry: AgentRegistryRepository,
        job_queue: JobQueueRepository,
        job_context: PenaltyJobItemContextRepository,
        llm: AzureOpenAIChatClient,
        rules: PenaltyRuleRepository,
        master_data: MasterDataRepository,
        projections: PenaltyProjectionRepository,
        actual_penalties: ActualPenaltyRepository,
        projection_service: ProjectionService,
    ) -> None:
        super().__init__(
            purchase_orders=purchase_orders,
            summaries=summaries,
            agent_registry=agent_registry,
            job_queue=job_queue,
            job_context=job_context,
            llm=llm,
        )
        self._agent = PenaltyProjectionAgent(
            llm=llm,
            agent_registry=agent_registry,
            agent_code=self.agent_code,
            summary_domain=self.summary_domain,
            upstream_failure_message=self.upstream_failure_message,
        )
        self.rules = rules
        self.master_data = master_data
        self.projections = projections
        self.actual_penalties = actual_penalties
        self.projection_service = projection_service

    # ------------------------------------------------------------------
    # SummaryServiceBase hooks
    # ------------------------------------------------------------------

    def _validate(self, purchase_order_id: UUID, as_of_date: date | None) -> tuple[dict, date, list[dict]]:
        """Confirm the purchase order and its projection history exist and are in range.

        `as_of_date` must fall between the earliest recorded projection date and
        today; a date outside that window has nothing real to narrate. The
        returned `history` is unbounded, because bounding it to `as_of_date`
        belongs to `_assemble_mandatory_context`.
        """
        logger.info(
            "Penalty projection summary requested for purchase_order_id=%s as_of_date=%s",
            purchase_order_id,
            as_of_date,
        )

        purchase_order = self.purchase_orders.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        today = utc_today()
        as_of_date = as_of_date or today

        history = self.projections.list_history(purchase_order_id)
        if not history:
            raise BusinessRuleError(
                code="NO_PROJECTION_EXISTS",
                message=f"No projections exist yet for purchase_order_id={purchase_order_id}.",
            )

        earliest_projection_date = min(row["projection_date"] for row in history)

        if as_of_date > today:
            raise ValidationError(
                code="INVALID_AS_OF_DATE",
                message=f"as_of_date={as_of_date.isoformat()} is in the future.",
            )

        if as_of_date < earliest_projection_date:
            raise ValidationError(
                code="INVALID_AS_OF_DATE",
                message=(
                    f"as_of_date={as_of_date.isoformat()} predates the earliest "
                    f"projection date, {earliest_projection_date.isoformat()}."
                ),
            )

        return purchase_order, as_of_date, history

    def _history_for_generation(self, purchase_order_id: UUID, as_of_date: date) -> list[dict]:
        """Every projection row on record for this order; the job was validated at schedule time."""
        return self.projections.list_history(purchase_order_id)

    def _assemble_mandatory_context(
        self, purchase_order: dict, as_of_date: date, history: list[dict]
    ) -> PenaltyProjectionSummaryContext:
        """Build the mandatory (non-tool-fetched) LLM context for one projection narrative.

        `history` is bounded to rows on or before `as_of_date` so a backfilled
        request cannot leak future rows into what the narrative treats as
        current. `actual_outcomes` stays `None` until the order is DELIVERED,
        which distinguishes "not yet known" from "known to be empty".
        """
        purchase_order_id = purchase_order["id"]
        retailer_id = purchase_order["retailer_id"]
        rules = self.rules.list_rules_for_retailer(retailer_id)

        bounded_history = [row for row in history if row["projection_date"] <= as_of_date]

        active_rules = [
            ActiveRule(
                rule_id=rule.rule_id,
                violation_type=rule.violation_type,
                calc_type=rule.calc_type.value,
                rate=rule.rate,
                threshold_pct=rule.threshold_pct,
                cap_amount=rule.cap_amount,
                tiers=[
                    TierBand(band_min=tier.band_min, band_max=tier.band_max, rate=tier.rate)
                    for tier in (rule.tiers or [])
                ]
                or None,
            )
            for rule in rules
        ]

        daily_history = self._build_daily_history(purchase_order_id, bounded_history)

        retailer_name = next(
            (r["retailer_name"] for r in self.master_data.list_retailers() if r["id"] == retailer_id),
            str(retailer_id),
        )

        lines = self.purchase_orders.list_lines(purchase_order_id)
        primary_line = lines[0] if lines else None
        sku_description = self._resolve_sku_description(primary_line, purchase_order_id)

        carrier_id = None
        carrier_name = None
        shipment = self.projection_service.fulfillment.get_latest_shipment_for_purchase_order_not_after(
            purchase_order_id, as_of_date
        )
        if shipment is not None and shipment["carrier_id"] is not None:
            carrier_id = shipment["carrier_id"]
            carrier = self.master_data.get_carrier(carrier_id)
            carrier_name = carrier["carrier_name"] if carrier is not None else None

        other_open_orders: list[str] = []
        if primary_line is not None and primary_line["material_id"] and primary_line["plant_id"]:
            other_open_orders = [
                str(po_id)
                for po_id in self.purchase_orders.list_open_orders_for_material_plant(
                    primary_line["material_id"], primary_line["plant_id"], purchase_order_id
                )
            ]

        actual_outcomes = None
        if purchase_order["order_status"] == "DELIVERED":
            actual_outcomes = [
                ActualOutcome(
                    violation_type=penalty["violation_type"],
                    actual_penalty_amount=penalty["actual_penalty_amount"],
                    invoice_or_deduction_date=penalty["invoice_or_deduction_date"],
                )
                for penalty in self.actual_penalties.list_for_purchase_order(purchase_order_id)
            ]

        snapshot = self.projection_service.build_snapshot(purchase_order_id, as_of_date)

        return PenaltyProjectionSummaryContext(
            order=OrderContext(
                order_id=str(purchase_order_id),
                order_status=purchase_order["order_status"],
                retailer_name=retailer_name,
                sku_description=sku_description,
                order_qty=snapshot.order_qty,
                unit_price=snapshot.unit_price,
                required_ship_date=snapshot.required_ship_date,
                requested_delivery_date=snapshot.requested_delivery_date,
                carrier_id=str(carrier_id) if carrier_id is not None else None,
                carrier_name=carrier_name,
            ),
            current_projection_date=as_of_date,
            stacking_mode=self.master_data.get_stacking_mode(retailer_id),
            active_rules=active_rules,
            daily_history=daily_history,
            shared_production_line=bool(other_open_orders),
            other_open_orders_same_sku_location=other_open_orders,
            actual_outcomes=actual_outcomes,
        )

    def _compute_content_fingerprint(self, context: PenaltyProjectionSummaryContext) -> str:
        return _compute_content_fingerprint(context)

    def _build_tools(self, purchase_order: dict, as_of_date: date) -> list[BaseTool]:
        """Build this call's bounded tool set: carrier reliability, actual penalties, tier bands.

        `order_status` is passed through directly rather than wrapped in a tool
        so the prompt can gate whether the actual-penalties tool is worth
        calling at all; it returns rows only once the order is DELIVERED.
        """
        purchase_order_id = purchase_order["id"]
        retailer_id = purchase_order["retailer_id"]
        return build_penalty_projection_summary_tools(
            carrier_reliability=self._get_carrier_reliability,
            actual_penalties=lambda: self.actual_penalties.list_for_purchase_order(purchase_order_id),
            tier_bands=lambda rule_id: self._get_tier_bands(retailer_id, rule_id),
            order_status=purchase_order["order_status"],
        )

    def _output_with_reuse_cls(self) -> type[PenaltyProjectionSummaryOutputWithReuse]:
        return PenaltyProjectionSummaryOutputWithReuse

    def _generate(
        self,
        context: PenaltyProjectionSummaryContext,
        *,
        order_id: str,
        as_of_date: date,
        tools: list[BaseTool],
        heartbeat: Callable[[], None] | None,
    ) -> PenaltySummaryOutputBase:
        """Delegate to `PenaltyProjectionAgent.generate_projection_summary` for the tool-calling loop."""
        return self._agent.generate_projection_summary(
            context,
            order_id=order_id,
            as_of_date=as_of_date,
            tools=tools,
            heartbeat=heartbeat,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_daily_history(
        self, purchase_order_id: UUID, bounded_history: list[dict]
    ) -> list[DailyHistoryEntry]:
        """One entry per distinct projection_date.

        Combines that day's engine outputs from `bounded_history` with that
        day's inputs from `ProjectionService.build_snapshot`.
        """
        entries: list[DailyHistoryEntry] = []

        for day in sorted({row["projection_date"] for row in bounded_history}):
            day_rows = [row for row in bounded_history if row["projection_date"] == day]
            snapshot = self.projection_service.build_snapshot(purchase_order_id, day)

            shortage_probability = next(
                (
                    r["failure_probability"]
                    for r in day_rows
                    if r["violation_type"] in SHORTAGE_VIOLATION_TYPES
                ),
                0.0,
            )
            delay_probability = next(
                (r["failure_probability"] for r in day_rows if r["violation_type"] in DELAY_VIOLATION_TYPES),
                0.0,
            )

            violations = [
                ViolationEntry(
                    violation_type=r["violation_type"],
                    rule_id=str(r["rule_id"]),
                    probability=r["failure_probability"],
                    penalty_amount=r["penalty_amount"],
                    expected_penalty_amount=r["expected_penalty_amount"],
                )
                for r in day_rows
            ]

            entries.append(
                DailyHistoryEntry(
                    entry_date=day,
                    confirmed_qty=snapshot.confirmed_qty,
                    production_status=snapshot.production_status.value,
                    appointment_status=snapshot.appointment_status.value,
                    actual_ship_date=snapshot.actual_ship_date,
                    expected_ship_date_override=snapshot.expected_ship_date,
                    demand_exception_flagged=snapshot.demand_exception_flagged,
                    days_to_delivery=day_rows[0]["days_to_delivery"],
                    shortage_probability=shortage_probability,
                    delay_probability=delay_probability,
                    violations=violations,
                    total_expected_penalty_amount=round(
                        sum(r["expected_penalty_amount"] for r in day_rows), 2
                    ),
                )
            )
        return entries

    def _resolve_sku_description(self, primary_line: dict | None, purchase_order_id: UUID) -> str:
        """Best-effort human-readable label for the order's primary line.

        Falls back through SKU description, SKU code, retailer material code,
        material id, then the purchase-order id, so the narrative always has
        something to reference even when master data is incomplete.
        """
        if primary_line is None:
            return str(purchase_order_id)
        if primary_line["sku_id"] is not None:
            sku = next((s for s in self.master_data.list_skus() if s["id"] == primary_line["sku_id"]), None)
            if sku is not None:
                return sku["description"] or sku["sku_code"]
        if primary_line["retailer_material_code"]:
            return primary_line["retailer_material_code"]
        if primary_line["material_id"] is not None:
            return str(primary_line["material_id"])
        return str(purchase_order_id)

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _get_carrier_reliability(self, carrier_id: str) -> dict[str, Any]:
        """Tool implementation: look up a carrier's on-time-reliability record by id.

        `carrier_id` arrives as a raw string from tool-call arguments. Both a
        malformed and an unknown id degrade to `{"found": False}`, so the agent
        loop never crashes on a bad lookup.
        """
        try:
            carrier = self.master_data.get_carrier(UUID(str(carrier_id)))
        except ValueError:
            carrier = None
        if carrier is None:
            return {"carrier_id": carrier_id, "found": False}
        return {**carrier, "found": True}

    def _get_tier_bands(self, retailer_id: UUID, rule_id: str) -> dict[str, Any]:
        """Tool implementation: the tier bands for one TIERED penalty rule, for the agent to cite verbatim.

        Returns `{"rule_id": ..., "found": False}` when the rule can't be
        resolved for this retailer, so the agent loop degrades gracefully
        instead of crashing on an unknown or stale `rule_id`.
        """
        rules = self.rules.list_rules_for_retailer(retailer_id)
        rule = next((rule for rule in rules if rule.rule_id == rule_id), None)

        if rule is None:
            return {"rule_id": rule_id, "found": False}

        return {
            "rule_id": rule.rule_id,
            "calc_type": rule.calc_type.value,
            "tiers": [
                {"band_min": tier.band_min, "band_max": tier.band_max, "rate": tier.rate}
                for tier in (rule.tiers or [])
            ],
        }


def _fmt_number(value: float) -> str:
    """Format numbers deterministically for fingerprinting."""
    return f"{float(value):.6f}"


def _compute_content_fingerprint(context: PenaltyProjectionSummaryContext) -> str:
    """Hash the facts that determine the generated narrative.

    A pure, DB-free function over the current day's engine outputs and material
    facts. Deliberately excludes `as_of_date`, `current_projection_date`, any
    generated-at timestamp, and `days_to_delivery`, so a later re-run over
    unchanged facts fingerprints identically.
    """
    current_entry = context.daily_history[-1] if context.daily_history else None

    violations = sorted(
        (v.violation_type, _fmt_number(v.probability), _fmt_number(v.expected_penalty_amount))
        for v in (current_entry.violations if current_entry is not None else [])
    )
    active_rule_ids = sorted(rule.rule_id for rule in context.active_rules)

    payload = {
        "violations": violations,
        "stacking_mode": context.stacking_mode,
        "total_expected_penalty_amount": _fmt_number(
            current_entry.total_expected_penalty_amount if current_entry else 0.0
        ),
        "order_status": context.order.order_status,
        "confirmed_qty": current_entry.confirmed_qty if current_entry else None,
        "production_status": current_entry.production_status if current_entry else None,
        "appointment_status": current_entry.appointment_status if current_entry else None,
        "actual_ship_date": (
            current_entry.actual_ship_date.isoformat()
            if current_entry is not None and current_entry.actual_ship_date is not None
            else None
        ),
        "expected_ship_date_override": (
            current_entry.expected_ship_date_override.isoformat()
            if current_entry is not None and current_entry.expected_ship_date_override is not None
            else None
        ),
        "demand_exception_flagged": current_entry.demand_exception_flagged if current_entry else None,
        "active_rule_ids": active_rule_ids,
    }

    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
