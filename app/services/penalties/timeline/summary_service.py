"""Service for generating, caching, and serving LLM-powered 4-step explainability for the fulfillment timeline."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.penalties.projection.context import (
    TimelineAlertContext,
    TimelineEventContext,
    TimelineLineContext,
    TimelineMilestoneContext,
    TimelineOptionContext,
    TimelineProjectionSummaryContext,
    TimelineRiskContext,
)
from app.agents.penalties.projection.prompts.v3 import SYSTEM_PROMPT
from app.agents.penalties.projection.schema import (
    TimelineProjectionSummaryOutput,
    parse_reasoning_steps_from_markdown,
)
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.exceptions import NotFoundError
from app.models import (
    Material,
    MilestoneType,
    PenaltySummary,
    PurchaseOrderLine,
    Warehouse,
)
from app.models.enums import SummaryStatus, SummaryType
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.utils.clock import utc_today
from app.utils.json_helpers import json_default, wrap_data

logger = logging.getLogger(__name__)

AGENT_CODE = "penalty_projection_summary"
PROMPT_V3 = "v3"


class TimelineSummaryService:
    """Orchestrates LLM-powered 4-step explainability generation and caching for the fulfillment timeline."""

    def __init__(
        self,
        *,
        session: Session,
        timeline: FulfillmentTimelineRepository,
        risks: FulfillmentRiskRepository,
        alerts: TimelineAlertRepository,
        purchase_orders: PurchaseOrderRepository,
        master_data: MasterDataRepository,
        summaries: PenaltySummaryRepository,
        agent_registry: AgentRegistryRepository,
        llm: AzureOpenAIChatClient,
    ) -> None:
        self._session = session
        self.timeline = timeline
        self.risks = risks
        self.alerts = alerts
        self.purchase_orders = purchase_orders
        self.master_data = master_data
        self.summaries = summaries
        self.agent_registry = agent_registry
        self._llm = llm

    def build_context(
        self, plan_id: UUID, selected_risk_type: str | None = None
    ) -> TimelineProjectionSummaryContext:
        """Assemble the complete, grounded context for one fulfillment plan."""
        plan = self.timeline.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(
                code="FULFILLMENT_PLAN_NOT_FOUND",
                message=f"No fulfillment plan found with plan_id={plan_id}",
            )

        po = self.purchase_orders.get_purchase_order(plan["purchase_order_id"])
        if po is None:
            raise NotFoundError(
                code="PURCHASE_ORDER_NOT_FOUND",
                message=f"No purchase order found with id={plan['purchase_order_id']}",
            )

        retailer = self.master_data.get_retailer(po["retailer_id"])
        retailer_name = retailer["retailer_name"] if retailer else "Customer"

        carrier = self.master_data.get_carrier(plan["carrier_id"]) if plan.get("carrier_id") else None
        carrier_name = carrier["carrier_name"] if carrier else None

        warehouse = (
            self._session.get(Warehouse, plan["ship_from_warehouse_id"])
            if plan.get("ship_from_warehouse_id")
            else None
        )
        warehouse_name = warehouse.warehouse_name if warehouse else None

        # Load plan lines
        lines_raw = self.timeline.list_plan_lines(plan_id)
        lines: list[TimelineLineContext] = []
        for l in lines_raw:
            po_line = self._session.get(PurchaseOrderLine, l["purchase_order_line_id"])
            material = (
                self._session.get(Material, po_line.material_id) if po_line and po_line.material_id else None
            )
            planned_qty = float(l.get("planned_quantity", 0))
            confirmed_qty = (
                float(l["confirmed_quantity"]) if l.get("confirmed_quantity") is not None else None
            )
            shortfall = max(0.0, planned_qty - (confirmed_qty if confirmed_qty is not None else planned_qty))
            lines.append(
                TimelineLineContext(
                    material_code=material.material_code if material else None,
                    material_description=material.description if material else None,
                    planned_quantity=planned_qty,
                    confirmed_quantity=confirmed_qty,
                    shortfall_quantity=shortfall,
                    unit_price=float(po_line.unit_price) if po_line and po_line.unit_price else None,
                )
            )

        # Load milestones and risks
        milestones_raw = self.timeline.list_milestones(plan_id)
        milestones: list[TimelineMilestoneContext] = []
        latest_risks = self.risks.list_latest_for_plan(plan_id)

        projected_by_code: dict[str, Any] = {}
        if latest_risks:
            for pm in latest_risks[0].get("projected_milestones") or []:
                projected_by_code[pm["code"]] = (pm.get("slip_days"), pm.get("projected_date"))

        for m in milestones_raw:
            m_type = self._session.scalars(
                select(MilestoneType).where(MilestoneType.code == m["code"])
            ).first()
            slip, proj_date = projected_by_code.get(m["code"], (0, m.get("planned_date")))
            milestones.append(
                TimelineMilestoneContext(
                    code=m["code"],
                    name=m_type.name if m_type else m["code"],
                    sequence_no=m_type.sequence_no if m_type else 0,
                    owner_team=m_type.owner_team if m_type else None,
                    is_measurement_point=m_type.is_measurement_point if m_type else False,
                    baseline_date=m.get("baseline_date"),
                    planned_date=m.get("planned_date"),
                    projected_date=proj_date,
                    actual_date=m.get("actual_date"),
                    slip_days=slip or 0,
                )
            )

        # Safety buffer days
        safety_buffer = 0
        delivery_ms = next((m for m in milestones if m.code == "DELIVERED"), None)
        if delivery_ms and delivery_ms.baseline_date and po.get("window_end"):
            safety_buffer = max(0, (po["window_end"] - delivery_ms.baseline_date).days)

        # Load events
        events_raw = self.timeline.list_events(plan_id)
        events: list[TimelineEventContext] = [
            TimelineEventContext(
                event_at=e["event_at"].isoformat()
                if hasattr(e["event_at"], "isoformat")
                else str(e["event_at"]),
                event_type=e["event_type"],
                milestone_code=e.get("milestone_code"),
                reason_code=e.get("reason_code"),
                old_value=e.get("old_value"),
                new_value=e.get("new_value"),
                source=e.get("source", "SAP ERP"),
            )
            for e in events_raw
        ]

        # Risks
        timeline_risks: list[TimelineRiskContext] = [
            TimelineRiskContext(
                projection_date=r.get("projection_date"),
                risk_type=r["risk_type"],
                status=r["status"],
                days_off=r.get("days_off"),
                shortfall_quantity=float(r["shortfall_quantity"])
                if r.get("shortfall_quantity") is not None
                else None,
                projected_penalty_amount=float(r["projected_penalty_amount"]),
                currency_code=r.get("currency_code", "USD"),
                driver_milestone_code=r.get("driver_milestone_code"),
                driver_reason_code=r.get("driver_reason_code"),
                calculation_detail=r.get("calculation_detail"),
            )
            for r in latest_risks
        ]

        # Options
        options_raw = (
            self.risks.list_options_for_plan(plan_id, latest_risks[0]["projection_date"])
            if latest_risks
            else []
        )
        timeline_options: list[TimelineOptionContext] = [
            TimelineOptionContext(
                action_code=o["action_code"],
                owner_team=o.get("owner_team"),
                feasible=bool(o["feasible"]) if o.get("feasible") is not None else True,
                infeasible_reason=o.get("infeasible_reason"),
                action_cost=float(o.get("action_cost") or 0.0),
                penalty_before=float(o.get("penalty_before") or 0.0),
                penalty_after=float(o.get("penalty_after") or 0.0),
                net_saving=float(o.get("net_saving") or 0.0),
                act_by_date=o.get("act_by_date"),
                confidence=o.get("confidence") or "HIGH",
                rank_no=o.get("rank_no") or 1,
                addresses_risk_types=o.get("addresses_risk_types") or [],
            )
            for o in options_raw
        ]

        # Active alert
        alerts_raw = self.alerts.list_for_plan(plan_id)
        active_alert = None
        if alerts_raw:
            first_alert = alerts_raw[0]
            active_alert = TimelineAlertContext(
                alert_id=str(first_alert["id"]),
                risk_type=first_alert["risk_type"],
                status=first_alert["status"],
                first_seen_date=first_alert["first_seen_date"],
                last_seen_date=first_alert["last_seen_date"],
            )

        return TimelineProjectionSummaryContext(
            plan_id=str(plan_id),
            plan_number=plan["plan_number"],
            purchase_order_id=str(po["id"]),
            purchase_order_number=po["purchase_order_number"],
            retailer_po_number=po.get("retailer_po_number"),
            retailer_name=retailer_name,
            order_status=plan.get("status", "OPEN"),
            freight_term=plan.get("freight_term", "PREPAID"),
            window_start=po.get("window_start"),
            window_end=po.get("window_end"),
            cancel_date=po.get("cancel_date"),
            carrier_name=carrier_name,
            warehouse_name=warehouse_name,
            safety_buffer_days=safety_buffer,
            lines=lines,
            milestones=milestones,
            events=events,
            latest_risks=timeline_risks,
            options=timeline_options,
            active_alert=active_alert,
            selected_risk_type=selected_risk_type,
        )

    def generate_and_persist(
        self,
        plan_id: UUID,
        force_regenerate: bool = False,
        selected_risk_type: str | None = None,
    ) -> TimelineProjectionSummaryOutput:
        """Generate the LLM 4-step reasoning for one fulfillment plan, caching it in `penalties.penalty_summary`."""
        context = self.build_context(plan_id, selected_risk_type=selected_risk_type)
        po_id = UUID(context.purchase_order_id)
        as_of = (
            context.latest_risks[0].projection_date
            if (context.latest_risks and context.latest_risks[0].projection_date)
            else utc_today()
        )

        agent_id = self.agent_registry.ensure_registered(
            agent_code=AGENT_CODE,
            prompt_version=PROMPT_V3,
            system_prompt=SYSTEM_PROMPT,
            agent_name="Penalty Projection Summary (Timeline)",
            domain="penalties",
            is_active=True,
        )

        context_json = json.dumps(context.model_dump(mode="json"), default=json_default, sort_keys=True)
        context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()

        # Check cache if not forcing regeneration
        if not force_regenerate:
            cached = self.summaries.get_cached(po_id, SummaryType.PROJECTION.value, as_of)
            if cached is not None and cached.get("summary"):
                steps = parse_reasoning_steps_from_markdown(cached["summary"])
                if len(steps) >= 4:
                    return TimelineProjectionSummaryOutput(
                        order_id=str(po_id),
                        as_of_date=as_of,
                        prompt_version=PROMPT_V3,
                        model_name=cached.get("model_name", "cached"),
                        summary=cached["summary"],
                        reasoning_steps=steps,
                    )

        # Invoke Azure OpenAI with grounded v3 prompt
        logger.info(
            "Calling Azure OpenAI to generate timeline reasoning for plan=%s PO=%s",
            plan_id,
            context.purchase_order_number,
        )
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=wrap_data(context.model_dump(mode="json"), default=json_default)),
        ]
        response = self._llm.invoke(messages)
        content = str(response.content)
        steps = parse_reasoning_steps_from_markdown(content)

        # Upsert into penalties.penalty_summary
        existing = self._session.scalars(
            select(PenaltySummary).where(
                PenaltySummary.purchase_order_id == po_id,
                PenaltySummary.summary_type == SummaryType.PROJECTION.value,
                PenaltySummary.as_of_date == as_of,
            )
        ).first()

        if existing is not None:
            existing.agent_id = agent_id
            existing.context_hash = context_hash
            existing.status = SummaryStatus.READY
            existing.model_name = self._llm.model_name
            existing.summary = content
            existing.error_message = None
        else:
            new_row = PenaltySummary(
                id=uuid4(),
                purchase_order_id=po_id,
                summary_type=SummaryType.PROJECTION.value,
                as_of_date=as_of,
                agent_id=agent_id,
                context_hash=context_hash,
                status=SummaryStatus.READY,
                model_name=self._llm.model_name,
                summary=content,
            )
            self._session.add(new_row)

        self._session.commit()

        return TimelineProjectionSummaryOutput(
            order_id=str(po_id),
            as_of_date=as_of,
            prompt_version=PROMPT_V3,
            model_name=self._llm.model_name,
            summary=content,
            reasoning_steps=steps,
        )
