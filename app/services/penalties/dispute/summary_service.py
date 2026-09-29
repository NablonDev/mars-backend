"""Dispute-summary service.

A structural subclass of `SummaryServiceBase` that implements only the shared
hook contract and otherwise uses the base class's `get_or_schedule`,
`get_status`, and `run_generation` unchanged. Reads and writes
`penalties.penalty_summary` under `summary_type=SummaryType.DISPUTE`, keyed by
the same `(purchase_order_id, summary_type, as_of_date)` triple as PROJECTION
and MITIGATION, with no dispute-specific repository methods.

Callers address a dispute by `dispute_id`, so `get_or_schedule_for_dispute` and
`get_status_for_dispute` translate one into its owning `purchase_order_id` plus
`as_of_date=dispute["analyzed_at"].date()` before delegating to the generic
base methods. `_dispute_id_value` records which dispute the current call is
about, so the hooks, which only ever receive `purchase_order_id` and
`as_of_date`, can still assemble per-dispute context. It is instance
bookkeeping, not a second persistence path.

Known limitation of that translation: two disputes on one PO analyzed the same
calendar day resolve to the same summary row, so the second generation
overwrites the first dispute's narrative. Only narrative text is affected;
`penalty_dispute.verdict`, `computed_amount`, and `delta_amount` are stored on
`penalty_dispute` alone. See `docs/architecture/penalty-summary-future-redesign.md`
for the proposed fix.

A narrative can never be requested before a verdict exists:
`_require_analyzed_dispute` refuses an OPEN dispute up front. The LLM only
narrates the persisted verdict; it never computes one.
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
from app.agents.penalties.dispute import (
    DisputeOrderContext,
    DisputeSummaryContext,
    build_dispute_summary_tools,
)
from app.agents.penalties.dispute.agent import DisputeResolutionAgent
from app.agents.penalties.dispute.prompts.v2 import PROMPT_VERSION, SYSTEM_PROMPT
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import get_settings
from app.core.exceptions import BusinessRuleError, NotFoundError
from app.models.enums import JobRunType, JobTaskType, SummaryType
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties._summary_base import (
    SummaryJob,
    SummaryServiceBase,
)
from app.utils.clock import utc_today

logger = logging.getLogger(__name__)

_UPSTREAM_FAILURE_MESSAGE = "Penalty dispute summary generation failed upstream"


class DisputeSummaryOutputWithReuse(PenaltySummaryOutputBase):
    """`DisputeSummaryOutput` with fields describing narrative reuse."""

    is_reused: bool = False
    generated_for_date: date | None = None
    unchanged_since: date | None = None
    unchanged_for_days: int | None = None


class DisputeSummaryService(SummaryServiceBase[DisputeSummaryContext, DisputeSummaryOutputWithReuse]):
    """Generates narrative summaries for already-analyzed penalty disputes.

    Implements the `SummaryServiceBase` hooks for dispute context assembly,
    tool-calling, and content fingerprinting, and translates `dispute_id`-keyed
    requests onto the base class's generic identity. The LLM narrates only; the
    verdict and amount were computed deterministically by the dispute engine.
    """

    summary_type = SummaryType.DISPUTE
    summary_domain = "dispute"
    agent_code = "penalty_dispute_summary"
    agent_name = "Penalty Dispute Summary"
    prompt_version = PROMPT_VERSION
    system_prompt = SYSTEM_PROMPT
    job_task_type = JobTaskType.DISPUTE_SUMMARY_REGEN
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
        disputes: PenaltyDisputeRepository,
        rules: PenaltyRuleRepository,
        master_data: MasterDataRepository,
    ) -> None:
        super().__init__(
            purchase_orders=purchase_orders,
            summaries=summaries,
            agent_registry=agent_registry,
            job_queue=job_queue,
            job_context=job_context,
            llm=llm,
        )
        self._agent = DisputeResolutionAgent(
            llm=llm,
            agent_registry=agent_registry,
            agent_code=self.agent_code,
            upstream_failure_message=self.upstream_failure_message,
        )
        self.disputes = disputes
        self.rules = rules
        self.master_data = master_data
        self._dispute_id_value: UUID | None = None

    # ------------------------------------------------------------------
    # dispute_id-first entry points: translate a dispute_id into its owning
    # (purchase_order_id, as_of_date) and delegate to SummaryServiceBase's
    # generic get_or_schedule/get_status. Neither overrides a base method;
    # there is no dispute-specific persistence to run.
    # ------------------------------------------------------------------

    def get_or_schedule_for_dispute(
        self, dispute_id: UUID, force_regenerate: bool = False
    ) -> SummaryJob[DisputeSummaryOutputWithReuse]:
        """Translate a `dispute_id` into the base class's `(purchase_order_id, as_of_date)` identity.

        Refuses an OPEN dispute, which has no verdict to narrate, and records
        `_dispute_id_value` for the hooks further down the shared call chain.
        """
        self._dispute_id_value = dispute_id
        dispute = self._require_analyzed_dispute(dispute_id)
        as_of_date = dispute["analyzed_at"].date() if dispute["analyzed_at"] else utc_today()
        return self.get_or_schedule(dispute["purchase_order_id"], as_of_date, force_regenerate)

    def get_status_for_dispute(self, dispute_id: UUID) -> SummaryJob[DisputeSummaryOutputWithReuse]:
        """Translate a `dispute_id` into the base class's `(purchase_order_id, as_of_date)` identity.

        Unlike `get_or_schedule_for_dispute`, an OPEN dispute is tolerated: a
        status check is a read, not a request to generate. `as_of_date=None` in
        that case lets the base class's `get_status` resolve it.
        """
        dispute = self.disputes.get_by_id(dispute_id)
        if dispute is None:
            raise NotFoundError(
                code="DISPUTE_NOT_FOUND", message=f"No penalty dispute found with dispute_id={dispute_id}"
            )
        self._dispute_id_value = dispute_id
        as_of_date = dispute["analyzed_at"].date() if dispute["analyzed_at"] else None
        return self.get_status(dispute["purchase_order_id"], as_of_date=as_of_date)

    def _enqueue_regeneration_job(
        self, purchase_order_id: UUID, as_of_date: date, force_regenerate: bool
    ) -> None:
        """Enqueue a regeneration job keyed on `dispute_id` rather than the base class's key.

        The dispute id goes onto `process.job_item.metadata_json` so
        `app.workers.penalty_dispute.run_dispute_summary` can read back which
        dispute a `DISPUTE_SUMMARY_REGEN` item is for; `penalty_job_item_context`
        has no dispute-specific column. Dedupe uses `dispute_id` alone because
        two disputes can share a PO and date, and the base key would wrongly
        dedupe the second dispute's job against the first's.
        """
        dispute_id = self._current_dispute_id()
        settings = get_settings()
        run = self.job_queue.create_run(
            job_type=self.job_task_type,
            trigger_type=JobRunType.ON_DEMAND,
            requested_item_count=1,
        )
        dedupe_key = f"{dispute_id}:{self.job_task_type}"
        item = self.job_queue.enqueue(
            run["id"],
            item_type=self.job_task_type,
            dedupe_key=dedupe_key,
            max_attempts=settings.job_queue.max_attempts,
            metadata={"dispute_id": str(dispute_id)},
        )
        if item is None:
            return
        if self.job_context.get(item["id"]) is not None:
            return
        self.job_context.create(
            job_item_id=item["id"],
            purchase_order_id=purchase_order_id,
            projection_date=as_of_date,
            task_type=self.job_task_type,
            force_regenerate_summary=force_regenerate,
        )

    def _require_analyzed_dispute(self, dispute_id: UUID) -> dict:
        """Fetch a dispute by id and enforce the "no narrative before a verdict" invariant.

        A narrative can only describe a verdict `DisputeResolutionService.analyze` has
        already persisted. An OPEN dispute has no verdict or computed amount, so
        it raises `BusinessRuleError(code="DISPUTE_NOT_ANALYZED")` rather than
        let a hook assemble context out of empty fields.
        """
        dispute = self.disputes.get_by_id(dispute_id)
        if dispute is None:
            raise NotFoundError(
                code="DISPUTE_NOT_FOUND", message=f"No penalty dispute found with dispute_id={dispute_id}"
            )
        if dispute["dispute_status"] == "OPEN":
            raise BusinessRuleError(
                code="DISPUTE_NOT_ANALYZED",
                message=f"Dispute {dispute_id} has not been analyzed yet; POST .../analyze first.",
            )
        return dispute

    def _current_dispute_id(self) -> UUID:
        """Non-optional `_dispute_id_value`, narrowing the type for hooks that run after an entry point."""
        assert self._dispute_id_value is not None, (
            "DisputeSummaryService hook called before get_or_schedule_for_dispute set _dispute_id_value"
        )
        return self._dispute_id_value

    # ------------------------------------------------------------------
    # SummaryServiceBase hooks
    # ------------------------------------------------------------------

    def _validate(self, purchase_order_id: UUID, as_of_date: date | None) -> tuple[dict, date, list[dict]]:
        """Confirm the current dispute is analyzed and the purchase order exists.

        The `_require_analyzed_dispute` check repeats the one in
        `get_or_schedule_for_dispute` because this hook also runs from the
        `force_regenerate` path and cannot assume the caller validated.
        The returned `history` includes the current dispute; `_build_tools`
        filters it back out for the prior-dispute-history tool.
        """
        dispute = self._require_analyzed_dispute(self._current_dispute_id())
        purchase_order = self.purchase_orders.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )
        resolved_date = as_of_date or (
            dispute["analyzed_at"].date() if dispute["analyzed_at"] else utc_today()
        )
        history = self.disputes.list_for_purchase_order(purchase_order_id)
        return purchase_order, resolved_date, history

    def _history_for_generation(self, purchase_order_id: UUID, as_of_date: date) -> list[dict]:
        """Every dispute on this purchase order; the job was validated at schedule time."""
        return self.disputes.list_for_purchase_order(purchase_order_id)

    def _assemble_mandatory_context(
        self, purchase_order: dict, as_of_date: date, history: list[dict]
    ) -> DisputeSummaryContext:
        """Build the mandatory (non-tool-fetched) LLM context for one dispute's narrative.

        The `0.0` and `""` defaults on `computed_amount`, `delta_amount`, and
        `verdict` exist only for the type checker; `_require_analyzed_dispute`
        already guarantees a verdict by this point.
        """
        dispute = self._require_analyzed_dispute(self._current_dispute_id())
        retailer_name = next(
            (
                r["retailer_name"]
                for r in self.master_data.list_retailers()
                if r["id"] == purchase_order["retailer_id"]
            ),
            str(purchase_order["retailer_id"]),
        )
        sku_description = self._resolve_sku_description(purchase_order["id"])

        return DisputeSummaryContext(
            dispute_number=dispute["dispute_number"],
            reason_code=dispute["reason_code"],
            claimed_amount=dispute["claimed_amount"],
            computed_amount=dispute["computed_amount"] or 0.0,
            delta_amount=dispute["delta_amount"] or 0.0,
            verdict=dispute["verdict"] or "",
            dispute_status=dispute["dispute_status"],
            analyzed_at=as_of_date,
            order=DisputeOrderContext(
                order_id=str(purchase_order["id"]),
                retailer_name=retailer_name,
                sku_description=sku_description,
            ),
        )

    def _compute_content_fingerprint(self, context: DisputeSummaryContext) -> str:
        return _compute_content_fingerprint(context)

    def _build_tools(self, purchase_order: dict, as_of_date: date) -> list[BaseTool]:
        """Build this call's bounded tool set: rule detail, facts used, prior disputes.

        `prior_dispute_history` excludes the current dispute by id, so the agent
        cites only other disputes on this PO as history.
        """
        dispute_id = self._dispute_id_value
        assert dispute_id is not None  # set by every entry point before hooks run
        purchase_order_id = purchase_order["id"]
        return build_dispute_summary_tools(
            rule_detail=lambda: self._get_rule_detail(dispute_id),
            facts_used=lambda: self._get_facts_used(dispute_id),
            prior_dispute_history=lambda: [
                row
                for row in self.disputes.list_for_purchase_order(purchase_order_id)
                if row["id"] != dispute_id
            ],
        )

    def _output_with_reuse_cls(self) -> type[DisputeSummaryOutputWithReuse]:
        return DisputeSummaryOutputWithReuse

    def _generate(
        self,
        context: DisputeSummaryContext,
        *,
        order_id: str,
        as_of_date: date,
        tools: list[BaseTool],
        heartbeat: Callable[[], None] | None,
    ) -> PenaltySummaryOutputBase:
        """Delegate to `DisputeResolutionAgent.generate_dispute_summary` for the bounded tool-calling loop."""
        return self._agent.generate_dispute_summary(
            context, order_id=order_id, as_of_date=as_of_date, tools=tools, heartbeat=heartbeat
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_sku_description(self, purchase_order_id: UUID) -> str:
        """Best-effort human-readable label for the order's primary SKU line.

        Falls back through SKU description, SKU code, retailer material code,
        then the purchase-order id, so incomplete master data degrades the label
        instead of failing generation.
        """
        lines = self.purchase_orders.list_lines(purchase_order_id)
        if not lines:
            return str(purchase_order_id)
        primary_line = lines[0]
        if primary_line["sku_id"] is not None:
            sku = next((s for s in self.master_data.list_skus() if s["id"] == primary_line["sku_id"]), None)
            if sku is not None:
                return sku["description"] or sku["sku_code"]
        if primary_line["retailer_material_code"]:
            return primary_line["retailer_material_code"]
        return str(purchase_order_id)

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _get_rule_detail(self, dispute_id: UUID) -> dict[str, Any]:
        """Tool implementation: the effective penalty rule `analyze()` priced the dispute against.

        Returns `{"found": False}` for a missing `rule_id` or an unresolvable
        rule, degrading gracefully rather than raising into the agent loop.
        TIERED rules carry their tier bands so the agent can name the band that
        applied.
        """
        dispute = self.disputes.get_by_id(dispute_id)
        if dispute is None or dispute["rule_id"] is None:
            return {"found": False}
        rule = next((r for r in self.rules.list_rules() if r["id"] == dispute["rule_id"]), None)
        if rule is None:
            return {"found": False}
        tiers = []
        if rule["calc_type"] == "TIERED":
            tiers = [
                {"band_min": t.band_min, "band_max": t.band_max, "rate": t.rate}
                for t in self.rules.get_tiers_for_rule(rule["id"])
            ]
        return {**rule, "tiers": tiers, "found": True}

    def _get_facts_used(self, dispute_id: UUID) -> dict[str, Any]:
        """Tool implementation: `analyze()`'s persisted `analysis_breakdown`, surfaced verbatim.

        Lets the agent cite the exact inputs behind the verdict rather than
        re-deriving or guessing at them.
        """
        dispute = self.disputes.get_by_id(dispute_id)
        if dispute is None or not dispute["analysis_breakdown"]:
            return {"found": False}
        return {**dispute["analysis_breakdown"], "found": True}


def _fmt_number(value: float) -> str:
    """Fixed 6-decimal string, so floats equal by value hash alike whatever path produced them."""
    return f"{float(value):.6f}"


def _compute_content_fingerprint(context: DisputeSummaryContext) -> str:
    """Hash only the facts that determine the generated narrative.

    A dispute's verdict is immutable once RESOLVED or OVERRIDDEN, so this only
    matters across a re-analyze-then-regenerate cycle while still ANALYZED.
    """
    payload = {
        "verdict": context.verdict,
        "claimed_amount": _fmt_number(context.claimed_amount),
        "computed_amount": _fmt_number(context.computed_amount),
        "delta_amount": _fmt_number(context.delta_amount),
        "reason_code": context.reason_code,
        "dispute_status": context.dispute_status,
    }
    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
