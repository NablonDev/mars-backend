"""Section-aware contract splitting for the extraction screening stage.

Screening asks the model to enumerate every penalty clause in whatever text it is
given, and recall on "find ALL of X" degrades as the candidate pool grows: each
screening unit carries exactly one clause-bearing section, never a bundle of them. A
genuinely oversized section still splits at block boundaries, never inside a table.
"""

import itertools
import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# A section past this many characters is split at block boundaries, not screened as one
# call. Whole blocks are repeated into each continuation part so a split clause is
# intact in at least one part.
_MAX_UNIT_CHARS = 6_000
_OVERLAP_BLOCKS = 1

# Trigger phrases for the penalty categories a bundled clause most plausibly mixes,
# restricted to the categories `clause_matching.CATEGORY_SCOPE` actually prices
# (TRIGGER_SHORTAGE/TRIGGER_BOTH/TRIGGER_DELAY/TRIGGER_QUANTITY roles). Categories with no
# safely distinguishing phrase (an escape value, a generic liability-cap clause) are
# deliberately left out rather than guessed at.
_CATEGORY_TRIGGER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SHORT_SHIP": ("short-shipment", "short shipment", "short-ship"),
    "MINIMUM_VOLUME_SHORTFALL": (
        "minimum volume commitment",
        "annual volume commitment",
        "purchase volume shortfall",
    ),
    "OTIF_LATE": ("late delivery", "on-time-in-full"),
    "DELIVERY_WINDOW_VIOLATION": ("early delivery", "delivery window"),
    "DELIVERY_ACCEPTANCE_COST_SHIFT": ("delivery acceptance", "acceptance delay"),
    "ALTERNATE_SOURCING_MARKUP": ("cover purchase", "alternate sourcing"),
    "STORAGE_DURATION_FEE": ("storage fee", "warehousing fee", "demurrage"),
    "OVERAGE_CHARGEBACK": ("over-delivery", "over-shipment", "overage charge"),
}

# The floor on how much of a clause its deduplicated split windows must collectively
# cover for `split_bundled_clause` to trust the split; below this, it returns no split.
_MIN_SPLIT_COVERAGE = 0.3


@dataclass(frozen=True)
class ScreeningUnit:
    """One screening LLM call's worth of contract text, from exactly one section."""

    index: int
    section_path: str
    text: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class _Section:
    """One heading and everything under it, up to the next heading of any level."""

    level: int
    heading: str
    breadcrumb: str
    text: str
    start: int
    end: int


def split_into_screening_units(markdown_text: str) -> list[ScreeningUnit]:
    """Split contract markdown into screening units, one per clause-bearing section.

    A section larger than `_MAX_UNIT_CHARS` splits into several units at paragraph or
    table boundaries; every other section, however small, is its own unit.
    """
    units: list[ScreeningUnit] = []
    for section in _parse_sections(markdown_text):
        if len(section.text) <= _MAX_UNIT_CHARS:
            units.append(
                ScreeningUnit(
                    index=0,
                    section_path=section.breadcrumb,
                    text=section.text,
                    start_offset=section.start,
                    end_offset=section.end,
                )
            )
        else:
            units.extend(_split_oversized_section(section))

    return [
        ScreeningUnit(i, u.section_path, u.text, u.start_offset, u.end_offset) for i, u in enumerate(units)
    ]


def split_bundled_clause(clause_text: str) -> list[str]:
    """Split a clause into per-category sub-excerpts when it bundles more than one remedy.

    Returns an empty list, meaning "do not split", when the text carries a trigger
    keyword for at most one distinct penalty category (the common case, left untouched),
    or when the candidate windows collectively cover too little of the clause to trust:
    the caller then keeps processing the whole clause instead of losing it to a bad split.
    """
    hits = _category_hits_by_line(clause_text)
    if len({category for category, _position in hits}) < 2:
        return []
    windows = [_block_window(clause_text, position) for _category, position in hits]
    deduplicated = _drop_duplicate_windows(windows)
    if len(deduplicated) < 2 or not _looks_complete(clause_text, deduplicated):
        return []
    return deduplicated


def _fenced_line_spans(text: str) -> list[tuple[int, int]]:
    """Character spans covered by ``` / ~~~ fenced blocks."""
    spans: list[tuple[int, int]] = []
    open_at: int | None = None
    pos = 0
    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            if open_at is None:
                open_at = pos
            else:
                spans.append((open_at, pos + len(line)))
                open_at = None
        pos += len(line)
    if open_at is not None:
        spans.append((open_at, len(text)))
    return spans


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _parse_sections(text: str) -> list[_Section]:
    """Split markdown into its heading-delimited sections.

    Headings inside fenced code blocks are ignored. Any preamble before the first real
    heading becomes its own section, so no text is ever dropped.
    """
    fenced = _fenced_line_spans(text)
    matches = [m for m in _HEADING_RE.finditer(text) if not _in_spans(m.start(), fenced)]

    if not matches:
        return [_Section(0, "(untitled document)", "(untitled document)", text, 0, len(text))]

    sections: list[_Section] = []
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()]
        if preamble.strip():
            sections.append(_Section(0, "(preamble)", "(preamble)", preamble, 0, matches[0].start()))

    ancestry: list[tuple[int, str]] = []  # (level, heading) stack
    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        while ancestry and ancestry[-1][0] >= level:
            ancestry.pop()
        breadcrumb = " > ".join([h for _lvl, h in ancestry] + [heading])
        ancestry.append((level, heading))

        sections.append(_Section(level, heading, breadcrumb, text[start:end], start, end))

    return sections


