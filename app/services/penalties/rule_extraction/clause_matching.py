"""Locate screened-clause evidence and determine its PO scope.

Excerpt matching verifies that screened evidence can be located in the source
contract. PO-scope classification separately determines whether the clause
belongs in a delay/shortage-scoped run.

The two concerns are intentionally kept separate:
- excerpt matching operates on source-text evidence;
- PO scoping operates on the classifier's category and PO flags.
"""

from dataclasses import dataclass
from typing import Literal

from app.services.penalties.rule_extraction.segmentation import ScreeningUnit
from app.services.penalties.rule_extraction.vocabulary import CategoryDefaults

MatchKind = Literal["EXACT", "NORMALIZED", "UNIT_FALLBACK"]

_OUT_OF_SCOPE = "OUT_OF_SCOPE"

# Governed scope role for each penalty category.
#
# This is separate from the PO flags, which describe the trigger for reporting:
# - TRIGGER_SHORTAGE / TRIGGER_DELAY / TRIGGER_BOTH / TRIGGER_QUANTITY
#   trigger a PO charge.
# - BOUNDS_PO bounds PO exposure without triggering a charge.
# - UNKNOWN_EXPOSURE preserves a promised but unquantified PO exposure.
# - OUT_OF_SCOPE covers penalties whose trigger is unrelated to PO delay,
#   shortage, or quantity.

CATEGORY_SCOPE: dict[str, str] = {
    "SHORT_SHIP": "TRIGGER_SHORTAGE",
    "MINIMUM_VOLUME_SHORTFALL": "TRIGGER_SHORTAGE",
    "OTIF_LATE": "TRIGGER_BOTH",
    "DELIVERY_WINDOW_VIOLATION": "TRIGGER_DELAY",
    "DELIVERY_ACCEPTANCE_COST_SHIFT": "TRIGGER_DELAY",
    # A cover purchase's trigger is the failure to deliver, on date and quantity alike.
    "ALTERNATE_SOURCING_MARKUP": "TRIGGER_BOTH",
    "STORAGE_DURATION_FEE": "TRIGGER_DELAY",
    # Over-delivery is the same PO-quantity axis as a shortfall, the other direction.
    "OVERAGE_CHARGEBACK": "TRIGGER_QUANTITY",
    "OVERAGE_NONPAYMENT": "TRIGGER_QUANTITY",
    # An uncapped per-day late fee overstates exposure and cannot survive a dispute.
    "AGGREGATE_LIABILITY_CAP": "BOUNDS_PO",
    # Dropping these loses the only record that an unquantified PO exposure exists.
    "UNSPECIFIED_EXTERNAL": "UNKNOWN_EXPOSURE",
    "UNSPECIFIED_INTERNAL": "UNKNOWN_EXPOSURE",
    "QUALITY_DEFECT_CHARGEBACK": _OUT_OF_SCOPE,
    "NON_CONFORMANCE_COST_RECOVERY": _OUT_OF_SCOPE,
    "DEFECT_RECTIFICATION_COST_SHIFT": _OUT_OF_SCOPE,
    "RECALL_COST_RECOVERY": _OUT_OF_SCOPE,
    "PRICE_PARITY_CLAWBACK": _OUT_OF_SCOPE,
    "LATE_PAYMENT_INTEREST": _OUT_OF_SCOPE,
    "EARLY_PAYMENT_DISCOUNT": _OUT_OF_SCOPE,
    "AUDIT_FINDING_PENALTY": _OUT_OF_SCOPE,
    "UNMAPPED": _OUT_OF_SCOPE,
}


@dataclass(frozen=True)
class MatchedExcerpt:
    """The result of locating one screened excerpt in the source contract."""

    text: str
    start_offset: int
    end_offset: int
    match_kind: MatchKind


@dataclass(frozen=True)
class PoScopeDecision:
    """The PO-scope decision and the trigger dimensions that support it."""

    in_scope: bool
    shortage: bool
    delay: bool
    reason: str


def match_excerpt(source_text: str, excerpt: str, unit: ScreeningUnit) -> MatchedExcerpt:
    """Locate an excerpt in the source, falling back to the screening unit.

    An exact match is preferred, followed by whitespace-normalized matching.
    If neither succeeds, the screening unit is returned rather than dropping
    the clause, preserving recall at the cost of weaker positional evidence.
    """
    excerpt = (excerpt or "").strip()
    if not excerpt:
        return MatchedExcerpt(unit.text, unit.start_offset, unit.end_offset, "UNIT_FALLBACK")

    exact_pos = source_text.find(excerpt)
    if exact_pos != -1:
        return MatchedExcerpt(excerpt, exact_pos, exact_pos + len(excerpt), "EXACT")

    norm_source, offsets = _normalize_with_offsets(source_text)
    norm_excerpt, _ = _normalize_with_offsets(excerpt)
    norm_pos = norm_source.find(norm_excerpt) if norm_excerpt else -1
    if norm_pos != -1:
        start = offsets[norm_pos]
        end = offsets[min(norm_pos + len(norm_excerpt) - 1, len(offsets) - 1)] + 1
        return MatchedExcerpt(source_text[start:end], start, end, "NORMALIZED")

    return MatchedExcerpt(unit.text, unit.start_offset, unit.end_offset, "UNIT_FALLBACK")


def deduplicate_overlaps(matches: list[MatchedExcerpt]) -> list[MatchedExcerpt]:
    """Collapse overlapping source spans, keeping the widest match.

    Screening units intentionally overlap, so a boundary clause may be matched
    more than once. Only matches with positional evidence participate in
    deduplication; UNIT_FALLBACK matches are retained independently because
    their offsets identify the screening unit rather than a confirmed source span.
    """
    located = sorted(
        (m for m in matches if m.match_kind != "UNIT_FALLBACK"),
        key=lambda m: (m.start_offset, -(m.end_offset - m.start_offset)),
    )
    kept: list[MatchedExcerpt] = []
    for m in located:
        if kept and m.start_offset < kept[-1].end_offset:
            if (m.end_offset - m.start_offset) > (kept[-1].end_offset - kept[-1].start_offset):
                kept[-1] = m
            continue
        kept.append(m)

    fallback = [m for m in matches if m.match_kind == "UNIT_FALLBACK"]
    return kept + fallback


def decide_po_scope(penalty_category: str, po_shortage_flag: bool, po_delay_flag: bool) -> PoScopeDecision:
    """Determine whether a penalty belongs in a PO-scoped run.

    CATEGORY_SCOPE governs whether the category is PO-scoped. The classifier's
    shortage and delay flags are preserved, but category defaults can only raise
    those flags, never lower them, preventing under-reporting of governed scope.
    """
    role = CATEGORY_SCOPE.get(penalty_category, _OUT_OF_SCOPE) if penalty_category else _OUT_OF_SCOPE
    defaults = CategoryDefaults.for_category(penalty_category) or {}
    shortage = bool(po_shortage_flag) or bool(defaults.get("po_shortage_flag"))
    delay = bool(po_delay_flag) or bool(defaults.get("po_delay_flag"))
    return PoScopeDecision(
        in_scope=role != _OUT_OF_SCOPE,
        shortage=shortage,
        delay=delay,
        reason=f"category {penalty_category!r} has scope role {role}",
    )


def _normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace while preserving offsets into the original text."""
    out: list[str] = []
    offsets: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            offsets.append(i)
            prev_space = True
        else:
            out.append(ch)
            offsets.append(i)
            prev_space = False
    return "".join(out), offsets
