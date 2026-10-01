"""Pure tests for `plan_alert_changes`, covering every rule in the brief."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.services.penalties.timeline.alerts import AlertState, CurrentRisk, plan_alert_changes

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _risk(**overrides) -> CurrentRisk:
    base: dict = {
        "risk_type": "LATE",
        "status": "PROJECTED_BREACH",
        "penalty_amount": 100.0,
        "days_off": 2,
        "shortfall_quantity": None,
    }
    base.update(overrides)
    return CurrentRisk(**base)


def _state(**overrides) -> AlertState:
    base: dict = {
        "alert_id": "alert-1",
        "risk_type": "LATE",
        "status": "NEW",
        "last_seen_date": date(2026, 1, 9),
        "prev_penalty_amount": None,
        "last_penalty_amount": 80.0,
        "prev_days_off": None,
        "last_days_off": 1,
        "prev_shortfall_quantity": None,
        "last_shortfall_quantity": None,
    }
    base.update(overrides)
    return AlertState(**base)


def test_new_risk_with_no_tracking_alert_creates_one() -> None:
    changes = plan_alert_changes(date(2026, 1, 10), [_risk()], [], now=NOW)

    assert len(changes) == 1
    change = changes[0]
    assert change.kind == "CREATE"
    assert change.alert_id is None
    assert change.risk_type == "LATE"
    assert change.fields["status"] == "NEW"
    assert change.fields["change_type"] == "NEW"
    assert change.fields["first_seen_date"] == date(2026, 1, 10)
    assert change.fields["last_seen_date"] == date(2026, 1, 10)
    assert change.fields["prev_penalty_amount"] is None
    assert change.fields["last_penalty_amount"] == 100.0
    assert change.fields["is_tracking"] is True


def test_new_breached_risk_with_penalty_creates_a_penalty_incurred_alert() -> None:
    risk = _risk(status="BREACHED", penalty_amount=250.0)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [], now=NOW)

    change = changes[0]
    assert change.kind == "CREATE"
    assert change.fields["status"] == "PENALTY_INCURRED"
    assert change.fields["closed_reason"] == "BREACHED"
    assert change.fields["closed_at"] == NOW
    assert change.fields["is_tracking"] is True


def test_worsening_risk_shifts_snapshot_and_marks_worse() -> None:
    state = _state(last_seen_date=date(2026, 1, 9), last_penalty_amount=80.0, last_days_off=1)
    risk = _risk(penalty_amount=150.0, days_off=3)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    change = changes[0]
    assert change.kind == "UPDATE"
    assert change.alert_id == "alert-1"
    assert change.fields["prev_penalty_amount"] == 80.0
    assert change.fields["last_penalty_amount"] == 150.0
    assert change.fields["prev_days_off"] == 1
    assert change.fields["last_days_off"] == 3
    assert change.fields["change_type"] == "WORSE"
    assert change.fields["last_seen_date"] == date(2026, 1, 10)


def test_improving_risk_with_no_increase_marks_better() -> None:
    state = _state(last_seen_date=date(2026, 1, 9), last_penalty_amount=200.0, last_days_off=5)
    risk = _risk(penalty_amount=100.0, days_off=2)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    assert changes[0].fields["change_type"] == "BETTER"


def test_unchanged_risk_marks_same() -> None:
    state = _state(last_seen_date=date(2026, 1, 9), last_penalty_amount=100.0, last_days_off=2)
    risk = _risk(penalty_amount=100.0, days_off=2)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    assert changes[0].fields["change_type"] == "SAME"


def test_days_off_comparison_uses_absolute_value() -> None:
    """An EARLY risk's `days_off` growing more negative still counts as worse (magnitude, not sign)."""
    state = _state(last_seen_date=date(2026, 1, 9), last_days_off=-1, last_penalty_amount=50.0)
    risk = _risk(days_off=-4, penalty_amount=50.0)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    assert changes[0].fields["change_type"] == "WORSE"


def test_rerun_same_date_keeps_prev_as_is() -> None:
    """A same-day re-run does not shift the snapshot: prev_* stays whatever it already was."""
    state = _state(
        last_seen_date=date(2026, 1, 10),
        prev_penalty_amount=None,
        last_penalty_amount=100.0,
        prev_days_off=None,
        last_days_off=2,
    )
    risk = _risk(penalty_amount=120.0, days_off=3)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    change = changes[0]
    assert change.fields["prev_penalty_amount"] is None
    assert change.fields["prev_days_off"] is None
    assert change.fields["last_penalty_amount"] == 120.0
    assert change.fields["change_type"] == "NEW"


