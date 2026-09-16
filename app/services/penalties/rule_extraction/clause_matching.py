"""Locate screened-clause evidence and determine its engine-family routing.

Excerpt matching verifies that screened evidence can be located in the source
contract. Engine-family routing separately determines which pricing engine (if
any) can price the clause.

The two concerns are intentionally kept separate:
- excerpt matching operates on source-text evidence;
- engine-family routing operates on the classifier's category and PO flags.
"""

from dataclasses import dataclass
from typing import Literal

from app.services.penalties.rule_extraction.segmentation import ScreeningUnit
from app.services.penalties.rule_extraction.vocabulary import CategoryDefaults

MatchKind = Literal["EXACT", "NORMALIZED", "UNIT_FALLBACK"]

_UNPRICEABLE = "UNPRICEABLE"


@dataclass(frozen=True)
class MatchedExcerpt:
    """The result of locating one screened excerpt in the source contract."""

    text: str
    start_offset: int
    end_offset: int
    match_kind: MatchKind


@dataclass(frozen=True)
class PoScopeDecision:
    """The engine-family routing decision and the reporting flags that support it.

    `in_scope` means "some engine can price this" (`engine_family != UNPRICEABLE`), not
    the old PO-scope test; `shortage`/`delay` stay reporting-only flags for a KPI query,
    independent of whether the category is actually priceable.
    """

    in_scope: bool
    shortage: bool
    delay: bool
    engine_family: str
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
    """Determine which engine family (if any) can price a penalty, and its reporting flags.

    `CategoryDefaults.engine_family_for` governs whether the category is priceable at all.
    The classifier's shortage and delay flags are preserved, but category defaults can only
    raise those flags, never lower them, preventing under-reporting of governed scope.
    """
    family = CategoryDefaults.engine_family_for(penalty_category)
    defaults = CategoryDefaults.for_category(penalty_category) or {}
    shortage = bool(po_shortage_flag) or bool(defaults.get("po_shortage_flag"))
    delay = bool(po_delay_flag) or bool(defaults.get("po_delay_flag"))
    return PoScopeDecision(
        in_scope=family != _UNPRICEABLE,
        shortage=shortage,
        delay=delay,
        engine_family=family,
        reason=f"category {penalty_category!r} has engine_family {family}",
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