def _split_into_blocks(text: str) -> list[str]:
    """Split text into atomic blocks that must never be cut apart.

    A markdown table (a run of consecutive `|`-leading lines, plus the line immediately
    above it, often its caption) is one block, as is a fenced code block; otherwise
    blocks are blank-line-separated paragraphs.
    """
    lines = text.splitlines(keepends=True)
    fenced = _fenced_line_spans(text)
    starts = list(itertools.accumulate((len(line) for line in lines), initial=0))

    blocks: list[str] = []
    buf: list[str] = []
    i = 0

    def flush() -> None:
        if buf:
            blocks.append("".join(buf))
            buf.clear()

    while i < len(lines):
        if _in_spans(starts[i], fenced):
            flush()
            start = i
            while i < len(lines) and _in_spans(starts[i], fenced):
                i += 1
            blocks.append("".join(lines[start:i]))
            continue

        line = lines[i]
        if line.lstrip().startswith("|"):
            # Pull the preceding non-blank line in with the table as its lead-in.
            lead = [buf.pop()] if buf and buf[-1].strip() else []
            flush()
            table = lead
            while i < len(lines) and (lines[i].lstrip().startswith("|") or not lines[i].strip()):
                if not lines[i].strip() and not (
                    i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|")
                ):
                    break
                table.append(lines[i])
                i += 1
            blocks.append("".join(table))
            continue

        if not line.strip():
            buf.append(line)
            if i + 1 < len(lines) and lines[i + 1].strip():
                flush()
            i += 1
            continue

        buf.append(line)
        i += 1

    flush()
    return [b for b in blocks if b.strip()]


def _split_oversized_section(section: _Section) -> list[ScreeningUnit]:
    """Split one section bigger than `_MAX_UNIT_CHARS` at block boundaries.

    Never splits mid-table or mid-paragraph, and carries `_OVERLAP_BLOCKS` whole blocks
    forward into each continuation part.
    """
    blocks = _split_into_blocks(section.text)
    if len(blocks) <= 1:
        return [ScreeningUnit(0, section.breadcrumb, section.text, section.start, section.end)]

    groups: list[list[str]] = []
    current: list[str] = []
    for block in blocks:
        if current and sum(len(b) for b in current) + len(block) > _MAX_UNIT_CHARS:
            groups.append(current)
            current = current[-_OVERLAP_BLOCKS:] if _OVERLAP_BLOCKS else []
        current.append(block)
    if current:
        groups.append(current)

    total = len(groups)
    out = []
    for n, group in enumerate(groups, start=1):
        body = "".join(group)
        if n > 1:
            # Repeat the heading on every part so a continuation is never anonymous.
            body = (
                f"{'#' * max(section.level, 1)} {section.heading} (continued, part {n} of {total})\n\n{body}"
            )
        out.append(ScreeningUnit(0, section.breadcrumb, body, section.start, section.end))
    return out


def _category_hits_by_line(text: str) -> list[tuple[str, int]]:
    """Every (category, offset) hit, detected one physical line at a time.

    Scanning line by line, rather than taking one global first-hit position per
    category, means an incidental keyword mention on an earlier, unrelated line can
    never mask a distinct category's real hit on a later line: both are recorded, and
    `split_bundled_clause` windows and deduplicates each independently. Matches against
    `text` directly with `re.IGNORECASE`, never a lowered copy, so a hit's offset is
    always valid against `text` even where casefolding is not length-preserving.
    """
    hits: list[tuple[str, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        for category, phrases in _CATEGORY_TRIGGER_KEYWORDS.items():
            match = next(
                (m for phrase in phrases if (m := re.search(re.escape(phrase), line, re.IGNORECASE))), None
            )
            if match:
                hits.append((category, offset + match.start()))
        offset += len(line)
    return hits


def _block_window(text: str, position: int) -> str:
    """The stripped text of the blank-line-delimited block containing offset `position`.

    A block, not a single line, so a sentence hard-wrapped across lines with no blank
    line between them (routine in a PDF-extracted contract) is never truncated.
    """
    cursor = 0
    for block in _split_into_blocks(text):
        start = text.find(block, cursor)
        end = start + len(block)
        if start <= position < end:
            return block.strip()
        cursor = end
    return text.strip()


def _drop_duplicate_windows(windows: list[str]) -> list[str]:
    """Collapse windows with identical text, keeping the first, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for window in windows:
        if window in seen:
            continue
        seen.add(window)
        out.append(window)
    return out


def _looks_complete(clause_text: str, windows: list[str]) -> bool:
    """Whether the deduplicated windows cover enough of `clause_text` to trust the split."""
    total = len(clause_text.strip())
    covered = sum(len(w) for w in windows)
    return total == 0 or covered / total >= _MIN_SPLIT_COVERAGE
