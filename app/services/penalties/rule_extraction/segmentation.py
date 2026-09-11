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
