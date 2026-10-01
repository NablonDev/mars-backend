"""Tests for `RiskRowAssembler`'s `calculation_detail.supply` gating.

`build_risk_rows` is exercised directly against a hand-built `PlanAssessment`
(via the real `assess_plan`) and a synthetic `PlanSupplyOutcome`, rather than
through the full service/supply-engine stack: the gating decision under test
-- which risk rows get a `supply` block -- depends only on the row's own
`risk_type`/`driver_milestone_code` and the outcome passed in, not on how
that outcome was computed.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.services.penalties.projection import CalcType, PenaltyRule
from app.services.penalties.timeline.assessment import assess_plan
from app.services.penalties.timeline.pricing import PricingBasis
from app.services.penalties.timeline.risk_rows import RiskRowAssembler
from app.services.penalties.timeline.supply import PlanSupplyOutcome
from app.services.penalties.timeline.types import MilestoneState, PlanTimelineInput
from scripts.seed.seed_milestone_types import seed_milestone_types

from ._definitions import definitions

BASIS = PricingBasis(quantity=100.0, unit_cost=10.0, unit_price=20.0)

# A pool-contention-style outcome: no shortfall, but the pool's supply position was
# fully covered only after the plan's own material-available need date.
POOL_CONTENTION_OUTCOME = PlanSupplyOutcome(
    plan_id="PLAN-1",
    shortfall_quantity=0.0,
    material_available_not_before=date(2026, 1, 9),
    cause_code="DEMAND_EXCEEDS_SUPPLY",
    cause_source_id=None,
    full_cover_date=date(2026, 1, 9),
    required_quantity=100.0,
    on_time_quantity=80.0,
    on_hand=50.0,
)


def _assembler() -> RiskRowAssembler:
    return RiskRowAssembler(timeline=FulfillmentTimelineRepository(_session()))


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.db.session import apply_sqlite_schema_translation

    engine = apply_sqlite_schema_translation(create_engine("sqlite://"))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_milestone_types(session)
    return session


def test_picking_driven_late_row_gets_no_supply_block_despite_pool_contention():
    """A LATE row driven by PICKED never gets `supply`, even when the pool's own
    outcome shows a `material_available_not_before` from unrelated demand contention:
    that delay isn't this row's story to tell."""
    plan = PlanTimelineInput(
        plan_id="PLAN-1",
        freight_term="PREPAID",
        planned_transit_days=2,
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
        cancel_date=None,
        milestones=(
            MilestoneState(
                code="PICKED",
                baseline_date=date(2026, 1, 3),
                planned_date=date(2026, 1, 10),
                actual_date=None,
            ),
        ),
        as_of=date(2026, 1, 3),
    )
    rule = PenaltyRule(rule_id="R1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=2.0)
    assessment = assess_plan(definitions(), plan, 0.0, None, BASIS, [rule], "SUM")

    rows = _assembler().build_risk_rows(
        uuid4(),
        uuid4(),
        plan,
        assessment,
        0.0,
        None,
        None,
        {},
        BASIS,
        "SUM",
        {"R1": "RULE-OTIF"},
        POOL_CONTENTION_OUTCOME,
    )

    assert len(rows) == 1
    assert rows[0]["risk_type"] == "LATE"
    assert rows[0]["driver_milestone_code"] == "PICKED"
    assert rows[0]["calculation_detail"]["supply"] is None


def test_material_available_driven_late_row_gets_a_supply_block():
    """A LATE row actually driven by MATERIAL_AVAILABLE does get `supply`."""
    plan = PlanTimelineInput(
        plan_id="PLAN-1",
        freight_term="PREPAID",
        planned_transit_days=2,
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 10),
        cancel_date=None,
        milestones=(
            MilestoneState(
                code="MATERIAL_AVAILABLE",
                baseline_date=date(2026, 1, 5),
                planned_date=date(2026, 1, 5),
                actual_date=None,
            ),
        ),
        as_of=date(2026, 1, 3),
        not_before={"MATERIAL_AVAILABLE": date(2026, 1, 9)},
    )
    rule = PenaltyRule(rule_id="R1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=2.0)
    assessment = assess_plan(definitions(), plan, 0.0, None, BASIS, [rule], "SUM")

    rows = _assembler().build_risk_rows(
        uuid4(),
        uuid4(),
        plan,
        assessment,
        0.0,
        None,
        None,
        {},
        BASIS,
        "SUM",
        {"R1": "RULE-OTIF"},
        POOL_CONTENTION_OUTCOME,
    )

    late_row = next(r for r in rows if r["risk_type"] == "LATE")
    assert late_row["driver_milestone_code"] == "MATERIAL_AVAILABLE"
    assert late_row["calculation_detail"]["supply"] is not None


def test_short_row_gets_a_supply_block():
    """A SHORT row always gets `supply`, regardless of its (absent) timing driver."""
    plan = PlanTimelineInput(
        plan_id="PLAN-1",
        freight_term="PREPAID",
        planned_transit_days=2,
        window_start=date(2026, 1, 1),
        window_end=date(2026, 2, 1),
        cancel_date=None,
        milestones=(),
        as_of=date(2026, 1, 3),
    )
    rule = PenaltyRule(rule_id="R2", violation_type="SHORT_SHIP", calc_type=CalcType.PER_UNIT, rate=3.0)
    assessment = assess_plan(definitions(), plan, 60.0, "PROJECTED_BREACH", BASIS, [rule], "SUM")

    rows = _assembler().build_risk_rows(
        uuid4(),
        uuid4(),
        plan,
        assessment,
        60.0,
        None,
        None,
        {},
        BASIS,
        "SUM",
        {"R2": "RULE-SHORT"},
        POOL_CONTENTION_OUTCOME,
    )

    short_row = next(r for r in rows if r["risk_type"] == "SHORT")
    assert short_row["calculation_detail"]["supply"] is not None


def _late_plan() -> PlanTimelineInput:
    return PlanTimelineInput(
        plan_id="PLAN-NR",
        freight_term="PREPAID",
        planned_transit_days=2,
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 5),
        cancel_date=None,
        milestones=(),
        as_of=date(2026, 1, 10),
    )


def _row_for(plan: PlanTimelineInput, rules: list[PenaltyRule]) -> dict:
    assessment = assess_plan(definitions(), plan, 0.0, None, BASIS, rules, "SUM")
    rows = _assembler().build_risk_rows(
        uuid4(), uuid4(), plan, assessment, 0.0, None, None, {}, BASIS, "SUM", {}, POOL_CONTENTION_OUTCOME
    )
    return next(r for r in rows if r["risk_type"] == "LATE")


def test_late_row_with_no_live_rule_records_that_no_rule_applied():
    row = _row_for(_late_plan(), rules=[])

    assert row["status"] == "SLIPPING"
    assert row["projected_penalty_amount"] == 0.0
    assert row["calculation_detail"]["pricing"]["zero_reason"] == "NO_APPLICABLE_RULE"
    assert row["calculation_detail"]["pricing"]["rules"] == []


def test_late_row_that_is_charged_has_no_zero_reason():
    rule = PenaltyRule(rule_id="R1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=2.0)

    row = _row_for(_late_plan(), rules=[rule])

    assert row["projected_penalty_amount"] > 0
    assert row["calculation_detail"]["pricing"]["zero_reason"] is None
