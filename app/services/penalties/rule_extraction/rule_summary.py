"""Deterministic, framework-free renderer of what the pricing engine will do with a rule.

`describe_rule` reads a rule's stored facts alone (no LLM call, no database access) and
renders the same three plain-English fields (`when`, `charge`, `how`) every time given
the same facts, so a reviewer's summary never drifts from what the compiler will
actually price.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

_BASIS_PHRASES = {
    "INVOICE_VALUE": "invoice value",
    "PO_VALUE": "PO value",
    "SHORTFALL_VALUE": "shortfall value",
    "SHORTFALL_UNITS": "shortfall units",
    "UNIT_COST": "unit cost",
    "WHOLESALE_PRICE": "wholesale price",
    "RETAIL_PRICE": "retail price",
    "COST_OF_GOODS": "cost of goods",
    "PRICE_DIFFERENTIAL": "price differential",
    "OTHER": "stated costs",
}

_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
_APPLIES_PER_UPPER_WORDS = {"PO", "ASN", "SKU", "DC"}

_OPERATOR_SYMBOLS = {
    "LT": "<",
    "LTE": "≤",
    "GT": ">",
    "GTE": "≥",
    "EQ": "=",
    "BETWEEN": "between",
}

_SETTLEMENT_PHRASES = {
    "INVOICE_DEDUCTION": "Collected via invoice deduction",
    "CREDIT_MEMO": "Collected via credit memo",
    "DIRECT_PAYMENT": "Collected via direct payment",
    "CHARGEBACK": "Collected via chargeback",
    "NETTING": "Collected via netting",
    "OTHER": "Collected via another stated method",
}

_SOURCE_TEXT_LIMIT = 160
_SOURCE_TEXT_CUT = 157


def describe_rule(
    *,
    calc_type: str,
    consequence_type: str | None,
    settlement_method: str | None,
    facts: list[dict[str, Any]],
) -> dict[str, str | None]:
    """Render what the pricing engine will do with this rule, from its stored facts alone."""
    by_branch: dict[Any, list[dict[str, Any]]] = {}
    for fact in facts:
        branch = _field(fact, "branch_no")
        if branch is None:
            branch = _field(fact, "group_no")
        if branch is None:
            branch = 0
        by_branch.setdefault(branch, []).append(fact)

    ordered_facts = [fact for branch in sorted(by_branch) for fact in by_branch[branch]]
    thresholds = [f for f in ordered_facts if f.get("attribute_role") == "THRESHOLD"]
    rates = [f for f in ordered_facts if f.get("attribute_role") == "RATE"]
    caps = [f for f in ordered_facts if f.get("attribute_role") == "CAP"]

    return {
        "when": _render_when(thresholds),
        "charge": _render_charge(calc_type, consequence_type, rates, caps),
        "how": _SETTLEMENT_PHRASES.get(settlement_method) if settlement_method else None,
    }


def _field(fact: dict[str, Any], key: str) -> Any:
    """Read an optional fact key from either a normalized pipeline row or a DB-shaped row."""
    value = fact.get(key)
    if value is not None:
        return value
    return (fact.get("extra") or {}).get(key)


def _num(v: Any) -> str:
    """Render a number with trailing zeros dropped and no exponent notation."""
    d = Decimal(str(v)).normalize()
    return format(d, "f")


def _amount(value: Any, unit: str | None) -> str:
    """Render one value with its unit, per the formatting rules in the task brief."""
    if unit is None:
        return _num(value)
    if unit == "PERCENT":
        return f"{_num(value)}%"
    if unit == "BASIS_POINTS":
        return f"{_num(value)} bps"
    if unit in _CURRENCY_SYMBOLS:
        symbol = _CURRENCY_SYMBOLS[unit]
        d = Decimal(str(value))
        if d == d.to_integral_value():
            return f"{symbol}{int(d):,}"
        return f"{symbol}{d:,.2f}"
    return f"{_num(value)} {unit.lower().replace('_', ' ')}"


def _basis_phrase(unit: str | None, basis_type: str | None) -> str:
    """Render the ' of <basis>' suffix, only for percent/bps units with a real basis."""
    if unit not in ("PERCENT", "BASIS_POINTS"):
        return ""
    if basis_type is None or basis_type == "NONE":
        return ""
    return " of " + _BASIS_PHRASES[basis_type]


def _per_phrase(applies_per: str | None) -> str:
    """Render the ' per <label>' suffix for a RATE's applies_per."""
    if applies_per is None or applies_per == "NONE":
        return ""
    words = applies_per.split("_")
    label = " ".join(word if word in _APPLIES_PER_UPPER_WORDS else word.lower() for word in words)
    return f" per {label}"


