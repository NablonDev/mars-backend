"""Ops-alert lifecycle: one tracked `timeline_alert` per (fulfillment plan, risk type).

Pure module, framework-free: given one run date's current PROJECTED_BREACH/BREACHED
risks and the plan's existing tracked alerts, computes the instructions the repository
must apply (create, update, close, or stop tracking) so a plan keeps at most one
tracked alert per risk type across engine runs. SLIPPING risk never reaches this
module -- callers filter to PROJECTED_BREACH/BREACHED before calling `plan_alert_changes`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

OPEN_STATUSES = frozenset({"NEW", "ACKNOWLEDGED", "ACTION_TAKEN"})

_CREATE = "CREATE"
_UPDATE = "UPDATE"
_CLOSE = "CLOSE"
_STOP_TRACKING = "STOP_TRACKING"


@dataclass(frozen=True)
class CurrentRisk:
    """One PROJECTED_BREACH/BREACHED risk row from this run, as seen by the alert engine."""

    risk_type: str
    status: str
    penalty_amount: float
    days_off: int | None
    shortfall_quantity: float | None


@dataclass(frozen=True)
class AlertState:
    """A plan's existing tracked alert for one risk type, as read from the repository."""

    alert_id: str
    risk_type: str
    status: str
    last_seen_date: date
    prev_penalty_amount: float | None
    last_penalty_amount: float
    prev_days_off: int | None
    last_days_off: int | None
    prev_shortfall_quantity: float | None
    last_shortfall_quantity: float | None


@dataclass(frozen=True)
class AlertChange:
    """One instruction for `TimelineAlertRepository.apply_changes` to carry out."""

    kind: str
    risk_type: str
    alert_id: str | None
    fields: Mapping[str, object]


def plan_alert_changes(
    run_date: date, risks: Sequence[CurrentRisk], tracking: Sequence[AlertState], *, now: datetime
) -> list[AlertChange]:
    """Compute this run's alert changes for one plan: rule 1 updates, rule 2 creates, rule 3 closes."""
    tracking_by_type = {state.risk_type: state for state in tracking}
    changes: list[AlertChange] = []

    for risk in risks:
        state = tracking_by_type.get(risk.risk_type)
        if state is not None:
            changes.append(_update_existing(run_date, risk, state, now))
        else:
            changes.append(_create_new(run_date, risk, now))

    current_types = {risk.risk_type for risk in risks}
    for state in tracking:
        if state.risk_type not in current_types:
            changes.append(_close_or_stop_tracking(state, now))

    return changes


def _update_existing(run_date: date, risk: CurrentRisk, state: AlertState, now: datetime) -> AlertChange:
    """Rule 1: snapshot-shift an existing tracking alert against this run's current risk."""
    prev_penalty: float | None
    if state.last_seen_date < run_date:
        prev_penalty = state.last_penalty_amount
        prev_days_off = state.last_days_off
        prev_shortfall = state.last_shortfall_quantity
    else:
        prev_penalty = state.prev_penalty_amount
        prev_days_off = state.prev_days_off
        prev_shortfall = state.prev_shortfall_quantity

    fields: dict[str, object] = {
        "last_seen_date": run_date,
        "prev_penalty_amount": prev_penalty,
        "last_penalty_amount": risk.penalty_amount,
        "prev_days_off": prev_days_off,
        "last_days_off": risk.days_off,
        "prev_shortfall_quantity": prev_shortfall,
        "last_shortfall_quantity": risk.shortfall_quantity,
        "change_type": _classify_change(prev_penalty, prev_days_off, prev_shortfall, risk),
    }

    if state.status in OPEN_STATUSES and risk.status == "BREACHED" and risk.penalty_amount > 0:
        fields.update(status="PENALTY_INCURRED", closed_at=now, closed_reason="BREACHED")
        kind = _CLOSE
    else:
        kind = _UPDATE

    return AlertChange(kind=kind, risk_type=risk.risk_type, alert_id=state.alert_id, fields=fields)


def _classify_change(
    prev_penalty: float | None, prev_days_off: int | None, prev_shortfall: float | None, risk: CurrentRisk
) -> str:
    """WORSE if any measure increased, BETTER if any decreased and none increased, else SAME/NEW."""
    if prev_penalty is None and prev_days_off is None and prev_shortfall is None:
        return "NEW"

    increased = False
    decreased = False

    def _compare(prev: float | None, current: float | None, *, absolute: bool = False) -> None:
        nonlocal increased, decreased
        if prev is None or current is None:
            return
        prev_value, current_value = (abs(prev), abs(current)) if absolute else (prev, current)
        if current_value > prev_value:
            increased = True
        elif current_value < prev_value:
            decreased = True

    _compare(prev_penalty, risk.penalty_amount)
    _compare(prev_days_off, risk.days_off, absolute=True)
    _compare(prev_shortfall, risk.shortfall_quantity)

    if increased:
        return "WORSE"
    if decreased:
        return "BETTER"
    return "SAME"


def _create_new(run_date: date, risk: CurrentRisk, now: datetime) -> AlertChange:
    """Rule 2: a current risk with no tracking alert starts a brand-new one."""
    fields: dict[str, object] = {
        "status": "NEW",
        "change_type": "NEW",
        "first_seen_date": run_date,
        "last_seen_date": run_date,
        "prev_penalty_amount": None,
        "last_penalty_amount": risk.penalty_amount,
        "prev_days_off": None,
        "last_days_off": risk.days_off,
        "prev_shortfall_quantity": None,
        "last_shortfall_quantity": risk.shortfall_quantity,
        "is_tracking": True,
    }
    if risk.status == "BREACHED" and risk.penalty_amount > 0:
        fields.update(status="PENALTY_INCURRED", closed_at=now, closed_reason="BREACHED")

    return AlertChange(kind=_CREATE, risk_type=risk.risk_type, alert_id=None, fields=fields)


def _close_or_stop_tracking(state: AlertState, now: datetime) -> AlertChange:
    """Rule 3: a tracking alert whose risk type is gone from this run either closes or stops tracking."""
    if state.status in OPEN_STATUSES:
        closed_reason = "FIX_WORKED" if state.status == "ACTION_TAKEN" else "AUTO_CLEARED"
        fields: dict[str, object] = {
            "status": "RESOLVED",
            "closed_reason": closed_reason,
            "closed_at": now,
            "is_tracking": False,
        }
        return AlertChange(kind=_CLOSE, risk_type=state.risk_type, alert_id=state.alert_id, fields=fields)

    return AlertChange(
        kind=_STOP_TRACKING, risk_type=state.risk_type, alert_id=state.alert_id, fields={"is_tracking": False}
    )
