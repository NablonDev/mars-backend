"""Tests for agreement markdown specification, normalizer, and validator."""

from __future__ import annotations

from pathlib import Path

from app.services.penalties.rule_extraction.agreement_normalizer import (
    normalize_agreement_markdown,
)
from app.services.penalties.rule_extraction.agreement_validator import (
    validate_agreement_markdown,
)
from app.services.penalties.rule_extraction.segmentation import split_into_screening_units

# ---------------------------------------------------------------------------
# Unit tests: Normalizer
# ---------------------------------------------------------------------------


def test_normalize_promotes_bold_numbered_clause_with_title() -> None:
    raw = (
        "# TEST AGREEMENT\n\n"
        "## 7. DISPUTE RESOLUTION\n\n"
        "**7.1 Dispute Window.** If Vendor disputes any deduction, Vendor must submit a claim.\n"
    )
    normalized = normalize_agreement_markdown(raw)

    assert "### 7.1 Dispute Window" in normalized
    assert "If Vendor disputes any deduction, Vendor must submit a claim." in normalized
    # The number must stay in the heading
    assert "### 7.1 Dispute Window\n\nIf Vendor disputes" in normalized


def test_normalize_promotes_bold_number_without_title() -> None:
    raw = (
        "# SUPPLY CONTRACT\n\n"
        "## 1. Definitions and Interpretation\n\n"
        "**1.1** In this Agreement, the following words and expressions shall apply:\n"
    )
    normalized = normalize_agreement_markdown(raw)

    assert "### 1.1\n\nIn this Agreement, the following words" in normalized


def test_normalize_promotes_run_in_bold_title() -> None:
    raw = (
        "# MASTER SUPPLY AGREEMENT\n\n"
        "## 2. SCOPE OF AGREEMENT\n\n"
        "2.1. **Supply of Products.** Vendor shall manufacture and supply products.\n"
    )
    normalized = normalize_agreement_markdown(raw)

    assert "### 2.1 Supply of Products\n\nVendor shall manufacture and supply products." in normalized


def test_normalize_promotes_third_level_numbering() -> None:
    raw = (
        "# MASTER SUPPLY AGREEMENT\n\n"
        "## 21. Sub-Contracting\n\n"
        "### 21.3 Subcontractor Approval\n\n"
        "**21.3.1** The alternative subcontractor meets all quality standards.\n"
    )
    normalized = normalize_agreement_markdown(raw)

    assert "#### 21.3.1\n\nThe alternative subcontractor meets all quality standards." in normalized


def test_normalize_relevels_heading_drift() -> None:
    # Upstream converter gave top-level sections # instead of ##
    raw = (
        "# MASTER SUPPLY AGREEMENT\n\n"
        "# 1. Products and Specifications\n\n"
        "Buyer agrees to purchase products.\n\n"
        "# 2. Term\n\n"
        "The agreement lasts for 5 years.\n"
    )
    normalized = normalize_agreement_markdown(raw)

    assert normalized.startswith("# MASTER SUPPLY AGREEMENT")
    assert "## 1. Products and Specifications" in normalized
    assert "## 2. Term" in normalized
    # Document title is the only H1
    h1_lines = [l for l in normalized.splitlines() if l.startswith("# ")]
    assert len(h1_lines) == 1


def test_normalize_preserves_lettered_list_items() -> None:
    raw = (
        "# MASTER VENDOR AGREEMENT\n\n"
        "## 3. OTIF Compliance\n\n"
        "### 3.4 Penalties\n\n"
        "Purchaser shall assess the following chargebacks:\n"
        "(a) **Late Delivery:** 4% penalty per day late.\n"
        "**(b)** Shortages: 7% on invoice value.\n"
        "- (c) Early delivery fee of $150.\n"
    )
    normalized = normalize_agreement_markdown(raw)

    assert "- (a) **Late Delivery:** 4% penalty per day late." in normalized
    assert "- (b) Shortages: 7% on invoice value." in normalized
    assert "- (c) Early delivery fee of $150." in normalized
    # Lettered items should NOT be headings
    assert not any(l.startswith("#") and "(a)" in l for l in normalized.splitlines())


def test_normalize_preserves_fenced_code_blocks() -> None:
    raw = (
        "# MASTER AGREEMENT\n\n"
        "## 1. Code Example\n\n"
        "```markdown\n"
        "# Inside code block: should NOT be touched\n"
        "**7.1 Bold inside code block**\n"
        "```\n"
    )
    normalized = normalize_agreement_markdown(raw)

    assert "# Inside code block: should NOT be touched" in normalized
    assert "**7.1 Bold inside code block**" in normalized


