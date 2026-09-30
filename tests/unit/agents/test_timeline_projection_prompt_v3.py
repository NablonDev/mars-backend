"""Unit tests for the event-driven fulfillment timeline penalty projection prompt (v3),
context models, and markdown reasoning steps parser.
"""

from datetime import date

from app.agents.penalties.projection import (
    V3_PROMPT_VERSION,
    V3_SYSTEM_PROMPT,
    TimelineAlertContext,
    TimelineEventContext,
    TimelineLineContext,
    TimelineMilestoneContext,
    TimelineOptionContext,
    TimelineProjectionSummaryContext,
    TimelineProjectionSummaryOutput,
    TimelineRiskContext,
    parse_reasoning_steps_from_markdown,
)


def test_v3_prompt_metadata_and_sections():
    """Verify v3 prompt version and presence of the 4-step framework."""
    assert V3_PROMPT_VERSION == "v3"
    assert "Data Trust & Grounding Invariants" in V3_SYSTEM_PROMPT
    assert "NEVER invent numbers, dates" in V3_SYSTEM_PROMPT
    assert "Step 1 — Fine Projection" in V3_SYSTEM_PROMPT
    assert "Step 2 — Penalty Rule Extraction" in V3_SYSTEM_PROMPT
    assert "Step 3 — Mitigation Options Modelled" in V3_SYSTEM_PROMPT
    assert "Step 4 — Action Prepared" in V3_SYSTEM_PROMPT


def test_timeline_projection_context_creation():
    """Verify TimelineProjectionSummaryContext instantiates with full event-driven payload."""
    ctx = TimelineProjectionSummaryContext(
        plan_id="plan-123",
        plan_number="FP-001",
        purchase_order_id="po-456",
        purchase_order_number="TL-S10B-PO",
        retailer_name="Walmart",
        order_status="OPEN",
        freight_term="PREPAID",
        window_start=date(2026, 9, 20),
        window_end=date(2026, 10, 20),
        safety_buffer_days=15,
        lines=[
            TimelineLineContext(
                material_code="PED-101",
                material_description="Pedigree Adult 20lb",
                planned_quantity=1500,
                confirmed_quantity=1440,
                shortfall_quantity=60,
                unit_price=22.50,
            )
        ],
        milestones=[
            TimelineMilestoneContext(
                code="PICKED",
                name="Picked",
                sequence_no=60,
                baseline_date=date(2026, 10, 2),
                planned_date=date(2026, 10, 20),
                projected_date=date(2026, 10, 20),
                slip_days=18,
            )
        ],
        events=[
            TimelineEventContext(
                event_at="2026-09-23T10:00:00Z",
                event_type="DATE_REPLANNED",
                milestone_code="PICKED",
                reason_code="EQUIPMENT_BREAKDOWN",
                old_value="2026-10-02",
                new_value="2026-10-20",
                source="SAP ERP",
            )
        ],
        latest_risks=[
            TimelineRiskContext(
                risk_type="LATE",
                status="PROJECTED_BREACH",
                days_off=3,
                projected_penalty_amount=450.00,
                driver_milestone_code="PICKED",
                driver_reason_code="EQUIPMENT_BREAKDOWN",
            )
        ],
        options=[
            TimelineOptionContext(
                action_code="EXPEDITE_TRANSIT",
                feasible=True,
                action_cost=200.00,
                penalty_before=450.00,
                penalty_after=0.00,
                net_saving=250.00,
                act_by_date=date(2026, 9, 25),
                rank_no=1,
            )
        ],
        active_alert=TimelineAlertContext(
            alert_id="alt-001",
            risk_type="LATE",
            status="NEW",
            first_seen_date=date(2026, 9, 23),
            last_seen_date=date(2026, 9, 23),
        ),
    )

    dumped = ctx.model_dump(mode="json")
    assert dumped["purchase_order_number"] == "TL-S10B-PO"
    assert dumped["safety_buffer_days"] == 15
    assert len(dumped["lines"]) == 1
    assert dumped["lines"][0]["shortfall_quantity"] == 60
    assert dumped["milestones"][0]["slip_days"] == 18
    assert dumped["latest_risks"][0]["projected_penalty_amount"] == 450.00
    assert dumped["options"][0]["net_saving"] == 250.00


def test_parse_reasoning_steps_from_markdown():
    """Verify parsing of 4-step markdown into structured ReasoningStepOutput items."""
    sample_markdown = """
### Step 1 — Fine Projection
I analysed the delivery trajectory for TL-S10B-PO (Walmart, 1,500 cases from Fort Smith DC). The Picked milestone slipped from Oct 2 to Oct 20 (+18d slip) due to Equipment Breakdown. Baseline buffer was 15 days, resulting in 3 days late. Projected fine: $450.00.

### Step 2 — Penalty Rule Extraction
Walmart's OTIF Late delivery charge is 3.0% of PO value for deliveries arriving past the Oct 20 delivery window close.

### Step 3 — Mitigation Options Modelled
I modelled candidate mitigation options. Expedite transit costs $200.00, eliminating the fine and delivering a net saving of +$250.00.

### Step 4 — Action Prepared
Carrier Swift Transportation pre-identified with expedited transit rate of $200.00. Slashed transit allows delivery inside window. Projected fine: $0. Net saving: +$250.00.
"""

    steps = parse_reasoning_steps_from_markdown(sample_markdown)
    assert len(steps) == 4
    assert steps[0].step_number == 1
    assert steps[0].title == "Fine Projection"
    assert "TL-S10B-PO" in steps[0].content
    assert steps[1].step_number == 2
    assert steps[1].title == "Penalty Rule Extraction"
    assert steps[2].step_number == 3
    assert steps[2].title == "Mitigation Options Modelled"
    assert steps[3].step_number == 4
    assert steps[3].title == "Action Prepared"
    assert "+$250.00" in steps[3].content

    # Output schema verification
    output = TimelineProjectionSummaryOutput(
        order_id="po-456",
        as_of_date=date(2026, 9, 23),
        prompt_version="v3",
        model_name="gpt-4o",
        summary=sample_markdown.strip(),
        reasoning_steps=steps,
    )
    assert output.prompt_version == "v3"
    assert len(output.reasoning_steps) == 4
