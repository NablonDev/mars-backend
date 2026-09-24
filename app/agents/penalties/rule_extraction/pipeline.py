"""Reusable classify-and-extract pipeline for one penalty clause.

Classification, fact extraction, consistency retries, and draft construction
use the same path for every caller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agents.penalties.rule_extraction.context import ClauseClassificationContext, RuleFactContext
from app.agents.penalties.rule_extraction.schema import PenaltyFact, PenaltyFactList, PenaltyRuleExtraction
from app.services.penalties.rule_extraction.clause_matching import decide_po_scope
from app.services.penalties.rule_extraction.consistency_checks import consistency_issues
from app.services.penalties.rule_extraction.fact_processing import evaluate_readiness, normalize_facts
from app.services.penalties.rule_extraction.rule_summary import describe_rule

_MAX_FACT_EXTRACTION_ATTEMPTS = 4

# Keep this list aligned with the columns accepted by `ExtractedPenaltyRuleAttribute`.
# Fields outside this set are preserved in `extra`.
_ATTRIBUTE_COLUMNS = (
    "branch_no",
    "attribute_role",
    "metric_code",
    "metric_denominator",
    "operator",
    "value",
    "value_max",
    "value_unit",
    "value_status",
    "currency_code",
    "basis_type",
    "applies_per",
    "tier_application",
    "cap_scope",
    "source_text",
    "confidence",
)

# These classification fields have no dedicated database columns. Preserve them
# for reviewer context rather than exposing them as publisher inputs.
_MASTER_EXTRA_FIELDS = (
    "economic_effect_type",
    "consequence_type",
    "tax_treatment",
    "settlement_method",
    "obligor_role",
    "beneficiary_role",
    "po_scope_reason",
    "plain_explanation",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClauseFeedback:
    """A reviewer's instruction on one staged rule, fed back into both LLM stages as data."""

    reviewer_instruction: str
    current_rule: dict[str, Any]
    current_facts: list[dict[str, Any]]
    revision_history: list[dict[str, str]]