def test_normalize_is_idempotent() -> None:
    raw = (
        "# MASTER VENDOR AGREEMENT AND MERCHANDISE COMPLIANCE TERMS\n\n"
        "## RECITALS\n\n"
        "WHEREAS, Costco operates warehouse clubs.\n\n"
        "## 7. DISPUTE RESOLUTION, AUDIT PROCEDURES, AND CHARGEBACK REVERSALS\n\n"
        "**7.1 Dispute Window.** If Vendor disputes any deduction, submit within 30 days.\n\n"
        "**7.2 Required Evidence.** Include PO and BOL.\n"
    )
    pass1 = normalize_agreement_markdown(raw)
    pass2 = normalize_agreement_markdown(pass1)

    assert pass1 == pass2


# ---------------------------------------------------------------------------
# Unit tests: Validator
# ---------------------------------------------------------------------------


def test_validator_passes_canonical_markdown() -> None:
    doc = (
        "# MASTER AGREEMENT\n\n"
        "## RECITALS\n\n"
        "Recital text.\n\n"
        "## 7. Dispute Resolution\n\n"
        "### 7.1 Dispute Window\n\n"
        "Vendor must file disputes within thirty (30) days:\n"
        "- (a) Submit via portal;\n"
        "- (b) Attach BOL.\n\n"
        "#### 7.1.1 Filing Method\n\n"
        "Electronic submission required.\n"
    )
    result = validate_agreement_markdown(doc)

    assert result.is_valid
    assert len(result.errors) == 0


def test_validator_fails_multiple_h1_headings() -> None:
    doc = "# DOCUMENT TITLE\n\n# 1. First Section\n\nBody.\n"
    result = validate_agreement_markdown(doc)

    assert not result.is_valid
    assert any(e.rule == "DOC_TITLE_SINGLE_H1" for e in result.errors)


def test_validator_fails_heading_level_mismatch() -> None:
    # 7.1 should be ###, but is written as ##
    doc = "# DOCUMENT TITLE\n\n## 7. Dispute Resolution\n\n## 7.1 Dispute Window\n\nBody.\n"
    result = validate_agreement_markdown(doc)

    assert not result.is_valid
    assert any(e.rule == "HEADING_LEVEL_MISMATCH" for e in result.errors)


def test_validator_fails_lettered_item_as_heading() -> None:
    doc = "# DOCUMENT TITLE\n\n## 1. Terms\n\n### (a) Late Delivery\n\nBody.\n"
    result = validate_agreement_markdown(doc)

    assert not result.is_valid
    assert any(e.rule == "LETTERED_ITEMS_ARE_LISTS" for e in result.errors)


def test_validator_fails_unpromoted_bold_number() -> None:
    doc = (
        "# DOCUMENT TITLE\n\n"
        "## 7. Dispute Resolution\n\n"
        "**7.1 Dispute Window.** Vendor must dispute within 30 days.\n"
    )
    result = validate_agreement_markdown(doc)

    assert not result.is_valid
    assert any(e.rule == "UNPROMOTED_BOLD_NUMBER" for e in result.errors)


# ---------------------------------------------------------------------------
# Downstream compatibility tests
# ---------------------------------------------------------------------------


def test_segmentation_creates_clean_subclause_screening_units() -> None:
    doc = (
        "# MASTER VENDOR AGREEMENT\n\n"
        "## 4. ON-TIME PERFORMANCE\n\n"
        "### 4.1 On-Time Benchmark\n\n"
        "Vendor covenants to maintain 95% on-time score.\n\n"
        "### 4.2 Late Arrival Penalty\n\n"
        "Vendor pays 5% penalty for delayed goods.\n"
    )
    units = split_into_screening_units(doc)

    # Screening units include the document title and the sections
    paths = [u.section_path for u in units]
    assert any("4. ON-TIME PERFORMANCE > 4.1 On-Time Benchmark" in p for p in paths)
    assert any("4. ON-TIME PERFORMANCE > 4.2 Late Arrival Penalty" in p for p in paths)


# ---------------------------------------------------------------------------
# Comprehensive test on all repository agreements in data/retailer_agreements/
# ---------------------------------------------------------------------------


def test_all_retailer_agreements_are_valid_and_idempotent() -> None:
    repo_root = Path(__file__).resolve().parents[5]
    agreement_files = sorted(repo_root.glob("data/retailer_agreements/*.md"))

    assert len(agreement_files) == 9, f"Expected 9 agreement files, found {len(agreement_files)}"

    for agreement_path in agreement_files:
        filename = agreement_path.name
        text = agreement_path.read_text(encoding="utf-8")

        # 1. Must pass validator cleanly
        val_result = validate_agreement_markdown(text)
        assert val_result.is_valid, (
            f"{filename} failed validation with {len(val_result.errors)} error(s): "
            + "; ".join(f"L{e.line_number}[{e.rule}]: {e.message}" for e in val_result.errors[:5])
        )

        # 2. Must be idempotent under normalizer
        normalized = normalize_agreement_markdown(text)
        assert normalized == text, f"{filename} is not idempotent under normalize_agreement_markdown"

        # 3. Exactly one H1 document title
        h1s = [line for line in text.splitlines() if line.startswith("# ")]
        assert len(h1s) == 1, f"{filename} expected 1 H1 heading, found {len(h1s)}: {h1s}"