def test_open_alert_closes_to_penalty_incurred_when_risk_breaches_with_penalty() -> None:
    state = _state(status="ACKNOWLEDGED", last_seen_date=date(2026, 1, 9))
    risk = _risk(status="BREACHED", penalty_amount=300.0)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    change = changes[0]
    assert change.kind == "CLOSE"
    assert change.fields["status"] == "PENALTY_INCURRED"
    assert change.fields["closed_reason"] == "BREACHED"
    assert change.fields["closed_at"] == NOW


def test_action_taken_alert_still_incurs_penalty_when_breached() -> None:
    state = _state(status="ACTION_TAKEN", last_seen_date=date(2026, 1, 9))
    risk = _risk(status="BREACHED", penalty_amount=10.0)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    assert changes[0].kind == "CLOSE"
    assert changes[0].fields["status"] == "PENALTY_INCURRED"


def test_dismissed_alert_keeps_receiving_updates_without_spawning_a_new_alert() -> None:
    state = _state(status="DISMISSED", last_seen_date=date(2026, 1, 9), last_penalty_amount=100.0)
    risk = _risk(penalty_amount=150.0)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    assert len(changes) == 1
    change = changes[0]
    assert change.kind == "UPDATE"
    assert change.alert_id == "alert-1"
    assert change.fields["last_penalty_amount"] == 150.0
    assert change.fields["change_type"] == "WORSE"


def test_penalty_incurred_alert_with_penalty_still_open_risk_does_not_close_again() -> None:
    state = _state(status="PENALTY_INCURRED", last_seen_date=date(2026, 1, 9))
    risk = _risk(status="BREACHED", penalty_amount=300.0)

    changes = plan_alert_changes(date(2026, 1, 10), [risk], [state], now=NOW)

    assert changes[0].kind == "UPDATE"


def test_open_alert_with_risk_type_now_absent_resolves_as_auto_cleared() -> None:
    state = _state(status="NEW", last_seen_date=date(2026, 1, 9))

    changes = plan_alert_changes(date(2026, 1, 10), [], [state], now=NOW)

    change = changes[0]
    assert change.kind == "CLOSE"
    assert change.fields["status"] == "RESOLVED"
    assert change.fields["closed_reason"] == "AUTO_CLEARED"
    assert change.fields["closed_at"] == NOW
    assert change.fields["is_tracking"] is False


def test_action_taken_alert_with_risk_type_now_absent_resolves_as_fix_worked() -> None:
    state = _state(status="ACTION_TAKEN", last_seen_date=date(2026, 1, 9))

    changes = plan_alert_changes(date(2026, 1, 10), [], [state], now=NOW)

    assert changes[0].fields["closed_reason"] == "FIX_WORKED"


def test_closed_alert_with_risk_type_now_absent_stops_tracking() -> None:
    state = _state(status="DISMISSED", last_seen_date=date(2026, 1, 9))

    changes = plan_alert_changes(date(2026, 1, 10), [], [state], now=NOW)

    change = changes[0]
    assert change.kind == "STOP_TRACKING"
    assert change.fields == {"is_tracking": False}


def test_penalty_incurred_alert_with_risk_type_now_absent_stops_tracking() -> None:
    state = _state(status="PENALTY_INCURRED", last_seen_date=date(2026, 1, 9))

    changes = plan_alert_changes(date(2026, 1, 10), [], [state], now=NOW)

    assert changes[0].kind == "STOP_TRACKING"


def test_reappearance_after_resolve_creates_a_new_alert() -> None:
    """A resolved alert (is_tracking False, no longer surfaced by the repository) does not
    suppress a fresh CREATE when the same risk type reappears later."""
    risk = _risk()

    changes = plan_alert_changes(date(2026, 1, 15), [risk], [], now=NOW)

    assert changes[0].kind == "CREATE"


def test_mixed_run_creates_updates_and_closes_in_one_call() -> None:
    tracked_late = _state(risk_type="LATE", status="NEW", last_seen_date=date(2026, 1, 9))
    tracked_short = _state(risk_type="SHORT", status="ACKNOWLEDGED", last_seen_date=date(2026, 1, 9))
    current_late = _risk(risk_type="LATE", penalty_amount=90.0)
    current_new_short = _risk(risk_type="EARLY", status="PROJECTED_BREACH", penalty_amount=40.0, days_off=-1)

    changes = plan_alert_changes(
        date(2026, 1, 10), [current_late, current_new_short], [tracked_late, tracked_short], now=NOW
    )

    kinds_by_type = {c.risk_type: c.kind for c in changes}
    assert kinds_by_type == {"LATE": "UPDATE", "EARLY": "CREATE", "SHORT": "CLOSE"}
