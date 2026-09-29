# Canonical Agreement Markdown Format Specification

## 1. Overview and Purpose

This specification establishes the canonical Markdown format for retailer and supplier agreements across the Mars Petcare platform.

Standardizing contract heading depth and formatting ensures:
1. **Deterministic Segmentation**: Downstream extraction and screening layers (e.g. `segmentation.split_into_screening_units`) split agreements into section- and clause-level screening units with accurate ancestry breadcrumbs.
2. **Converter Immunity**: Upstream document converters and chunked LLM extraction pipelines often suffer from heading level drift (e.g. assigning `#` to top sections or `####` to subclauses). The deterministic normalizer corrects these drifts as a required post-processing step.
3. **Lossless Transformation**: All legal text, clause numbers, and structural relations are strictly preserved.

---

## 2. Heading Hierarchy

Agreements follow a strict four-level markdown heading hierarchy:

| Level | Syntax | Element | Example | Notes |
|---|---|---|---|---|
| **1** | `#` | Document Title | `# MASTER VENDOR AGREEMENT` | Exactly **one** Level 1 heading per agreement. |
| **2** | `##` | Article, Section, or Major Division | `## 7. Dispute Resolution`<br>`## RECITALS`<br>`## Schedule 1: Form of Order`<br>`## Exhibit A — Standards` | Numbering (e.g. `7.`, `Section 7.`, `ARTICLE VII.`) always stays in the heading. |
| **3** | `###` | Numbered Sub-clause | `### 7.1 Dispute Window`<br>`### 1.1 Defined Terms`<br>`### 3.A Supplier Inputs`<br>`### B.1 Definitions` | Two-level numbering (`N.N`, `N.Letter`, `Letter.N`). Number always stays in heading text. |
| **4** | `####` | Third-level Numbering | `#### 7.1.1 Filing Period`<br>`#### 21.3.1 Alternative Supplier` | Three-level numbering (`N.N.N`). Number always stays in heading text. |

---

## 3. Formatting Rules

### 3.1. Number Retention in Headings
The clause or section number **must always remain** inside the heading text:
- **Correct**: `## 7. Dispute Resolution`
- **Incorrect**: `## Dispute Resolution`
- **Correct**: `### 7.1 Dispute Window`
- **Incorrect**: `### Dispute Window`

### 3.2. Lettered Items are List Items
Lettered sub-items (e.g. `(a)`, `(b)`, `(c)`) **must never** be headings (`#`, `##`, `###`). They must remain standard Markdown list items:
```markdown
### 3.4 OTIF Penalties

In the event Vendor fails to meet delivery requirements, Purchaser shall assess:
- (a) **Late Delivery:** Four percent (4.0%) penalty per calendar day delayed.
- (b) **Shortages:** Seven percent (7.0%) on the total invoice value of missing merchandise.
- (c) **Early Delivery:** $150.00 per pallet per day warehousing fee.
```

### 3.3. Bold-Numbered Lines Promoted to Headings
Source documents or OCR conversions frequently format numbered sub-clauses as bold inline text at the start of a paragraph rather than as Markdown headings:
```markdown
**7.1 Dispute Window.** If Vendor disputes any compliance deduction, Vendor must...
```
The canonical format requires promoting these lines to `###` headings, placing the clause body on subsequent lines:
```markdown
### 7.1 Dispute Window

If Vendor disputes any compliance deduction, Vendor must...
```

### 3.4. Major Sections and Exhibits
- Recitals, Background, General Terms, and Exhibit headers without numeric prefixes are Level 2 (`##`):
  - `## RECITALS`
  - `## Background`
  - `## Exhibit A: Products and Pricing`
- Sub-items within schedules or exhibits are Level 3 (`###`):
  - `### Schedule 1 ([**])`
  - `### 1. COLD DRINK EQUIPMENT COMMITMENT`

---

## 4. Deterministic Normalizer

The platform provides a deterministic normalizer (`app.services.penalties.rule_extraction.agreement_normalizer.normalize_agreement_markdown`):

- **Heading Re-leveling**:
  - `N.` -> `##` (Level 2)
  - `N.N` -> `###` (Level 3)
  - `N.N.N` -> `####` (Level 4)
- **Bold-Numbered Promotion**:
  - Detects inline bold clause headers (`**7.1 Dispute Window.**`, `**1.1**`, `**3.A**`, `2.1. **Title.**`) and elevates them to `###` or `####` headings.
- **List Item Formatting**:
  - Automatically prefixes lettered items (`(a)`, `**(a)**`) with `- `.
- **Idempotency**:
  - For any input text $T$, $\text{normalize}(\text{normalize}(T)) == \text{normalize}(T)$.

Any extraction layer or converter **must** run its output through this normalizer as its final step before persisting or segmenting agreement text.

---

## 5. Validator

The validator (`app.services.penalties.rule_extraction.agreement_validator.validate_agreement_markdown`) verifies that Markdown adheres strictly to this specification:

| Rule Code | Description | Severity |
|---|---|---|
| `DOC_TITLE_SINGLE_H1` | Exactly one Level 1 (`#`) heading representing the document title. | Error |
| `HEADING_LEVEL_MISMATCH` | Heading level must match numbering depth (`N.` -> `##`, `N.N` -> `###`, `N.N.N` -> `####`). | Error |
| `LETTERED_ITEMS_ARE_LISTS` | Lettered items `(a)`, `(b)` must not be formatted as Markdown headings. | Error |
| `UNPROMOTED_BOLD_NUMBER` | Numbered subclauses (`**7.1 ...**`) must not be left unpromoted as bold body text. | Error |
| `HEADING_EMPTY_TEXT` | Headings must contain descriptive text following `#` hashes. | Error |
