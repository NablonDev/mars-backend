"""The cross-row checks: rules that need to see a rule's sibling attribute rows, so
they cannot be expressed as a single-row database CHECK constraint like most of the
schema's conditional rules.

Rule 1 (pure-cap misfiling), Rule 4 (tier_application required beside a THRESHOLD),
Rule 5 (tier bands must not gap or overlap, TIERED only), Rule 9 (a combinator group
must not mix a selection with a tally), Rule 13 (trigger_logic must agree across a
branch's THRESHOLD rows), Rule 14 (a fractional-period RATE needs a ROUNDING_RULE
sibling), Rule 15 (a RATE must not carry a time unit). Numbers match the schema and
data dictionary the extraction prompts are built against: the issue strings this module
produces are persisted into review notes.
"""

import itertools
from typing import Any

from app.services.penalties.rule_extraction.vocabulary import DURATION_APPLIES_PER, tier_bands_are_contiguous

# Mirrors `PenaltyFact.value_unit`'s time-duration literals: a duration is a THRESHOLD,
# TIME_WINDOW, GRACE_PERIOD or CURE_PERIOD row's business, never a RATE's.
_TIME_UNITS = (
    "CALENDAR_DAYS",
    "BUSINESS_DAYS",
    "HOURS",
    "MINUTES",
    "WEEKS",
    "MONTHS",
    "QUARTERS",
    "YEARS",
)


def consistency_issues(facts: list[dict[str, Any]], calc_type: str) -> list[str]:
    """Return a list of human-readable issue strings; empty means nothing found."""
    issues: list[str] = []

    cap_facts = [f for f in facts if f.get("attribute_role") == "CAP"]
    other_facts = [f for f in facts if f.get("attribute_role") != "CAP"]
    if cap_facts and not other_facts and not any(f.get("group_no", 1) >= 1 for f in cap_facts):
        issues.append("Rule 1 (pure-cap misfiling): group_no=0 rows exist with nothing at group_no>=1.")

    by_group: dict[Any, list[dict[str, Any]]] = {}
    for fact in facts:
        by_group.setdefault(fact.get("group_no", 1), []).append(fact)

    for group_no, rows in by_group.items():
        roles = {r.get("attribute_role") for r in rows}
        if "RATE" in roles and "THRESHOLD" in roles:
            for r in rows:
                if r.get("attribute_role") == "RATE" and not r.get("tier_application"):
                    issues.append(
                        f"Rule 4 (tier_application required): group_no={group_no} has a RATE "
                        "beside a THRESHOLD with tier_application unset."
                    )

    if calc_type == "TIERED":
        issues.extend(_check_tier_band_continuity(facts))
    issues.extend(_check_trigger_logic_homogeneity(by_group))
    issues.extend(_check_fractional_rate_rounding(facts, by_group))
    issues.extend(_check_combinator_shapes(facts))
    issues.extend(_check_time_unit_rates(facts))
    return issues


def _normalize_interval(fact: dict[str, Any]) -> tuple | None:
    """A fact's bound as `(lower, lower_inclusive, upper, upper_inclusive)`, or `None`."""
    op = fact.get("operator")
    value = fact.get("value")
    if op in (None, "ALWAYS", "EQ") or value is None:
        return None
    if op == "BETWEEN":
        return (
            value,
            bool(fact.get("lower_bound_inclusive")),
            fact.get("value_max"),
            bool(fact.get("upper_bound_inclusive")),
        )
    if op == "LT":
        return None, None, value, False
    if op == "LTE":
        return None, None, value, True
    if op == "GT":
        return value, False, None, None
    if op == "GTE":
        return value, True, None, None
    return None


def _check_tier_band_continuity(facts: list[dict[str, Any]]) -> list[str]:
    """Rule 5: adjacent tier rungs sharing a metric must not gap or overlap.

    Calls the same `tier_bands_are_contiguous` predicate as `publisher`'s
    TIER_BAND_GAP rejection, so the two can never disagree about what a gap is.
    """
    issues = []
    thresholds = [f for f in facts if f.get("attribute_role") == "THRESHOLD" and f.get("group_no", 1) >= 1]
    by_metric: dict[Any, list[dict[str, Any]]] = {}
    for fact in thresholds:
        metric_code = fact.get("metric_code")
        if metric_code is not None:
            by_metric.setdefault(metric_code, []).append(fact)

    for metric_code, rows in by_metric.items():
        if len(rows) < 2:
            continue
        intervals = [(r.get("group_no"), iv) for r in rows if (iv := _normalize_interval(r)) is not None]
        intervals.sort(key=lambda x: (x[1][0] is not None, x[1][0]))
        for (g1, (_lo1, _loi1, hi1, hii1)), (g2, (lo2, loi2, _hi2, _hii2)) in itertools.pairwise(intervals):
            if hi1 is None or lo2 is None:
                continue  # one end is open-ended, nothing to check against the next rung
            if not tier_bands_are_contiguous(hi1, lo2):
                issues.append(
                    f"Rule 5 (tier gap/overlap): metric {metric_code} group_no {g1}->{g2} has a gap/overlap between {hi1} and {lo2}."
                )
            elif hii1 == loi2:
                which = "include" if hii1 else "exclude"
                issues.append(
                    f"Rule 5 (tier gap/overlap): metric {metric_code} group_no {g1}->{g2} both {which} the boundary {hi1} - exactly one side must."
                )
    return issues


