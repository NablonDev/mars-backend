"""Normalize extracted fact rows and derive a rule's `pricing_readiness`.

The module has two stages:

1. `normalize_facts` makes extracted rows deterministic by deduplicating identical
   facts and applying a canonical sort order.
2. `evaluate_readiness` determines whether the normalized facts contain enough
   information to price the rule safely.

Normalization is pure deterministic post-processing. This matters because extraction
latitude can otherwise change the number or order of persisted attribute rows between
runs.

`pricing_readiness` is intentionally conservative: a false `AWAITING_DATA` costs a
review, while a false `READY` can produce a charge with no contractual basis.

A fact is one row destined for `penalties.extracted_penalty_rule_attribute`. Its
identity is defined by `IDENTITY_FIELDS`; prose and audit metadata such as `raw_text`
do not make otherwise identical facts distinct.

Readiness values:

    READY                  every input required for calculation is present
    NEEDS_EXTERNAL_FIGURE  the calculation shape is known, but a required number
                           lives outside the contract
    AWAITING_DATA          a required input was not captured or is unusable
    UNSUPPORTED_SHAPE      the clause promises a penalty without a supported form
    NOT_A_CHARGE           the rule is non-monetary or represents only a limit
"""

from typing import Any

# Fields that make an attribute row a distinct fact. Two rows agreeing on all of these
# assert the same thing about the rule, whatever prose accompanies them.
IDENTITY_FIELDS = (
    "group_no",
    "attribute_role",
    "segment_key",
    "segment_value",
    "metric_code",
    "metric_denominator",
    "metric_numerator",
    "measurement_level",
    "operator",
    "value",
    "value_max",
    "value_unit",
    "currency_code",
    "lower_bound_inclusive",
    "upper_bound_inclusive",
    "applies_per",
    "applies_per_secondary",
    "basis_type",
    "basis_exclusions",
    "applicability_dimension",
    "applicability_value",
    "tier_application",
    "cap_scope",
    "window_type",
    "window_length",
    "window_length_unit",
    "event_anchor",
    "calendar_window_start",
    "calendar_window_end",
    "occurrence_reset_type",
    "occurrence_reset_length",
    "occurrence_reset_unit",
    "combinator",
    "rounding_convention",
    "interest_day_count_convention",
    "interest_compounding",
    "external_reference_type",
    "is_dynamic_reference",
)

# Canonical role order: condition → charge → bound → timing → supporting detail.
_ROLE_ORDER = {
    "THRESHOLD": 0,
    "RATE": 1,
    "CAP": 2,
    "FLOOR": 3,
    "QUANTITY": 4,
    "BASIS": 5,
    "ESCALATION_FACTOR": 6,
    "GRACE_PERIOD": 7,
    "CURE_PERIOD": 8,
    "TIME_WINDOW": 9,
    "ROUNDING_RULE": 10,
    "EXCLUSION_CONDITION": 11,
    "OTHER": 12,
}

# Calculation shapes that require a monetary RATE.
MONETARY_CALC_TYPES = (
    "PER_UNIT",
    "PERCENT_OF_PO",
    "PERCENT_OF_INVOICE",
    "FLAT_FEE",
    "TIERED",
    "FORMULA_OTHER",
)

# These statuses mean a value cannot safely be used for pricing.
BLOCKING_VALUE_STATUSES = (
    "NOT_STATED",
    "REDACTED",
    "EXTRACTION_UNCERTAIN",
)


def normalize_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate fact rows and return them in canonical, reproducible order."""
    deduplicated = _deduplicate(facts)
    return sorted(deduplicated, key=_sort_key)


def evaluate_readiness(calc_type: str | None, facts: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Derive `pricing_readiness` from a calculation type and normalized facts.

    Returns `(pricing_readiness, notes)`. Readiness is deliberately conservative:
    missing or contradictory inputs result in a non-READY state rather than allowing
    a rule with insufficient contractual data to reach pricing.
    """
    if not calc_type or calc_type == "UNSPECIFIED":
        return "UNSUPPORTED_SHAPE", ["the clause names no calculation shape"]
    if calc_type in ("NON_MONETARY", "LIMIT_ONLY"):
        return "NOT_A_CHARGE", []
    if not facts:
        return "AWAITING_DATA", ["no attribute rows were captured"]

    if any(f.get("is_dynamic_reference") or f.get("value_status") == "EXTERNAL_REFERENCE" for f in facts):
        return "NEEDS_EXTERNAL_FIGURE", ["a fact's value lives outside the contract"]

    if any(f.get("value_status") in BLOCKING_VALUE_STATUSES for f in facts):
        return "AWAITING_DATA", ["a fact's value_status blocks pricing"]

    # PRESENT without a value is internally inconsistent: the row claims that a
    # value was captured, but the value itself is absent.
    if any(f.get("value_status") == "PRESENT" and f.get("value") is None for f in facts):
        return "AWAITING_DATA", ["a fact claims value_status=PRESENT with no value"]

    if calc_type in MONETARY_CALC_TYPES:
        rates = [f for f in facts if f.get("attribute_role") == "RATE"]
        if not rates:
            return "AWAITING_DATA", ["no RATE fact was captured"]
        if not any(r.get("value") is not None or r.get("value_max") is not None for r in rates):
            return "AWAITING_DATA", ["the RATE fact names no number"]

    return "READY", []


def _deduplicate(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical facts while preserving the strongest audit trail."""
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for fact in facts:
        key = tuple(fact.get(f) for f in IDENTITY_FIELDS)
        if key not in seen:
            seen[key] = fact
            continue
        kept = seen[key]
        # Keep the longer source fragment as the stronger audit trail.
        if len(str(fact.get("raw_text") or "")) > len(str(kept.get("raw_text") or "")):
            kept["raw_text"] = fact.get("raw_text")
        # Preserve review flags from either duplicate.
        if fact.get("needs_review") and not kept.get("needs_review"):
            kept["needs_review"] = True
            kept["review_notes"] = fact.get("review_notes") or kept.get("review_notes")
    return list(seen.values())


def _sort_key(fact: dict[str, Any]) -> tuple[Any, ...]:
    """Return a total, deterministic ordering key for a fact row.

    The identity tuple is the final tiebreaker. This prevents rows that differ only
    in non-leading fields from falling back to model emission order, which would make
    persisted row order unstable between runs.
    """
    value = fact.get("value")
    return (
        fact.get("group_no") if fact.get("group_no") is not None else 999,
        _ROLE_ORDER.get(str(fact.get("attribute_role") or ""), 99),
        str(fact.get("metric_code") or ""),
        str(fact.get("operator") or ""),
        float(value) if isinstance(value, (int, float)) else 0.0,
        str(fact.get("value_unit") or ""),
        str(fact.get("basis_type") or ""),
        tuple(str(fact.get(f)) for f in IDENTITY_FIELDS),
    )
