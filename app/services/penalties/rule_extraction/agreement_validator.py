"""Validation of contract markdown against canonical agreement specification.

Enforces deterministic heading levels, single document title, proper sub-clause
nesting, and preservation of lettered list items.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationError:
    """A violation of the canonical agreement markdown specification."""

    line_number: int
    rule: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating an agreement markdown document."""

    is_valid: bool
    errors: list[ValidationError]
    warnings: list[ValidationError]


def _get_numbering_level(num_str: str, in_schedule_or_exhibit: bool = False) -> int:
    """Determine expected heading level from numbering string."""
    clean = num_str.strip().rstrip(".:")
    prefix_match = re.match(r"^(?:Section|Article|ARTICLE)\s+([0-9IVXLCDMivxlcdm\.]+)", clean, re.IGNORECASE)
    if prefix_match:
        val = prefix_match.group(1)
        parts = [p for p in val.split(".") if p]
        if len(parts) <= 1:
            return 3 if in_schedule_or_exhibit else 2
        elif len(parts) == 2:
            return 3
        else:
            return 4

    if re.match(r"^[IVXLCDM]+$", clean, re.IGNORECASE):
        return 3 if in_schedule_or_exhibit else 2

    parts = [p for p in re.split(r"[\.-]", clean) if p]
    if len(parts) == 1:
        return 3 if in_schedule_or_exhibit else 2
    elif len(parts) == 2:
        return 3
    elif len(parts) >= 3:
        return 4
    return 2


def _is_lettered_item(line: str) -> bool:
    """Check if line is a lettered item like (a) or **(a)** or - (a)."""
    m = re.match(
        r"^([ \t]*)(?:[-*]\s*)?(?:\*\*)?\(?([a-z]|[ivxlcdm]{1,4})\)[\.\)]?(?:\*\*)?[ \t]+(.*)$",
        line,
    )
    return bool(m and not line.strip().startswith("#"))