def _check_trigger_logic_homogeneity(by_group: dict[Any, list[dict[str, Any]]]) -> list[str]:
    """Rule 13: sibling THRESHOLD rows in one branch must agree on trigger_logic."""
    issues = []
    for group_no, rows in by_group.items():
        thresholds = [r for r in rows if r.get("attribute_role") == "THRESHOLD"]
        if len(thresholds) < 2:
            continue
        logics = {r.get("trigger_logic") for r in thresholds if r.get("trigger_logic")}
        if len(logics) > 1:
            issues.append(
                f"Rule 13 (trigger_logic homogeneity): group_no={group_no} has {len(thresholds)} THRESHOLD "
                f"rows with conflicting trigger_logic values {sorted(str(v) for v in logics)} - an undefined boolean combination."
            )
    return issues


def _check_fractional_rate_rounding(
    facts: list[dict[str, Any]], by_group: dict[Any, list[dict[str, Any]]]
) -> list[str]:
    """Rule 14: a RATE row accruing per fractional time period needs a ROUNDING_RULE sibling.

    A duration landing mid-period has no stated rounding convention otherwise, and three
    different totals (round up / round down / prorate) are all equally consistent with
    the extracted row.
    """
    issues = []
    has_group0_rounding = any(
        f.get("attribute_role") == "ROUNDING_RULE" and f.get("group_no", 1) == 0 for f in facts
    )
    for group_no, rows in by_group.items():
        fractional_rates = [
            r
            for r in rows
            if r.get("attribute_role") == "RATE" and r.get("applies_per") in DURATION_APPLIES_PER
        ]
        if not fractional_rates:
            continue
        has_local_rounding = any(r.get("attribute_role") == "ROUNDING_RULE" for r in rows)
        if not has_local_rounding and not has_group0_rounding:
            for r in fractional_rates:
                issues.append(
                    f"Rule 14 (rounding required): group_no={group_no} has a RATE with applies_per="
                    f"{r.get('applies_per')} (fractional-period accrual) but no ROUNDING_RULE sibling in this "
                    "group or at group_no=0 - a duration landing mid-period is not mechanically computable."
                )
    return issues


def _check_combinator_shapes(facts: list[dict[str, Any]]) -> list[str]:
    """Rule 9: a flat combinator group must not mix a selection (MAX/MIN) with a tally
    (SUM/SUBTRACT/NONE)."""
    issues = []
    combinator_groups: dict[tuple[Any, Any], set[Any]] = {}
    for fact in facts:
        if fact.get("combinator"):
            key = (fact.get("group_no", 1), fact.get("attribute_role"))
            combinator_groups.setdefault(key, set()).add(fact["combinator"])
    tally_shape = {"SUM", "SUBTRACT", "NONE"}
    for key, combos in combinator_groups.items():
        is_tally = combos <= tally_shape
        is_selection = combos in ({"MAX"}, {"MIN"})
        if not (is_tally or is_selection):
            issues.append(
                f"Rule 9 (mixed combinator shapes): group_no/role {key} mixes a selection with a tally: {combos}."
            )
    return issues


def _check_time_unit_rates(facts: list[dict[str, Any]]) -> list[str]:
    """Rule 15: a RATE row must never carry a time-duration value_unit.

    A time limit is a deadline, not a monetary rate: it belongs on a THRESHOLD,
    TIME_WINDOW, GRACE_PERIOD or CURE_PERIOD row instead.
    """
    issues = []
    for fact in facts:
        if fact.get("attribute_role") != "RATE":
            continue
        unit = fact.get("value_unit")
        if unit in _TIME_UNITS:
            group_no = fact.get("group_no", 1)
            issues.append(
                f"Rule 15 (time value as rate): group_no={group_no} has a RATE with value_unit={unit}; "
                "a time limit is a THRESHOLD, TIME_WINDOW, GRACE_PERIOD or CURE_PERIOD row, never a RATE."
            )
    return issues
