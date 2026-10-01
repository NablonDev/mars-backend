"""Tests for the pure SAP-style backward scheduler."""

from datetime import date, timedelta

from app.services.penalties.timeline.projection import applicable_definitions, project_timeline
from app.services.penalties.timeline.schedule import backward_schedule, target_measured_date
from app.services.penalties.timeline.types import MilestoneState, PlanTimelineInput
from tests.unit.services.penalties.timeline._definitions import definitions as _definitions

DAY0 = date(2026, 1, 1)


def test_target_measured_date_is_window_midpoint() -> None:
    assert target_measured_date(DAY0, DAY0 + timedelta(days=10)) == DAY0 + timedelta(days=5)


def test_target_measured_date_single_day_window_targets_that_day() -> None:
    assert target_measured_date(DAY0, DAY0) == DAY0


def test_prepaid_delivered_at_midpoint_and_goods_issued_is_delivered_minus_transit() -> None:
    schedule = backward_schedule(
        _definitions(),
        "PREPAID",
        order_date=DAY0,
        window_start=DAY0 + timedelta(days=10),
        window_end=DAY0 + timedelta(days=20),
        planned_transit_days=2,
    )

    assert schedule["DELIVERED"] == DAY0 + timedelta(days=15)
    assert schedule["GOODS_ISSUED"] == schedule["DELIVERED"] - timedelta(days=2)


def test_collect_ready_for_pickup_at_target_and_goods_issued_not_before_it() -> None:
    schedule = backward_schedule(
        _definitions(),
        "COLLECT",
        order_date=DAY0,
        window_start=DAY0 + timedelta(days=10),
        window_end=DAY0 + timedelta(days=20),
        planned_transit_days=2,
    )

    assert schedule["READY_FOR_PICKUP"] == DAY0 + timedelta(days=15)
    assert schedule["GOODS_ISSUED"] >= schedule["READY_FOR_PICKUP"]


def test_every_dependency_date_is_at_or_before_its_dependant() -> None:
    applicable = applicable_definitions(_definitions(), "PREPAID")
    schedule = backward_schedule(
        _definitions(),
        "PREPAID",
        order_date=DAY0,
        window_start=DAY0 + timedelta(days=10),
        window_end=DAY0 + timedelta(days=20),
        planned_transit_days=2,
    )

    for definition in applicable:
        for dep_code in definition.depends_on:
            if dep_code not in schedule:
                continue
            assert schedule[dep_code] <= schedule[definition.code]


def test_tight_order_is_pushed_forward_and_measured_date_exceeds_target() -> None:
    window_start = DAY0 + timedelta(days=2)
    schedule = backward_schedule(
        _definitions(),
        "PREPAID",
        order_date=DAY0,
        window_start=window_start,
        window_end=window_start,
        planned_transit_days=2,
    )

    target = target_measured_date(window_start, window_start)
    assert schedule["DELIVERED"] > target


def test_walmart_single_day_window_targets_that_day() -> None:
    window = DAY0 + timedelta(days=15)
    schedule = backward_schedule(
        _definitions(),
        "PREPAID",
        order_date=DAY0,
        window_start=window,
        window_end=window,
        planned_transit_days=2,
    )

    assert schedule["DELIVERED"] == window


def test_feasible_baseline_round_trips_through_project_timeline_with_no_risk() -> None:
    order_date = DAY0
    window_start = DAY0 + timedelta(days=10)
    window_end = DAY0 + timedelta(days=20)
    schedule = backward_schedule(
        _definitions(), "PREPAID", order_date, window_start, window_end, planned_transit_days=2
    )
    milestones = tuple(
        MilestoneState(code=code, baseline_date=when, planned_date=when, actual_date=None)
        for code, when in schedule.items()
    )
    plan = PlanTimelineInput(
        plan_id="PLAN-1",
        freight_term="PREPAID",
        planned_transit_days=2,
        window_start=window_start,
        window_end=window_end,
        cancel_date=None,
        milestones=milestones,
        as_of=order_date,
    )

    result = project_timeline(_definitions(), plan)

    assert result.projected_measured_date == target_measured_date(window_start, window_end)
    assert result.timing_risks == ()
    assert result.slipping is False
    assert result.slip_driver_code is None
