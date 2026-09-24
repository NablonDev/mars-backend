"""Tests for `app.services.penalties.rule_extraction.segmentation`.

Zero DB dependency: pure text splitting.
"""

from app.services.penalties.rule_extraction import segmentation
from app.services.penalties.rule_extraction.segmentation import (
    split_bundled_clause,
    split_into_screening_units,
)
from app.services.penalties.rule_extraction.vocabulary import PENALTY_CATEGORIES

# Modeled on ground-truth clause GT-04 (`data/rule_extraction_ground_truth/contract_3.yaml`,
# `contract_intelligence/Synthetic_Contracts/contract_3_full_text_gemini.md` section 3.4): one
# "OTIF Penalties" clause bundling three distinct charge types under lettered sub-items, each
# its own blank-line-delimited block, matching how a PDF-extracted contract renders distinct
# list items in markdown.
_BUNDLED_CLAUSE = (
    "3.4. OTIF Penalties. In the event Vendor fails to meet the delivery requirements "
    "specified in the P.O., Purchaser shall automatically assess, and Vendor agrees to "
    "pay, the following chargebacks via invoice deduction:\n"
    "\n"
    "(a) Late Delivery: For any Products delivered after the confirmed P.O. delivery "
    "window, Vendor shall be subject to a late delivery penalty equal to four percent "
    "(4.0%) of the total Cost of Goods Sold (COGS) of the delayed Products for each "
    "calendar day the delivery is delayed, capped at a maximum of twenty percent (20.0%).\n"
    "\n"
    "(b) Shortages: For any Order where the unit volume delivered is less than ninety-five "
    "percent (95.0%) of the ordered unit volume, Purchaser shall assess a short-shipment "
    "penalty of seven percent (7.0%) on the total invoice value of the missing merchandise.\n"
    "\n"
    "(c) Early Delivery: Products delivered more than three (3) business days prior to the "
    "confirmed delivery window may be refused or subject to an early delivery warehousing "
    "fee of $150.00 per pallet per day.\n"
)


def test_two_adjacent_penalty_sections_produce_two_units_not_one():
    # Regression: the prior version bundled up to three sections into one screening
    # unit, which lost a clause when the classifier emitted one rule for the whole
    # block. Each section, however small, must be its own unit.
    text = (
        "# 3.4 Short Shipment Penalty\nSupplier pays $50 per short-shipped case.\n\n"
        "# 3.5 Late Delivery Penalty\nSupplier pays 1% of PO value per day late.\n"
    )

    units = split_into_screening_units(text)

    assert len(units) == 2
    assert "Short Shipment Penalty" in units[0].section_path
    assert "Late Delivery Penalty" in units[1].section_path
    assert units[0].index == 0
    assert units[1].index == 1


def test_every_section_is_its_own_unit_regardless_of_heading_depth():
    text = (
        "# Section One\nShort content one.\n\n"
        "# Section Two\nShort content two.\n\n"
        "# Section Three\nShort content three.\n\n"
        "# Section Four\nShort content four."
    )

    units = split_into_screening_units(text)

    assert len(units) == 4
    assert [u.index for u in units] == [0, 1, 2, 3]


def test_no_headings_produces_a_single_untitled_unit():
    text = "Just body text, no markdown heading anywhere in this contract excerpt."

    units = split_into_screening_units(text)

    assert len(units) == 1
    assert units[0].section_path == "(untitled document)"
    assert units[0].text == text


def test_preamble_before_the_first_heading_is_its_own_unit():
    text = "Some introductory recital text.\n\n# 1. Definitions\nBody.\n"

    units = split_into_screening_units(text)

    assert len(units) == 2
    assert units[0].section_path == "(preamble)"
    assert units[1].section_path == "1. Definitions"


def test_nested_headings_carry_a_breadcrumb():
    text = "# 3. Delivery\nIntro.\n\n## 3.4 Penalties\nSupplier pays a fee.\n"

    units = split_into_screening_units(text)

    assert units[-1].section_path == "3. Delivery > 3.4 Penalties"


def test_heading_only_sections_are_skipped_leaving_one_unit_per_sub_clause():
    text = "# T\n\n## 4. PERF\n\n### 4.1 A\nbody a\n\n### 4.2 B\nbody b\n"

    units = split_into_screening_units(text)

    assert len(units) == 2
    assert [u.section_path for u in units] == ["T > 4. PERF > 4.1 A", "T > 4. PERF > 4.2 B"]


def test_a_section_with_its_own_body_text_still_yields_a_unit():
    text = "# T\n\n## 4. PERF\nIntroductory paragraph for the article.\n\n### 4.1 A\nbody a\n"

    units = split_into_screening_units(text)

    paths = [u.section_path for u in units]
    assert "T > 4. PERF" in paths
    assert "T > 4. PERF > 4.1 A" in paths


def test_oversized_section_splits_at_block_boundaries_into_multiple_units():
    paragraphs = [
        f"Paragraph {i} pads this clause out to a realistic length for testing." for i in range(100)
    ]
    body = "\n\n".join(paragraphs)
    text = f"# Big Section\n\n{body}"

    units = split_into_screening_units(text)

    assert len(units) > 1
    assert all(u.section_path == "Big Section" for u in units)
    assert [u.index for u in units] == list(range(len(units)))


