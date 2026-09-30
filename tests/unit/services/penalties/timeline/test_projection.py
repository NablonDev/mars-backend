"""Tests for the pure fulfillment-timeline projection engine.

`definitions()` (in `_definitions.py`) copies the 12 milestone rows from
`scripts/seed/seed_milestone_types.py::MILESTONE_TYPE_SEEDS` rather than
importing that script, keeping this test suite free of any SQLAlchemy
dependency.
"""

from datetime import date, timedelta

import pytest

from app.services.penalties.timeline.projection import (
    applicable_definitions,
    project_timeline,
)
from app.services.penalties.timeline.types import MilestoneState, PlanTimelineInput
from tests.unit.services.penalties.timeline._definitions import definitions as _definitions

DAY0 = date(2026, 1, 1)


def _state(
    code: str,
    *,
    baseline: date | None = None,
    planned: date | None = None,
    actual: date | None = None,
) -> MilestoneState:
    return MilestoneState(code=code, baseline_date=baseline, planned_date=planned, actual_date=actual)


def _plan(
    *,
    freight_term: str = "PREPAID",
    planned_transit_days: int | None = 2,
    window_start: date = DAY0,
    window_end: date = DAY0 + timedelta(days=30),
    cancel_date: date | None = None,
    milestones: tuple[MilestoneState, ...] = (),
    as_of: date = DAY0,
    not_before: dict[str, date] | None = None,
    duration_overrides: dict[str, float] | None = None,
    transit_override_days: int | None = None,
) -> PlanTimelineInput:
    return PlanTimelineInput(
        plan_id="PLAN-1",
        freight_term=freight_term,
        planned_transit_days=planned_transit_days,
        window_start=window_start,
        window_end=window_end,
        cancel_date=cancel_date,
        milestones=milestones,
        as_of=as_of,
        not_before=not_before or {},
        duration_overrides=duration_overrides or {},
        transit_override_days=transit_override_days,
    )


def _milestone(result, code):
    return next(m for m in result.milestones if m.code == code)


def test_applicable_definitions_filters_and_sorts_by_sequence() -> None:
    codes = [d.code for d in applicable_definitions(_definitions(), "COLLECT")]

    assert codes == [
        "ORDER_RECEIVED",
        "ORDER_CONFIRMED",
        "MATERIAL_AVAILABLE",
        "DELIVERY_CREATED",
        "PICKED",
        "READY_FOR_PICKUP",
        "LOADED",
        "GOODS_ISSUED",
        "ASN_SENT",
    ]


def test_on_track_prepaid_plan_has_no_risks_and_is_not_slipping() -> None:
    plan = _plan(window_end=DAY0 + timedelta(days=15))

    result = project_timeline(_definitions(), plan)

    assert result.measured_milestone_code == "DELIVERED"
    assert result.projected_measured_date == DAY0 + timedelta(days=5)
    assert result.slack_days == 10
    assert result.timing_risks == ()
    assert result.slipping is False
    assert result.slip_driver_code is None


def test_picked_slip_beyond_slack_produces_late_risk_driven_by_picked() -> None:
    baseline = {
        "ORDER_RECEIVED": 0,
        "ORDER_CONFIRMED": 1,
        "MATERIAL_AVAILABLE": 1,
        "TENDER_ACCEPTED": 2,
        "DELIVERY_CREATED": 1,
        "PICKED": 2,
        "LOADED": 2,
        "APPOINTMENT_CONFIRMED": 3,
        "GOODS_ISSUED": 2,
        "ASN_SENT": 2,
        "DELIVERED": 5,
    }
    milestones = tuple(
        _state(
            code,
            baseline=DAY0 + timedelta(days=offset),
            planned=DAY0 + timedelta(days=(offset + 3 if code == "PICKED" else offset)),
        )
        for code, offset in baseline.items()
    )
    plan = _plan(window_end=DAY0 + timedelta(days=6), milestones=milestones)

    result = project_timeline(_definitions(), plan)

    assert result.projected_measured_date == DAY0 + timedelta(days=7)
    assert len(result.timing_risks) == 1
    risk = result.timing_risks[0]
    assert risk.risk_type == "LATE"
    assert risk.status == "PROJECTED_BREACH"
    assert risk.days_off == 1
    assert risk.driver_milestone_code == "PICKED"
    assert _milestone(result, "PICKED").slip_days == 3


def test_slip_absorbed_by_slack_is_slipping_without_timing_risk() -> None:
    baseline = {
        "ORDER_RECEIVED": 0,
        "ORDER_CONFIRMED": 1,
        "MATERIAL_AVAILABLE": 1,
        "TENDER_ACCEPTED": 2,
        "DELIVERY_CREATED": 1,
        "PICKED": 2,
        "LOADED": 2,
        "APPOINTMENT_CONFIRMED": 3,
        "GOODS_ISSUED": 2,
        "ASN_SENT": 2,
        "DELIVERED": 5,
    }
    milestones = tuple(
        _state(
            code,
            baseline=DAY0 + timedelta(days=offset),
            planned=DAY0 + timedelta(days=(offset + 1 if code == "PICKED" else offset)),
        )
        for code, offset in baseline.items()
    )
    plan = _plan(window_end=DAY0 + timedelta(days=30), milestones=milestones)

    result = project_timeline(_definitions(), plan)

    assert result.projected_measured_date == DAY0 + timedelta(days=5)
    assert result.timing_risks == ()
    assert result.slipping is True
    assert result.slip_driver_code == "PICKED"
    assert _milestone(result, "PICKED").slip_days == 1
    assert _milestone(result, "DELIVERED").slip_days == 0


