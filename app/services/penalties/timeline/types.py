"""Data structures for the fulfillment-timeline projection engine.

`MilestoneDefinition` mirrors the reference rows seeded by
`scripts/seed/seed_milestone_types.py` (the `milestone_type` table): code,
sequence, dependency graph, default duration, freight-term scope, and
whether the milestone is the freight term's measurement point.
`MilestoneState` and `PlanTimelineInput` describe one fulfillment plan's
dated milestones and delivery-window constraints; `ProjectedMilestone`,
`TimingRisk`, and `PlanTimelineResult` describe `projection.project_timeline`'s
output.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

MEASURE_PREPAID = "DELIVERED"
MEASURE_COLLECT = "READY_FOR_PICKUP"


@dataclass(frozen=True)
class MilestoneDefinition:
    code: str
    sequence_no: int
    depends_on: tuple[str, ...]
    default_duration_days: float | None
    freight_term_scope: str
    is_measurement_point: bool
    owner_team: str | None = None


@dataclass(frozen=True)
class MilestoneState:
    code: str
    baseline_date: date | None
    planned_date: date | None
    actual_date: date | None


@dataclass(frozen=True)
class PlanTimelineInput:
    plan_id: str
    freight_term: str
    planned_transit_days: int | None
    window_start: date
    window_end: date
    cancel_date: date | None
    milestones: tuple[MilestoneState, ...]
    as_of: date
    not_before: Mapping[str, date] = field(default_factory=dict)
    duration_overrides: Mapping[str, float] = field(default_factory=dict)
    transit_override_days: int | None = None


@dataclass(frozen=True)
class ProjectedMilestone:
    code: str
    baseline_date: date | None
    planned_date: date | None
    projected_date: date
    actual_date: date | None
    slip_days: int


@dataclass(frozen=True)
class TimingRisk:
    risk_type: str
    status: str
    days_off: int
    driver_milestone_code: str | None


@dataclass(frozen=True)
class PlanTimelineResult:
    plan_id: str
    measured_milestone_code: str
    projected_measured_date: date
    slack_days: int
    milestones: tuple[ProjectedMilestone, ...]
    timing_risks: tuple[TimingRisk, ...]
    slipping: bool
    slip_driver_code: str | None