def test_oversized_section_never_splits_a_table_in_half():
    table = "\n".join(f"| row {i} | value {i} |" for i in range(200))
    filler = "\n\n".join(f"Filler paragraph {i} of reasonable length." for i in range(60))
    text = f"# Big Section\n\n{filler}\n\n{table}\n"

    units = split_into_screening_units(text)

    table_rows = [f"| row {i} | value {i} |" for i in range(200)]
    containing = [u for u in units if table_rows[0] in u.text]
    assert len(containing) == 1
    assert all(row in containing[0].text for row in table_rows)


def test_bundled_clause_with_three_distinct_charge_types_splits_into_three_sub_excerpts():
    # GT-04: one clause with three lettered sub-items (late delivery, shortage, early
    # delivery) collapsed to a single candidate before this fix. Each sub-item must now
    # surface as its own, distinct sub-excerpt.
    sub_excerpts = split_bundled_clause(_BUNDLED_CLAUSE)

    assert len(sub_excerpts) == 3
    # Pinned on the actual clause_text content, not just which categories a downstream
    # FakeLLM happens to return: this would fail if the splitter picked the wrong spans.
    late_delivery = next(e for e in sub_excerpts if "late delivery penalty" in e)
    shortage = next(e for e in sub_excerpts if "short-shipment penalty" in e)
    early_delivery = next(e for e in sub_excerpts if "early delivery warehousing fee" in e)
    assert late_delivery.startswith("(a) Late Delivery:")
    assert "four percent (4.0%)" in late_delivery
    assert shortage.startswith("(b) Shortages:")
    assert "seven percent (7.0%)" in shortage
    assert early_delivery.startswith("(c) Early Delivery:")
    assert "$150.00 per pallet per day" in early_delivery
    # Each sub-excerpt is its own distinct string, so a downstream md5 fingerprint per
    # sub-excerpt can never collide with another.
    assert len(set(sub_excerpts)) == 3


def test_category_trigger_keywords_are_all_real_governed_category_codes():
    # `_CATEGORY_TRIGGER_KEYWORDS`'s category value is discarded and only used for
    # grouping/deduplication, so a typo'd category code would otherwise be silently
    # decorative instead of failing loudly.
    assert set(segmentation._CATEGORY_TRIGGER_KEYWORDS) <= set(PENALTY_CATEGORIES)


def test_an_incidental_keyword_on_one_block_does_not_mask_a_different_later_block():
    # Regression: the old global-first-hit algorithm let "late delivery" mentioned in
    # passing inside the Shortages block win OTIF_LATE's window, so the genuine Late
    # Delivery block below never got its own sub-excerpt and its remedy was lost.
    clause = (
        "Shortages: A short-shipment penalty applies when volume delivered is below "
        "ninety-five percent; this provision is unrelated to any late delivery scenario.\n"
        "\n"
        "Late Delivery: Vendor shall pay a late delivery penalty of four percent (4.0%) per day.\n"
    )

    sub_excerpts = split_bundled_clause(clause)

    assert len(sub_excerpts) == 2
    assert any(e.startswith("Shortages:") for e in sub_excerpts)
    assert any(e.startswith("Late Delivery:") for e in sub_excerpts)


def test_hard_wrapped_clause_widens_the_window_to_the_full_paragraph_not_one_line():
    # Regression: the old one-line window truncated a hard-wrapped sentence, dropping
    # whichever fact landed on the sentence's continuation line.
    clause = (
        "Late Delivery: Vendor shall pay a late delivery penalty equal to the rate stated\n"
        "below, four percent (4.0%) of COGS per day delayed.\n"
        "\n"
        "Shortages: A short-shipment penalty applies to any order missing units, at a rate\n"
        "of seven percent (7.0%) of the invoice value.\n"
    )

    sub_excerpts = split_bundled_clause(clause)

    assert len(sub_excerpts) == 2
    assert any("four percent (4.0%)" in excerpt for excerpt in sub_excerpts)
    assert any("seven percent (7.0%)" in excerpt for excerpt in sub_excerpts)


def test_single_category_clause_is_not_split():
    clause = "Supplier pays a $50 fee per short-shipped case, capped at $5,000 per invoice."

    assert split_bundled_clause(clause) == []


def test_clause_with_no_governed_category_signal_is_not_split():
    clause = "This Agreement is governed by the laws of the State of Delaware."

    assert split_bundled_clause(clause) == []


def test_two_categories_sharing_one_line_collapse_to_a_single_sub_excerpt():
    # "early delivery" (DELIVERY_WINDOW_VIOLATION) and "warehousing fee"
    # (STORAGE_DURATION_FEE) both land on the same line here, so they must not produce
    # two identical drafts (which would trip the run-scoped fingerprint uniqueness
    # constraint downstream). Blank-line-separated from the Late Delivery line above it
    # so each lands in its own block, isolating this from the hard-wrap widening.
    clause = (
        "Late Delivery: subject to a late delivery penalty of 4% of COGS per day.\n"
        "\n"
        "Early Delivery: subject to an early delivery warehousing fee of $150 per pallet.\n"
    )

    sub_excerpts = split_bundled_clause(clause)

    assert len(sub_excerpts) == 2
    assert len(set(sub_excerpts)) == 2
