"""Penalty-mitigation-summary service.

Delegates the shared get_or_schedule, reuse, and lifecycle machinery to
`SummaryServiceBase`, supplying only the mitigation-specific parts: which
repositories back the mitigation options, which agent and prompt to use, and
how the mandatory context and content fingerprint are assembled.

Reads and writes the merged `penalties.penalty_summary` table under
`summary_type=SummaryType.MITIGATION` (see `_summary_base.py`). Unlike the
projection service, the fingerprint hashes the mitigation options themselves,
which is what actually determines whether the narrative would change, rather
than projection-specific fields like `days_to_delivery`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import UUID

from langchain_core.tools import BaseTool

from app.agents.penalties._summary_output import PenaltySummaryOutputBase
from app.agents.penalties.mitigation import (
    ActualOutcome,
    MitigationOptionContext,
    OrderContext,
    PenaltyMitigationSummaryContext,
    PenaltyMitigationSummaryOutput,
    build_penalty_mitigation_summary_tools,
)
from app.agents.penalties.mitigation.agent import PenaltyMitigationAgent
from app.agents.penalties.mitigation.prompts.v2 import PROMPT_VERSION, SYSTEM_PROMPT
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.exceptions import BusinessRuleError, NotFoundError, ValidationError
from app.models.enums import JobTaskType, SummaryType
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.mitigation import MitigationOptionRepository
from app.repositories.penalties.projection import ActualPenaltyRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties._summary_base import (  # noqa: F401  (SummaryJob re-exported via __init__)
    SummaryJob,
    SummaryServiceBase,
)
from app.services.penalties.projection.service import ProjectionService
from app.utils.clock import utc_today

logger = logging.getLogger(__name__)

_UPSTREAM_FAILURE_MESSAGE = "Penalty mitigation summary generation failed upstream"


class PenaltyMitigationSummaryOutputWithReuse(PenaltyMitigationSummaryOutput):
    """`PenaltyMitigationSummaryOutput` with fields describing narrative reuse."""

    is_reused: bool = False
    generated_for_date: date | None = None
    unchanged_since: date | None = None
    unchanged_for_days: int | None = None


class MitigationSummaryService(
    SummaryServiceBase[PenaltyMitigationSummaryContext, PenaltyMitigationSummaryOutputWithReuse]
):
    """Mitigation-specific half of the shared summary-service machinery."""

    summary_type = SummaryType.MITIGATION
    summary_domain = "mitigation"
    agent_code = "penalty_mitigation_summary"
    agent_name = "Penalty Mitigation Summary"
    prompt_version = PROMPT_VERSION
    system_prompt = SYSTEM_PROMPT
    job_task_type = JobTaskType.MITIGATION_SUMMARY_REGEN
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
        master_data: MasterDataRepository,
        mitigation_options: MitigationOptionRepository,
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
        self._agent = PenaltyMitigationAgent(
            llm=llm,
            agent_registry=agent_registry,
            agent_code=self.agent_code,
            summary_domain=self.summary_domain,
            upstream_failure_message=self.upstream_failure_message,
        )
        self.master_data = master_data
        self.mitigation_options = mitigation_options
        self.actual_penalties = actual_penalties
        self.projection_service = projection_service

    # ------------------------------------------------------------------
    # SummaryServiceBase hooks
    # ------------------------------------------------------------------

    def _validate(self, purchase_order_id: UUID, as_of_date: date | None) -> tuple[dict, date, list[dict]]:
        """Confirm the purchase order and its mitigation-option history exist and are in range.

        `as_of_date` must fall between the earliest mitigation-options date on
        record and today; a date outside that window has nothing real to
        narrate. The returned `history` is the latest option set not after
        `as_of_date`, one row per candidate action including ACCEPT.
        """
        logger.info(
            "Penalty mitigation summary requested for purchase_order_id=%s as_of_date=%s",
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

        earliest_options_date = self.mitigation_options.earliest_date(purchase_order_id)
        latest_options_date = self.mitigation_options.latest_date(purchase_order_id)
        if earliest_options_date is None or latest_options_date is None:
            raise BusinessRuleError(
                code="NO_MITIGATION_OPTIONS_EXIST",
                message=f"No mitigation options exist yet for purchase_order_id={purchase_order_id}.",
            )

        max_allowed_date = max(today, latest_options_date)
        if as_of_date > max_allowed_date:
            raise ValidationError(
                code="INVALID_AS_OF_DATE",
                message=f"as_of_date={as_of_date.isoformat()} is beyond recorded mitigation horizon ({max_allowed_date.isoformat()}).",
            )

        if as_of_date < earliest_options_date:
            raise ValidationError(
                code="INVALID_AS_OF_DATE",
                message=(
                    f"as_of_date={as_of_date.isoformat()} predates the earliest "
                    f"mitigation-options date, {earliest_options_date.isoformat()}."
                ),
            )

        options_rows = self.mitigation_options.get_latest_not_after(purchase_order_id, as_of_date)
        return purchase_order, as_of_date, options_rows

    def _history_for_generation(self, purchase_order_id: UUID, as_of_date: date) -> list[dict]:
        """Mitigation-option rows in effect as of `as_of_date`; the job was validated at schedule time."""
        return self.mitigation_options.get_latest_not_after(purchase_order_id, as_of_date)

    def _assemble_mandatory_context(
        self, purchase_order: dict, as_of_date: date, history: list[dict]
    ) -> PenaltyMitigationSummaryContext:
        """Build the mandatory (non-tool-fetched) LLM context for one mitigation narrative.

        The ACCEPT row's `projected_penalty_after` becomes the baseline penalty
        every other option is compared against. `actual_outcomes` stays `None`
        until the order is DELIVERED rather than becoming an empty list, because
        the agent's prompt treats "not yet known" and "known to be empty"
        differently.
        """
        purchase_order_id = purchase_order["id"]
        retailer_id = purchase_order["retailer_id"]

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

        stacking_mode = self.master_data.get_stacking_mode(retailer_id)

        mitigation_options = [
            MitigationOptionContext(
                action=row["action"],
                projected_penalty_after=row["projected_penalty_after"],
                action_cost=row["action_cost"],
                net_saving=row["net_saving"],
                risk_level=row["risk_level"],
                confidence=row["confidence"],
                rationale=row["rationale"],
            )
            for row in history
        ]

        accept_row = next((row for row in history if row["action"] == "ACCEPT"), None)
        current_total_expected_penalty_amount = accept_row["projected_penalty_after"] if accept_row else 0.0

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

        return PenaltyMitigationSummaryContext(
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
            current_total_expected_penalty_amount=current_total_expected_penalty_amount,
            stacking_mode=stacking_mode,
            mitigation_options=mitigation_options,
            actual_outcomes=actual_outcomes,
        )

    def _compute_content_fingerprint(self, context: PenaltyMitigationSummaryContext) -> str:
        return _compute_content_fingerprint(context)

    def _build_tools(self, purchase_order: dict, as_of_date: date) -> list[BaseTool]:
        """Build this call's bounded tool set: carrier reliability and actual penalties.

        `order_status` is passed through directly rather than wrapped in a tool
        so the prompt can gate whether the actual-penalties tool is worth
        calling at all; it returns rows only once the order is DELIVERED.
        """
        purchase_order_id = purchase_order["id"]
        return build_penalty_mitigation_summary_tools(
            carrier_reliability=self._get_carrier_reliability,
            actual_penalties=lambda: self.actual_penalties.list_for_purchase_order(purchase_order_id),
            order_status=purchase_order["order_status"],
        )

    def _output_with_reuse_cls(self) -> type[PenaltyMitigationSummaryOutputWithReuse]:
        return PenaltyMitigationSummaryOutputWithReuse

    def _generate(
        self,
        context: PenaltyMitigationSummaryContext,
        *,
        order_id: str,
        as_of_date: date,
        tools: list[BaseTool],
        heartbeat: Callable[[], None] | None,
    ) -> PenaltySummaryOutputBase:
        """Delegate to `PenaltyMitigationAgent.generate_mitigation_summary` for the tool-calling loop."""
        return self._agent.generate_mitigation_summary(
            context,
            order_id=order_id,
            as_of_date=as_of_date,
            tools=tools,
            heartbeat=heartbeat,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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


def _fmt_number(value: float) -> str:
    """Format numbers deterministically for fingerprinting."""
    return f"{float(value):.6f}"


def _compute_content_fingerprint(context: PenaltyMitigationSummaryContext) -> str:
    """Hash the facts that determine the generated narrative.

    Hashes the ranked mitigation options themselves, plus the baseline penalty
    and order status, where the projection equivalent hashes the current day's
    violations and statuses. Excludes `current_projection_date`, so a narrative
    whose options are identical to yesterday's stays reusable.
    """
    options = sorted(
        (
            o.action,
            _fmt_number(o.projected_penalty_after),
            _fmt_number(o.action_cost),
            _fmt_number(o.net_saving),
            o.risk_level,
            o.confidence,
        )
        for o in context.mitigation_options
    )

    payload = {
        "options": options,
        "current_total_expected_penalty_amount": _fmt_number(context.current_total_expected_penalty_amount),
        "stacking_mode": context.stacking_mode,
        "order_status": context.order.order_status,
        "has_actual_outcomes": context.actual_outcomes is not None,
    }

    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
