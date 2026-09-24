"""Deterministic normalization of contract markdown to canonical agreement format.

Re-levels headings from clause numbering (N. -> ##, N.N -> ###, N.N.N -> ####),
promotes bold-numbered and numbered run-in clauses to headings, and normalizes
lettered items into standard markdown list items.
"""

from __future__ import annotations

import re


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


def _is_lettered_item(line: str) -> tuple[str, str, str] | None:
    """Check if line is a lettered item like (a) or **(a)** or - (a).
    Returns (indent, letter, rest) or None.
    """
    m = re.match(
        r"^([ \t]*)(?:[-*]\s*)?(?:\*\*)?\(?([a-z]|[ivxlcdm]{1,4})\)[\.\)]?(?:\*\*)?[ \t]+(.*)$",
        line,
    )
    if m and not line.strip().startswith("#"):
        indent, letter, rest = m.groups()
        return indent, letter, rest
    return None


def _extract_promotable_line(line: str, in_schedule_or_exhibit: bool = False) -> tuple[int, str, str] | None:
    """Check if line is a bold-numbered or numbered sub-clause line to promote to heading.
    Returns (level, heading_text, body_text) or None.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    if _is_lettered_item(line):
        return None

    if stripped.startswith(("|", "---", ">")):
        return None

    if re.match(
        r"^\*\*(?:WHEREAS|NOW,\s*THEREFORE|IN\s+WITNESS\s+WHEREOF|Parties|Effective Date|Source|Filer|Filing|Exhibit|URL|IT\s+IS\s+AGREED|THE\s+KROGER\s+CO|MARS\s+PETCARE|SIGNED\s+for)[^*]*\*\*",
        stripped,
        re.IGNORECASE,
    ):
        return None

    # Pattern 1: Bold number + bold title:
    # e.g. **1.1 Defined Terms.** As used in this Agreement...
    # e.g. **4.2 Late Delivery Non-Compliance Penalty.**
    # e.g. **2.2 EDI Purchase Order Acknowledgment (EDI 855).** Vendor shall...
    # e.g. **4.2 Rescheduled Delivery Fee (>24 Hours).** If an out-of-window...
    m1 = re.match(
        r"^\*\*(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\s+(?P<title>[^*]+?)\.?\*\*\s*(?P<rest>.*)$",
        stripped,
    )
    if m1:
        num = m1.group("num")
        title = m1.group("title").strip().rstrip(".")
        rest = m1.group("rest").strip()
        lvl = _get_numbering_level(num, in_schedule_or_exhibit)
        return lvl, f"{num} {title}", rest

    # Pattern 2: Bold number with non-bold title:
    # e.g. **4.D** Warehousing. Supplier will warehouse...
    # e.g. **5.A** Initial Prices. Subject to...
    m2 = re.match(
        r"^\*\*(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\*\*\s+(?P<title>[A-Z][A-Za-z0-9\s,\/’\x27\(\)\-–—;]+?)\.\s+(?P<rest>[A-Z“\"\[].*)$",
        stripped,
    )
    if m2:
        num = m2.group("num")
        title = m2.group("title").strip().rstrip(".")
        if len(title) <= 80:
            rest = m2.group("rest").strip()
            lvl = _get_numbering_level(num, in_schedule_or_exhibit)
            return lvl, f"{num} {title}", rest

    # Pattern 3: Bold number alone (no title):
    # e.g. **1.1** In this Agreement, the following words...
    # e.g. **21.3.1** such change including any termination...
    # e.g. **3.A** Supplier shall supply...
    m3 = re.match(
        r"^\*\*(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\*\*\s*(?P<rest>.*)$",
        stripped,
    )
    if m3:
        num = m3.group("num")
        rest = m3.group("rest").strip()
        lvl = _get_numbering_level(num, in_schedule_or_exhibit)
        return lvl, num, rest

    # Pattern 4: Numbered run-in lines with bold title:
    # e.g. 2.1. **Supply of Products.** Vendor shall manufacture...
    # e.g. 1.1. **"Applicable Laws"** means all federal...
    m4 = re.match(
        r"^(?P<num>\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+)\.?\s+\*\*(?P<title>[^*]+?)\.?\*\*\s*(?P<rest>.*)$",
        stripped,
    )
    if m4:
        num = m4.group("num").rstrip(".")
        title = m4.group("title").strip().rstrip(".")
        rest = m4.group("rest").strip()
        lvl = _get_numbering_level(num, in_schedule_or_exhibit)
        return lvl, f"{num} {title}", rest

    # Pattern 5: Definition in quotes:
    # e.g. 1.1 “Animal Health Care” means the promotion...
    m5 = re.match(
        r"^(?P<num>\d+\.\d+)\s+[“\"](?P<title>[^”\"]+?)[”\"]\s+(?P<rest>.*)$",
        stripped,
    )
    if m5:
        num = m5.group("num")
        title = m5.group("title").strip()
        rest = m5.group("rest").strip()
        lvl = _get_numbering_level(num, in_schedule_or_exhibit)
        return lvl, f"{num} “{title}”", rest

    # Pattern 6: Unbolded numbered clauses with title:
    # e.g. 2.1 General. This Agreement establishes...
    # e.g. 2.2 Distribution Rights.
    # e.g. 12.2 Specific Performance; Injunctive Relief. …
    m6 = re.match(
        r"^(?P<num>\d+\.\d+)\s+(?P<title>[A-Z][A-Za-z0-9\s,\/’\x27\(\)\-–—;]+?)\.(?:\s*(?P<rest>.*))?$",
        stripped,
    )
    if m6:
        num = m6.group("num")
        title = m6.group("title").strip().rstrip(".")
        rest = (m6.group("rest") or "").strip()
        if rest in ("…", "..."):
            rest = ""
        if len(title) <= 80:
            lvl = _get_numbering_level(num, in_schedule_or_exhibit)
            return lvl, f"{num} {title}", rest

    # Pattern 7: Bare numbered clauses e.g. 8.1 Oculus warrants...
    m7 = re.match(
        r"^(?P<num>\d+\.\d+)\s+(?P<rest>[A-Z“\"\[].*)$",
        stripped,
    )
    if m7:
        num = m7.group("num")
        rest = m7.group("rest").strip()
        lvl = _get_numbering_level(num, in_schedule_or_exhibit)
        return lvl, num, rest

    # Pattern 8: Schedule / Amendment item with bold title:
    # e.g. **1. COLD DRINK EQUIPMENT COMMITMENT** — In the event...
    # e.g. **1.** Capitalized Terms. Capitalized terms contained...
    if in_schedule_or_exhibit:
        m8 = re.match(
            r"^\*\*(?P<num>\d+)\.\s*(?P<title>[^*]+?)\.?\*\*(?:\s*[—–-]\s*|\s+)(?P<rest>.*)$",
            stripped,
        )
        if m8:
            num = m8.group("num")
            title = m8.group("title").strip().rstrip(".")
            rest = m8.group("rest").strip()
            return 3, f"{num}. {title}", rest

        m8b = re.match(
            r"^\*\*(?P<num>\d+)\.\*\*\s+(?P<title>[A-Z][A-Za-z0-9\s,\/’\x27\(\)\-–—;]+?)\.\s+(?P<rest>[A-Z“\"\[].*)$",
            stripped,
        )
        if m8b:
            num = m8b.group("num")
            title = m8b.group("title").strip().rstrip(".")
            rest = m8b.group("rest").strip()
            return 3, f"{num}. {title}", rest

    return None


def normalize_agreement_markdown(text: str) -> str:
    """Deterministically normalize contract markdown heading hierarchy and structure.

    Key guarantees:
    - Single `#` top-level document title.
    - Articles / sections with single-level numbering (N.) are `##`.
    - Numbered sub-clauses with two-level numbering (N.N, N.Letter) are `###`.
    - Third-level numbering (N.N.N) is `####`.
    - Bold-numbered clause lines are promoted to headings with numbers preserved.
    - Lettered items stay list items (`- (a) …`).
    - The transformation is idempotent: `normalize(normalize(text)) == normalize(text)`.
    """
    lines = text.splitlines()
    output_lines: list[str] = []

    in_fenced = False
    doc_title_found = False
    in_schedule_or_exhibit = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Fenced code block check
        if re.match(r"^\s*(```|~~~)", line):
            in_fenced = not in_fenced
            output_lines.append(line)
            i += 1
            continue

        if in_fenced:
            output_lines.append(line)
            i += 1
            continue

        # 1. Check existing markdown heading
        m_head = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if m_head:
            orig_hashes, head_text = m_head.groups()
            head_clean = head_text.strip()

            # Handle Document Title:
            # If doc title not yet found and line looks like agreement title
            if not doc_title_found and not re.match(
                r"^(?:Section|Article|\d+\.|\d+\b)", head_clean, re.IGNORECASE
            ):
                if head_clean.lower() not in ("contract overview",):
                    doc_title_found = True
                    output_lines.append(f"# {head_clean}")
                    i += 1
                    continue
                else:
                    output_lines.append(f"## {head_clean}")
                    i += 1
                    continue

            # Check if this heading enters or exits schedule/exhibit context
            if re.match(r"^(?:Schedule|Exhibit|Amendment|Schedules)\b", head_clean, re.IGNORECASE):
                in_schedule_or_exhibit = True
            elif (
                re.match(r"^(?:Section|Article|\d+\.)", head_clean, re.IGNORECASE)
                and not in_schedule_or_exhibit
            ):
                in_schedule_or_exhibit = False

            # Check if heading has numbering:
            # e.g. "## 7. Dispute Resolution" or "# 1. Products and Specifications"
            m_num = re.match(
                r"^(?:(Section|Article|ARTICLE)\s+)?(\d+(?:\.\d+)+(?:[A-Za-z])?|\d+\.[A-Za-z]|[A-Za-z]\.\d+|\d+\.?|[IVXLCDM]+\.?)\b(.*)$",
                head_clean,
                re.IGNORECASE,
            )
            if m_num:
                _prefix, num_part, _remainder = m_num.groups()
                lvl = _get_numbering_level(num_part, in_schedule_or_exhibit)
                hashes = "#" * lvl
                output_lines.append(f"{hashes} {head_clean}")
                i += 1
                continue

            # Check schedule / exhibit / major headings:
            # e.g. "## Schedule 1: ...", "## Exhibit A — ...", "## RECITALS", "## Background"
            if re.match(
                r"^(?:Schedule\s+[A-Za-z0-9]|Exhibit\s+[A-Za-z0-9]|RECITALS|Background|Schedules|Signature Block|Retrieval Limitations|Amendment\s+\d+)",
                head_clean,
                re.IGNORECASE,
            ):
                if re.match(r"^Schedule\s+\d+", head_clean, re.IGNORECASE) and "exhibit" in [
                    l.lower() for l in output_lines if l.startswith("##")
                ]:
                    output_lines.append(f"### {head_clean}")
                else:
                    output_lines.append(f"## {head_clean}")
                i += 1
                continue

            # Sub-items under schedules/exhibits:
            if len(orig_hashes) >= 3:
                output_lines.append(f"### {head_clean}")
                i += 1
                continue

            # Default fallback for other headings
            output_lines.append(f"## {head_clean}")
            i += 1
            continue

        # 2. Check lettered items (- (a) …)
        letter_match = _is_lettered_item(line)
        if letter_match:
            indent, letter, rest = letter_match
            output_lines.append(f"{indent}- ({letter}) {rest}")
            i += 1
            continue

        # 3. Check bold-numbered lines to promote to headings
        promoted = _extract_promotable_line(line, in_schedule_or_exhibit)
        if promoted:
            lvl, head_title, body = promoted
            hashes = "#" * lvl
            output_lines.append(f"{hashes} {head_title}")
            if body:
                output_lines.append("")
                output_lines.append(body)
            i += 1
            continue

        output_lines.append(line)
        i += 1

    # Second pass: clean up whitespace around headings
    cleaned: list[str] = []
    for l in output_lines:
        l_stripped = l.rstrip()
        if re.match(r"^#{1,6}\s+", l_stripped):
            if cleaned and cleaned[-1] != "" and not cleaned[-1].startswith("#"):
                cleaned.append("")
            cleaned.append(l_stripped)
        else:
            cleaned.append(l_stripped)

    # Collapse multiple blank lines
    result_lines: list[str] = []
    blank_count = 0
    for l in cleaned:
        if l == "":
            blank_count += 1
            if blank_count <= 1:
                result_lines.append(l)
        else:
            blank_count = 0
            result_lines.append(l)

    return "\n".join(result_lines) + "\n"
