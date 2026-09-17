"""Tests for `app.services.penalties.rule_extraction.clause_matching`.

Zero DB dependency: pure text matching.
"""

from app.services.penalties.rule_extraction.clause_matching import (
    MatchedExcerpt,
    deduplicate_overlaps,
    match_excerpt,
)
from app.services.penalties.rule_extraction.segmentation import ScreeningUnit

_SOURCE = (
    "Section 4.2 Penalties\n\n"
    "The vendor shall pay a $50 fee per short-shipped case, as described in Schedule B."
)
_UNIT = ScreeningUnit(
    index=0, section_path="Section 4.2 Penalties", text=_SOURCE, start_offset=0, end_offset=len(_SOURCE)
)


def test_match_excerpt_finds_an_exact_hit():
    excerpt = "The vendor shall pay a $50 fee per short-shipped case"

    matched = match_excerpt(_SOURCE, excerpt, _UNIT)

    assert matched.match_kind == "EXACT"
    assert matched.text == excerpt
    assert _SOURCE[matched.start_offset : matched.end_offset] == excerpt


def test_match_excerpt_recovers_a_whitespace_normalized_hit():
    # Real text, but re-wrapped with different whitespace: not an exact substring, but a
    # verbatim match once whitespace is collapsed.
    excerpt = "The vendor shall   pay a $50 fee\nper short-shipped case"

    matched = match_excerpt(_SOURCE, excerpt, _UNIT)

    assert matched.match_kind == "NORMALIZED"
    assert matched.text == "The vendor shall pay a $50 fee per short-shipped case"


def test_match_excerpt_falls_back_to_the_unit_for_a_fabricated_excerpt():
    excerpt = "Completely unrelated text that never appears in this contract at all."

    matched = match_excerpt(_SOURCE, excerpt, _UNIT)

    assert matched.match_kind == "UNIT_FALLBACK"
    assert matched.text == _UNIT.text
    assert matched.start_offset == _UNIT.start_offset
    assert matched.end_offset == _UNIT.end_offset


def test_match_excerpt_falls_back_for_a_blank_excerpt():
    matched = match_excerpt(_SOURCE, "   ", _UNIT)

    assert matched.match_kind == "UNIT_FALLBACK"


def test_deduplicate_overlaps_keeps_the_widest_of_two_overlapping_matches():
    narrow = MatchedExcerpt(text="b", start_offset=10, end_offset=20, match_kind="EXACT")
    wide = MatchedExcerpt(text="a", start_offset=5, end_offset=25, match_kind="EXACT")

    deduplicated = deduplicate_overlaps([narrow, wide])

    assert deduplicated == [wide]


def test_deduplicate_overlaps_never_merges_unit_fallback_matches():
    first = MatchedExcerpt(text="unit", start_offset=0, end_offset=100, match_kind="UNIT_FALLBACK")
    second = MatchedExcerpt(text="unit", start_offset=0, end_offset=100, match_kind="UNIT_FALLBACK")

    deduplicated = deduplicate_overlaps([first, second])

    assert deduplicated == [first, second]


def test_deduplicate_overlaps_keeps_non_overlapping_matches_separate():
    first = MatchedExcerpt(text="a", start_offset=0, end_offset=10, match_kind="EXACT")
    second = MatchedExcerpt(text="b", start_offset=20, end_offset=30, match_kind="EXACT")

    deduplicated = deduplicate_overlaps([first, second])

    assert deduplicated == [first, second]