def test_pulled_forward_goods_issued_produces_early_risk() -> None:
    milestones = (
        _state("GOODS_ISSUED", baseline=DAY0 + timedelta(days=9), planned=DAY0 + timedelta(days=1)),
    )
    plan = _plan(
        window_start=DAY0 + timedelta(days=11),
        window_end=DAY0 + timedelta(days=30),
        milestones=milestones,
    )

    result = project_timeline(_definitions(), plan)

    assert result.projected_measured_date == DAY0 + timedelta(days=5)
    assert len(result.timing_risks) == 1
    risk = result.timing_risks[0]
    assert risk.risk_type == "EARLY"
    assert risk.status == "PROJECTED_BREACH"
    assert risk.days_off == -6
    assert risk.driver_milestone_code == "GOODS_ISSUED"
    assert _milestone(result, "GOODS_ISSUED").slip_days == -7


def test_collect_plan_is_measured_at_ready_for_pickup_and_excludes_prepaid_only_milestones() -> None:
    plan = _plan(freight_term="COLLECT", planned_transit_days=None, window_end=DAY0 + timedelta(days=30))

    result = project_timeline(_definitions(), plan)

    assert result.measured_milestone_code == "READY_FOR_PICKUP"
    codes = {m.code for m in result.milestones}
    assert "TENDER_ACCEPTED" not in codes
    assert "APPOINTMENT_CONFIRMED" not in codes
    assert "DELIVERED" not in codes


def test_cancel_date_exceeded_reports_not_delivered_only() -> None:
    plan = _plan(
        window_end=DAY0 + timedelta(days=3),
        cancel_date=DAY0 + timedelta(days=2),
    )

    result = project_timeline(_definitions(), plan)

    assert result.projected_measured_date == DAY0 + timedelta(days=5)
    assert len(result.timing_risks) == 1
    risk = result.timing_risks[0]
    assert risk.risk_type == "NOT_DELIVERED"
    assert risk.status == "PROJECTED_BREACH"
    assert risk.days_off == 3


def test_delivered_late_with_actual_date_is_breached() -> None:
    milestones = (_state("DELIVERED", actual=DAY0 + timedelta(days=20)),)
    plan = _plan(window_end=DAY0 + timedelta(days=10), milestones=milestones)

    result = project_timeline(_definitions(), plan)

    assert result.projected_measured_date == DAY0 + timedelta(days=20)
    assert len(result.timing_risks) == 1
    risk = result.timing_risks[0]
    assert risk.risk_type == "LATE"
    assert risk.status == "BREACHED"
    assert risk.days_off == 10


def test_overdue_pending_milestone_projects_to_as_of() -> None:
    milestones = (_state("ORDER_RECEIVED", planned=DAY0 - timedelta(days=5)),)
    plan = _plan(as_of=DAY0, milestones=milestones)

    result = project_timeline(_definitions(), plan)

    order_received = _milestone(result, "ORDER_RECEIVED")
    assert order_received.projected_date == DAY0
    assert order_received.slip_days == 5


def test_not_before_on_material_available_cascades_to_delivered() -> None:
    plan = _plan(not_before={"MATERIAL_AVAILABLE": DAY0 + timedelta(days=10)})

    result = project_timeline(_definitions(), plan)

    assert _milestone(result, "MATERIAL_AVAILABLE").projected_date == DAY0 + timedelta(days=10)
    assert result.projected_measured_date == DAY0 + timedelta(days=13)


def test_transit_override_and_duration_overrides_change_delivered() -> None:
    plan = _plan(transit_override_days=5, duration_overrides={"PICKED": 3})

    result = project_timeline(_definitions(), plan)

    # PICKED duration override (3 instead of 1) pushes GOODS_ISSUED to day 4; the
    # 5-day transit override (instead of planned_transit_days=2) then makes the
    # GOODS_ISSUED -> DELIVERED path (4 + 5 = 9) dominate over the
    # APPOINTMENT_CONFIRMED -> DELIVERED path (3 + 5 = 8).
    assert result.projected_measured_date == DAY0 + timedelta(days=9)


def test_asn_pending_after_goods_issued_done_is_asn_late() -> None:
    milestones = (
        _state("GOODS_ISSUED", actual=DAY0 + timedelta(days=2)),
        _state("ASN_SENT", planned=DAY0 + timedelta(days=7)),
    )
    plan = _plan(milestones=milestones)

    result = project_timeline(_definitions(), plan)

    asn_risks = [r for r in result.timing_risks if r.risk_type == "ASN_LATE"]
    assert len(asn_risks) == 1
    risk = asn_risks[0]
    assert risk.status == "PROJECTED_BREACH"
    assert risk.days_off == 5
    assert risk.driver_milestone_code == "ASN_SENT"


def test_missing_measurement_point_raises_value_error() -> None:
    plan = _plan(freight_term="THIRD_PARTY")

    with pytest.raises(ValueError):
        project_timeline(_definitions(), plan)
