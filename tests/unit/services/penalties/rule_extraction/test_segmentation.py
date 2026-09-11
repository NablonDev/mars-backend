"""Tests for `app.services.penalties.rule_extraction.segmentation`.

Zero DB dependency: pure text splitting.
"""

from app.services.penalties.rule_extraction.segmentation import split_into_screening_units


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
