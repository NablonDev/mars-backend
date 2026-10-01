"""Tests for TimelineSummaryService: context building, LLM generation, parsing, and caching."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

from app.agents.penalties.projection.schema import parse_reasoning_steps_from_markdown
from app.services.penalties.timeline.summary_service import TimelineSummaryService


class FakeAIMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeLLMClient:
    model_name = "test-gpt-5.6"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.call_count = 0

    def invoke(self, messages: list) -> FakeAIMessage:
        self.call_count += 1
        return FakeAIMessage(self.response_text)


SAMPLE_LLM_OUTPUT = """
## 1. Fine Projection
I project a late delivery breach for Target PO TL-S14-PO. Milestone PICKED slipped by 18 days.

## 2. Penalty Rule Extraction
Target OTIF rule applies at 3.0% of purchase order value.

## 3. Mitigation Options Modelled
Option 1: Accept penalty ($0 cost, $132 penalty). Option 2: Prioritize pick ($300 cost, $0 penalty).

## 4. Action Prepared
I recommend accepting the penalty as operational fixes cost more than the penalty.
"""


def test_parse_reasoning_steps_from_markdown() -> None:
    steps = parse_reasoning_steps_from_markdown(SAMPLE_LLM_OUTPUT)
    assert len(steps) == 4
    assert steps[0].step_number == 1
    assert "Fine Projection" in steps[0].title
    assert "TL-S14-PO" in steps[0].content
    assert steps[1].step_number == 2
    assert "Penalty Rule Extraction" in steps[1].title
    assert steps[2].step_number == 3
    assert "Mitigation Options Modelled" in steps[2].title
    assert steps[3].step_number == 4
    assert "Action Prepared" in steps[3].title


def test_timeline_summary_service_build_context_and_generate() -> None:
    plan_id = uuid4()
    po_id = uuid4()
    retailer_id = uuid4()
    agent_id = uuid4()

    session = MagicMock()
    timeline = MagicMock()
    risks = MagicMock()
    alerts = MagicMock()
    purchase_orders = MagicMock()
    master_data = MagicMock()
    summaries = MagicMock()
    agent_registry = MagicMock()
    agent_registry.ensure_registered.return_value = agent_id

    timeline.get_plan.return_value = {
        "id": plan_id,
        "plan_number": "TL-S14-PLAN",
        "purchase_order_id": po_id,
        "carrier_id": None,
        "ship_from_warehouse_id": None,
        "status": "OPEN",
        "freight_term": "PREPAID",
    }
    purchase_orders.get_purchase_order.return_value = {
        "id": po_id,
        "purchase_order_number": "TL-S14-PO",
        "retailer_id": retailer_id,
        "retailer_po_number": "TGT-1234",
        "window_start": date(2026, 9, 20),
        "window_end": date(2026, 10, 20),
        "cancel_date": date(2026, 10, 23),
    }
    master_data.get_retailer.return_value = {"retailer_name": "Target"}
    timeline.list_plan_lines.return_value = []
    timeline.list_milestones.return_value = [
        {
            "code": "DELIVERED",
            "baseline_date": date(2026, 10, 5),
            "planned_date": date(2026, 10, 5),
            "actual_date": None,
        }
    ]
    timeline.list_events.return_value = []
    risks.list_latest_for_plan.return_value = [
        {
            "projection_date": date(2026, 9, 25),
            "risk_type": "LATE",
            "status": "PROJECTED_BREACH",
            "days_off": 3,
            "shortfall_quantity": None,
            "projected_penalty_amount": 132.0,
            "currency_code": "USD",
            "driver_milestone_code": "PICKED",
            "driver_reason_code": "WAVE_NOT_RELEASED",
            "calculation_detail": None,
            "projected_milestones": [],
        }
    ]
    risks.list_options_for_plan.return_value = []
    alerts.list_for_plan.return_value = []

    # Mock session scalar return None for existing cached summary
    session.scalars.return_value.first.return_value = None

    llm = FakeLLMClient(SAMPLE_LLM_OUTPUT)

    service = TimelineSummaryService(
        session=session,
        timeline=timeline,
        risks=risks,
        alerts=alerts,
        purchase_orders=purchase_orders,
        master_data=master_data,
        summaries=summaries,
        agent_registry=agent_registry,
        llm=llm,  # type: ignore[arg-type]
    )

    ctx = service.build_context(plan_id)
    assert ctx.purchase_order_number == "TL-S14-PO"
    assert ctx.retailer_name == "Target"
    assert ctx.safety_buffer_days == 15  # 2026-10-20 minus 2026-10-05

    output = service.generate_and_persist(plan_id, force_regenerate=True)
    assert output.order_id == str(po_id)
    assert len(output.reasoning_steps) == 4
    assert llm.call_count == 1
    session.commit.assert_called_once()