def classify_and_extract(
    *,
    classify: Callable[[ClauseClassificationContext], PenaltyRuleExtraction],
    extract_facts: Callable[[RuleFactContext], PenaltyFactList],
    section_title: str | None,
    clause_text: str,
    keep_non_penalty: bool = False,
    feedback: ClauseFeedback | None = None,
) -> dict[str, Any]:
    """Classify one clause, extract its facts, and build its draft.

    Returns an `error` result when classification or fact extraction fails, a `skipped`
    result for excluded non-penalty clauses, and a `draft` result otherwise. Fact
    extraction retries inconsistent results up to the configured attempt limit. With
    `feedback`, reviewer context is included in every LLM call.
    """
    try:
        classification = classify(
            ClauseClassificationContext(
                section_title=section_title,
                clause_text=clause_text,
                reviewer_instruction=feedback.reviewer_instruction if feedback else None,
                current_rule=feedback.current_rule if feedback else None,
                revision_history=feedback.revision_history if feedback else None,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "error": _extraction_error(
                "RULE_EXTRACTION_CLASSIFICATION_FAILED",
                str(exc),
                "process_clause",
                {"excerpt": clause_text[:500]},
            )
        }

    master = classification.model_dump()
    logger.info(
        "Classified clause '%s': is_penalty_rule=%s, category=%s, calc_type=%s",
        section_title or clause_text[:30].replace("\n", " "),
        master.get("is_penalty_rule"),
        master.get("penalty_category"),
        master.get("calc_type"),
    )

    if not master.get("is_penalty_rule") and not keep_non_penalty:
        return {
            "skipped": {
                "section_title": section_title,
                "excerpt": clause_text[:200],
                "review_notes": master.get("review_notes"),
            }
        }

    normalized: list[dict[str, Any]] = []
    issues: list[str] = []
    if master.get("is_penalty_rule"):
        try:
            normalized, issues, _attempts = _extract_facts_until_consistent(
                extract_facts,
                clause_text,
                master["penalty_category"],
                master["calc_type"],
                feedback,
            )
            logger.info(
                "Extracted %d attribute fact(s) for category '%s' (attempts=%d)",
                len(normalized),
                master.get("penalty_category"),
                _attempts,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "error": _extraction_error(
                    "RULE_EXTRACTION_FACT_EXTRACTION_FAILED",
                    str(exc),
                    "process_clause",
                    {"penalty_category": master.get("penalty_category")},
                )
            }

    scope = decide_po_scope(
        master["penalty_category"],
        bool(master.get("po_shortage_flag")),
        bool(master.get("po_delay_flag")),
    )
    master["po_shortage_flag"] = scope.shortage
    master["po_delay_flag"] = scope.delay
    master["po_scope_reason"] = scope.reason

    readiness, readiness_notes = evaluate_readiness(master["calc_type"], normalized)

    draft = {
        "clause_text": clause_text,
        "section_title": section_title,
        "master": master,
        "attributes": normalized,
        "issues": issues,
        "readiness_notes": readiness_notes,
        "pricing_readiness": readiness,
        "computed_summary": describe_rule(
            calc_type=master["calc_type"],
            consequence_type=master.get("consequence_type"),
            settlement_method=master.get("settlement_method"),
            facts=normalized,
        ),
    }
    return {"draft": draft}


def _extraction_error(
    error_code: str, message: str, node_name: str, detail: dict[str, Any]
) -> dict[str, Any]:
    """Build one JSON-safe error record, carried home in state until `stage_rules` flushes it."""
    return {"error_code": error_code, "message": message, "node_name": node_name, "detail": detail}


def _extract_facts_until_consistent(
    extract_facts: Callable[[RuleFactContext], PenaltyFactList],
    clause_text: str,
    penalty_category: str,
    calc_type: str,
    feedback: ClauseFeedback | None = None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Extract facts until they pass consistency checks or retries are exhausted.

    Feeds consistency issues from each failed attempt into the next extraction
    context. Returns the final facts, remaining issues, and number of attempts.
    """
    issues: list[str] = []
    normalized: list[dict[str, Any]] = []
    attempt = 0
    for attempt in range(1, _MAX_FACT_EXTRACTION_ATTEMPTS + 1):
        fact_list = extract_facts(
            RuleFactContext(
                clause_text=clause_text,
                penalty_category=penalty_category,
                calc_type=calc_type,
                previous_issues=issues or None,
                reviewer_instruction=feedback.reviewer_instruction if feedback else None,
                current_facts=feedback.current_facts if feedback else None,
            )
        )
        normalized = normalize_facts([_fact_to_dict(fact) for fact in fact_list.facts])
        issues = consistency_issues(normalized, calc_type)
        if not issues:
            break
    return normalized, issues, attempt


def _fact_to_dict(fact: PenaltyFact) -> dict[str, Any]:
    """Convert one LLM-extracted fact into the flat dict the pure normalizer/validator read.

    `group_no` is what those modules call what the schema and the database call
    `branch_no`; both names are kept on the row so persistence can read either.
    """
    row = fact.model_dump()
    row["group_no"] = row["branch_no"]
    return row


def _attribute_to_db_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Map one normalized fact row back onto `ExtractedPenaltyRuleAttribute`'s exact columns."""
    out: dict[str, Any] = {"branch_no": row.get("group_no", row.get("branch_no", 0))}
    extra = dict(row.get("extra") or {})
    for key, value in row.items():
        if key in ("group_no", "branch_no", "extra"):
            continue
        if key in _ATTRIBUTE_COLUMNS:
            out[key] = value
        else:
            extra[key] = value
    out["extra"] = extra
    return out


def _combine_notes(master: dict[str, Any], readiness_notes: list[str], issues: list[str]) -> str | None:
    """Join the model's own review_notes with anything readiness evaluation or consistency checks found."""
    parts = [p for p in (master.get("review_notes"), *readiness_notes, *issues) if p]
    return "\n".join(parts) if parts else None


def _master_extra(master: dict[str, Any]) -> dict[str, Any]:
    """Classification fields kept for a reviewer's context, never read by the publisher."""
    return {field: master[field] for field in _MASTER_EXTRA_FIELDS if master.get(field) is not None}
