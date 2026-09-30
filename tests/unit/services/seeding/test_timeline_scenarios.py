"""End-to-end replay tests for the fulfillment-timeline scenario simulator.

Runs the full 21-day replay once against SQLite and asserts every scenario's
projected outcome at T: this is both the simulator's own test and the
end-to-end test of the timeline penalty engine.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.services.seeding.timeline.scenarios import SCENARIOS, assert_unique_codes
from app.services.seeding.timeline.simulator import TimelineSimulator
from scripts.seed.seed_milestone_types import seed_milestone_types

T = date(2026, 6, 30)


@pytest.fixture
def simulator(db_session) -> TimelineSimulator:
    seed_milestone_types(db_session)
    return TimelineSimulator(db_session, T)


def _latest_risks(risks: FulfillmentRiskRepository, plan_id) -> list[dict]:
    """The plan's most recent projection date's risk rows, or [] if it never got one."""
    rows = risks.list_for_plan(plan_id)
    if not rows:
        return []
    latest_date = max(r["projection_date"] for r in rows)
    return [r for r in rows if r["projection_date"] == latest_date]


def _row(rows: list[dict], risk_type: str) -> dict:
    matches = [r for r in rows if r["risk_type"] == risk_type]
    assert len(matches) == 1, f"expected exactly one {risk_type} row, got {matches}"
    return matches[0]


def _options_by_code(risks: FulfillmentRiskRepository, plan_id, projection_date: date) -> dict[str, dict]:
    return {o["action_code"]: o for o in risks.list_options_for_plan(plan_id, projection_date)}


def test_scenario_codes_are_unique() -> None:
    assert_unique_codes()
    codes = [s.code for s in SCENARIOS]
    assert len(codes) == len(set(codes))


def test_replay_produces_every_outcome_table_row(simulator: TimelineSimulator) -> None:
    simulator.replay()
    risks = simulator.risks
    plans = simulator._plans

    assert _latest_risks(risks, plans["TL-S01"]["id"]) == []

    s02_rows = _latest_risks(risks, plans["TL-S02"]["id"])
    s02_late = _row(s02_rows, "LATE")
    assert s02_late["status"] == "PROJECTED_BREACH"
    s02_tracking = simulator.alerts.list_tracking_for_plan(plans["TL-S02"]["id"])
    assert len(s02_tracking) == 1
    assert s02_tracking[0]["status"] == "NEW"
    assert s02_late["driver_milestone_code"] == "PICKED"
    assert s02_late["driver_reason_code"] == "WAVE_NOT_RELEASED"
    s02_options = _options_by_code(risks, plans["TL-S02"]["id"], s02_late["projection_date"])
    top_ranked = next(o["action_code"] for o in s02_options.values() if o["rank_no"] == 1)
    assert top_ranked in ("PRIORITIZE_PICK", "EXPEDITE_FREIGHT")

    s03_rows = _latest_risks(risks, plans["TL-S03"]["id"])
    s03_early = _row(s03_rows, "EARLY")
    assert s03_early["status"] == "PROJECTED_BREACH"
    assert s03_early["driver_reason_code"] == "LOAD_PULLED_FORWARD"
    s03_options = _options_by_code(risks, plans["TL-S03"]["id"], s03_early["projection_date"])
    assert s03_options["HOLD_SHIPMENT"]["feasible"] is False
    assert s03_options["HOLD_SHIPMENT"]["infeasible_reason"] == "MILESTONE_ALREADY_DONE"
    assert s03_options["CARRIER_YARD_HOLD"]["feasible"] is True

    s04_rows = _latest_risks(risks, plans["TL-S04"]["id"])
    s04_short = _row(s04_rows, "SHORT")
    assert s04_short["status"] == "PROJECTED_BREACH"
    assert s04_short["driver_reason_code"] == "QA_HOLD"
    s04_options = _options_by_code(risks, plans["TL-S04"]["id"], s04_short["projection_date"])
    assert s04_options["EXPEDITE_QA_RELEASE"]["feasible"] is True

    s05_rows = _latest_risks(risks, plans["TL-S05"]["id"])
    s05_timing = next(r for r in s05_rows if r["risk_type"] in ("LATE", "NOT_DELIVERED"))
    assert s05_timing["status"] == "PROJECTED_BREACH"
    assert s05_timing["driver_milestone_code"] == "MATERIAL_AVAILABLE"
    assert s05_timing["driver_reason_code"] == "PRODUCTION_DELAY"

    s06_rows = _latest_risks(risks, plans["TL-S06"]["id"])
    s06_late = _row(s06_rows, "LATE")
    assert s06_late["status"] == "PROJECTED_BREACH"
    assert s06_late["driver_milestone_code"] == "TENDER_ACCEPTED"
    assert s06_late["driver_reason_code"] == "TENDER_REJECTED"
    s06_options = _options_by_code(risks, plans["TL-S06"]["id"], s06_late["projection_date"])
    assert s06_options["REBOOK_CARRIER"]["feasible"] is True

    s07_rows = _latest_risks(risks, plans["TL-S07"]["id"])
    s07_late = _row(s07_rows, "LATE")
    assert s07_late["status"] == "PROJECTED_BREACH"
    assert s07_late["measured_milestone_code"] == "READY_FOR_PICKUP"
    s07_options = _options_by_code(risks, plans["TL-S07"]["id"], s07_late["projection_date"])
    assert "EXPEDITE_FREIGHT" not in s07_options
    assert "REBOOK_CARRIER" not in s07_options

    s08_rows = _latest_risks(risks, plans["TL-S08"]["id"])
    s08_late = _row(s08_rows, "LATE")
    assert s08_late["status"] == "BREACHED"
    assert s08_late["projected_penalty_amount"] > 0.0
    s08_alerts = simulator.alerts.list_for_plan(plans["TL-S08"]["id"])
    s08_late_alert = _row(s08_alerts, "LATE")
    assert s08_late_alert["status"] == "PENALTY_INCURRED"

    s09_rows = _latest_risks(risks, plans["TL-S09"]["id"])
    s09_late = _row(s09_rows, "LATE")
    assert s09_late["status"] == "PROJECTED_BREACH"
    assert s09_late["driver_milestone_code"] == "APPOINTMENT_CONFIRMED"
    assert s09_late["driver_reason_code"] == "NO_APPOINTMENT_SLOT"
    s09_options = _options_by_code(risks, plans["TL-S09"]["id"], s09_late["projection_date"])
    assert s09_options["REBOOK_APPOINTMENT"]["feasible"] is True

    assert _latest_risks(risks, plans["TL-S10A"]["id"]) == []
    s10b_rows = _latest_risks(risks, plans["TL-S10B"]["id"])
    s10b_short = _row(s10b_rows, "SHORT")
    assert s10b_short["status"] == "PROJECTED_BREACH"
    assert s10b_short["driver_reason_code"] == "DEMAND_EXCEEDS_SUPPLY"

    s11_rows = _latest_risks(risks, plans["TL-S11"]["id"])
    s11_late = _row(s11_rows, "LATE")
    assert s11_late["status"] == "SLIPPING"
    assert s11_late["projected_penalty_amount"] == 0.0
    assert risks.list_options_for_plan(plans["TL-S11"]["id"], s11_late["projection_date"]) == []
    assert simulator.alerts.list_for_plan(plans["TL-S11"]["id"]) == []

    assert _latest_risks(risks, plans["TL-S12"]["id"]) == []

    s13_rows = _latest_risks(risks, plans["TL-S13"]["id"])
    s13_asn = _row(s13_rows, "ASN_LATE")
    assert s13_asn["status"] == "PROJECTED_BREACH"

    s14_rows = _latest_risks(risks, plans["TL-S14"]["id"])
    s14_late = _row(s14_rows, "LATE")
    assert s14_late["status"] == "PROJECTED_BREACH"
    assert s14_late["driver_milestone_code"] == "PICKED"
    assert s14_late["driver_reason_code"] == "WAVE_NOT_RELEASED"

    s15_rows = _latest_risks(risks, plans["TL-S15"]["id"])
    s15_late = _row(s15_rows, "LATE")
    assert s15_late["status"] == "PROJECTED_BREACH"
    assert s15_late["driver_milestone_code"] == "TENDER_ACCEPTED"

    s16_rows = _latest_risks(risks, plans["TL-S16"]["id"])
    s16_late = _row(s16_rows, "LATE")
    assert s16_late["status"] == "PROJECTED_BREACH"
    assert s16_late["driver_milestone_code"] == "APPOINTMENT_CONFIRMED"

    s17_rows = _latest_risks(risks, plans["TL-S17"]["id"])
    s17_late = _row(s17_rows, "LATE")
    assert s17_late["status"] == "PROJECTED_BREACH"
    assert s17_late["driver_milestone_code"] == "TENDER_ACCEPTED"


def test_reset_then_replay_gives_identical_risk_counts(simulator: TimelineSimulator) -> None:
    simulator.replay()
    first_count = len(simulator.risks.list_risks())

    simulator.reset()
    simulator.replay()
    second_count = len(simulator.risks.list_risks())

    assert first_count == second_count
    assert first_count > 0


def test_replay_twice_without_reset_gives_identical_risk_counts(simulator: TimelineSimulator) -> None:
    """`replay()` must self-clean: a second call with no `reset()` in between is idempotent."""
    simulator.replay()
    first_count = len(simulator.risks.list_risks())

    simulator.replay()
    second_count = len(simulator.risks.list_risks())

    assert first_count == second_count
    assert first_count > 0