def _rate_phrase(fact: dict[str, Any]) -> str:
    """Render one RATE row's phrase: the amount, its basis, and its per-unit, or a not-stated note."""
    value = fact.get("value")
    unit = fact.get("value_unit")
    if value is None:
        value_status = fact.get("value_status") or "NOT_STATED"
        return f"amount not stated ({value_status.lower().replace('_', ' ')})"
    basis_type = _field(fact, "basis_type")
    applies_per = _field(fact, "applies_per")
    return _amount(value, unit) + _basis_phrase(unit, basis_type) + _per_phrase(applies_per)


def _join_or(phrases: list[str]) -> str:
    """Join phrases with 'or', Oxford-comma style for three or more."""
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return f"{phrases[0]} or {phrases[1]}"
    return ", ".join(phrases[:-1]) + f" or {phrases[-1]}"


def _combine_rate_phrases(rates: list[dict[str, Any]]) -> str:
    """Combine every RATE row's phrase per the first row's combinator, in branch/input order."""
    phrases = [_rate_phrase(rate) for rate in rates]
    if len(phrases) == 1:
        return phrases[0]

    combinator = None
    for rate in rates:
        candidate = _field(rate, "combinator")
        if candidate in ("MAX", "MIN", "SUM"):
            combinator = candidate
            break

    if combinator == "MAX":
        return "greater of " + _join_or(phrases)
    if combinator == "MIN":
        return "lesser of " + _join_or(phrases)
    if combinator == "SUM":
        return " plus ".join(phrases)
    return "; ".join(phrases)


def _render_charge(
    calc_type: str, consequence_type: str | None, rates: list[dict[str, Any]], caps: list[dict[str, Any]]
) -> str:
    """Render the `charge` field: what is billed, or the non-monetary remedy, plus any cap."""
    if calc_type == "NON_MONETARY":
        return "Not charged — " + (
            consequence_type.lower().replace("_", " ") if consequence_type else "non-monetary remedy"
        )
    if not rates:
        return "No chargeable amount extracted"

    charge = _combine_rate_phrases(rates)
    for cap in caps:
        cap_value = cap.get("value")
        if cap_value is None:
            continue
        charge += f", capped at {_amount(cap_value, cap.get('value_unit'))}"
    return charge


def _collapse_whitespace(text: str) -> str:
    """Collapse internal whitespace to single spaces and truncate long source text."""
    collapsed = " ".join(text.split())
    if len(collapsed) > _SOURCE_TEXT_LIMIT:
        return collapsed[:_SOURCE_TEXT_CUT] + "…"
    return collapsed


def _threshold_condition(fact: dict[str, Any]) -> str:
    """Render a THRESHOLD row's `[metric_code op cond]` bracket, or '' when the operator is not renderable."""
    value = fact.get("value")
    if value is None:
        return ""
    operator = fact.get("operator")
    if not isinstance(operator, str):
        return ""
    op_symbol = _OPERATOR_SYMBOLS.get(operator)
    if op_symbol is None:
        return ""

    unit = fact.get("value_unit")
    if operator == "BETWEEN":
        cond = f"{_amount(value, unit)} and {_amount(fact.get('value_max'), unit)}"
    else:
        cond = _amount(value, unit)

    metric_code = fact.get("metric_code")
    inner = f"{metric_code} {op_symbol} {cond}" if metric_code else f"{op_symbol} {cond}"
    return f" [{inner}]"


def _render_when(thresholds: list[dict[str, Any]]) -> str:
    """Render the `when` field: every THRESHOLD row's source text plus its condition bracket."""
    if not thresholds:
        return "As stated in the clause (no measurable trigger extracted)"

    rendered: list[str] = []
    seen: set[str] = set()
    any_or = False
    for fact in thresholds:
        if _field(fact, "trigger_logic") == "OR":
            any_or = True
        source_text = _collapse_whitespace(fact.get("source_text") or "")
        piece = source_text + _threshold_condition(fact)
        if piece in seen:
            continue
        seen.add(piece)
        rendered.append(piece)

    joiner = " OR " if any_or else " AND "
    return joiner.join(rendered)