def _is_promotable_line(line: str, in_schedule_or_exhibit: bool = False) -> bool:
    """Check if line starts with an unpromoted bold-numbered or numbered clause."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False

    if _is_lettered_item(line):
        return False

    if stripped.startswith(("|", "---", ">")):
        return False

    if re.match(
        r"^\*\*(?:WHEREAS|NOW,\s*THEREFORE|IN\s+WITNESS\s+WHEREOF|Parties|Effective Date|Source|Filer|Filing|Exhibit|URL|IT\s+IS\s+AGREED|THE\s+KROGER\s+CO|MARS\s+PETCARE|SIGNED\s+for)[^*]*\*\*",
        stripped,
        re.IGNORECASE,
    ):
        return False

    # Bold number + bold title: e.g. **1.1 Defined Terms.**
    if re.match(
        r"^\*\*(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\s+(?P<title>[^*]+?)\.?\*\*",
        stripped,
    ):
        return True

    # Bold number with non-bold title: e.g. **4.D** Warehousing.
    if re.match(
        r"^\*\*(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\*\*\s+[A-Z][A-Za-z0-9\s,\/’\x27\(\)\-–—;]{1,80}\.\s+[A-Z“\"\[]",
        stripped,
    ):
        return True

    # Bold subclause number alone: e.g. **1.1** ... or **21.3.1** ... or **3.A** ...
    if re.match(
        r"^\*\*(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\*\*\s*",
        stripped,
    ):
        return True

    # Numbered run-in lines with bold title: e.g. 2.1. **Supply of Products.**
    if re.match(
        r"^(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\.?\s+\*\*[^*]+?\.\*\*",
        stripped,
    ):
        return True

    # Schedule / Amendment item: e.g. **1. COLD DRINK EQUIPMENT COMMITMENT**
    return bool(
        in_schedule_or_exhibit
        and re.match(
            r"^\*\*\d+\.\s*[^*]+?\.?\*\*(?:\s*[—–-]\s*|\s+)",
            stripped,
        )
    )


def validate_agreement_markdown(text: str) -> ValidationResult:
    """Validate agreement markdown against canonical specification.

    Rules:
    - DOC_TITLE_SINGLE_H1: Exactly one `#` document title at the top of the agreement.
    - HEADING_LEVEL_MISMATCH: Headings must conform to numbering (N. -> ##, N.N -> ###, N.N.N -> ####).
    - LETTERED_ITEMS_ARE_LISTS: Lettered items ((a), (b)) must be list items, never headings.
    - UNPROMOTED_BOLD_NUMBER: Numbered sub-clauses must be markdown headings, not bold paragraphs.
    - HEADING_EMPTY_TEXT: Headings must have non-empty text.
    """
    lines = text.splitlines()
    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []

    h1_count = 0
    in_fenced = False
    in_schedule_or_exhibit = False

    for idx, line in enumerate(lines):
        line_num = idx + 1
        stripped = line.strip()

        if re.match(r"^\s*(```|~~~)", line):
            in_fenced = not in_fenced
            continue
        if in_fenced:
            continue

        # Check H1
        if stripped.startswith("# "):
            h1_count += 1
            if h1_count > 1:
                errors.append(
                    ValidationError(
                        line_number=line_num,
                        rule="DOC_TITLE_SINGLE_H1",
                        message=f"Multiple Level 1 (#) headings found: '{stripped[:60]}'. Only the document title may be Level 1.",
                    )
                )

        # Check heading level vs numbering
        m_head = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if m_head:
            hashes, heading_text = m_head.groups()
            lvl = len(hashes)
            head_clean = heading_text.strip()

            if not head_clean:
                errors.append(
                    ValidationError(
                        line_number=line_num,
                        rule="HEADING_EMPTY_TEXT",
                        message=f"Heading on line {line_num} has no text.",
                    )
                )
                continue

            if re.match(r"^(?:Schedule|Exhibit|Amendment|Schedules)\b", head_clean, re.IGNORECASE):
                in_schedule_or_exhibit = True
            elif (
                re.match(r"^(?:Section|Article|\d+\.)", head_clean, re.IGNORECASE)
                and not in_schedule_or_exhibit
            ):
                in_schedule_or_exhibit = False

            # Check for lettered items as headings
            if re.match(r"^\(?[a-z]\)[\.\)]?(?:\s|$)", head_clean, re.IGNORECASE):
                errors.append(
                    ValidationError(
                        line_number=line_num,
                        rule="LETTERED_ITEMS_ARE_LISTS",
                        message=f"Lettered item formatted as heading: '{line}'. Lettered items must be list items (- (a) ...).",
                    )
                )

            # Check numbering level
            m_num = re.match(
                r"^(?:(Section|Article|ARTICLE)\s+)?(\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+|\d+\.?)\b",
                head_clean,
                re.IGNORECASE,
            )
            if m_num:
                _prefix, num_part = m_num.groups()
                expected_lvl = _get_numbering_level(num_part, in_schedule_or_exhibit)
                if lvl != expected_lvl:
                    errors.append(
                        ValidationError(
                            line_number=line_num,
                            rule="HEADING_LEVEL_MISMATCH",
                            message=(
                                f"Heading '{head_clean[:50]}' is Level {lvl} ({'#' * lvl}), "
                                f"expected Level {expected_lvl} ({'#' * expected_lvl}) based on numbering '{num_part}'."
                            ),
                        )
                    )

        # Check for unpromoted bold-numbered lines
        if _is_promotable_line(line, in_schedule_or_exhibit):
            errors.append(
                ValidationError(
                    line_number=line_num,
                    rule="UNPROMOTED_BOLD_NUMBER",
                    message=f"Line starts with numbered clause that should be promoted to a heading: '{stripped[:60]}'.",
                )
            )

    if h1_count == 0:
        errors.append(
            ValidationError(
                line_number=1,
                rule="DOC_TITLE_SINGLE_H1",
                message="No Level 1 (#) document title found in agreement markdown.",
            )
        )

    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )
