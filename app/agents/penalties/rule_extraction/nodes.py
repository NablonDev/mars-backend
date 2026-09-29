"""LangGraph nodes for the penalty rule extraction workflow.

Screening and clause processing fan out through `Send` and rejoin before
persistence. Database access is scoped to the nodes that perform writes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Literal
from uuid import UUID

from langgraph.types import Send

from app.agents.penalties.rule_extraction.context import (
    ClauseClassificationContext,
    RuleFactContext,
    ScreeningUnitContext,
)
from app.agents.penalties.rule_extraction.pipeline import (
    _attribute_to_db_dict,
    _combine_notes,
    _extraction_error,
    _master_extra,
    classify_and_extract,
)
from app.agents.penalties.rule_extraction.schema import (
    CandidateClauseList,
    PenaltyFactList,
    PenaltyRuleExtraction,
)
from app.agents.penalties.rule_extraction.state import (
    ProcessClauseInput,
    RuleExtractionState,
    ScreenUnitInput,
)
from app.db.session import Database
from app.repositories.penalties.rule_extraction import ExtractedPenaltyRuleRepository
from app.repositories.process.workflow import ProcessingErrorRepository
from app.services.penalties.rule_extraction.clause_matching import (
    deduplicate_overlaps,
    match_excerpt,
)
from app.services.penalties.rule_extraction.segmentation import (
    ScreeningUnit,
    split_bundled_clause,
    split_into_screening_units,
)
from app.utils.sanitize import strip_nul_bytes

logger = logging.getLogger(__name__)


class RuleExtractionNodes:
    """LangGraph node functions for the penalty rule extraction workflow, one atomic step per method."""

    def __init__(
        self,
        *,
        screen: Callable[[ScreeningUnitContext], CandidateClauseList],
        classify: Callable[[ClauseClassificationContext], PenaltyRuleExtraction],
        extract_facts: Callable[[RuleFactContext], PenaltyFactList],
        database: Database,
    ) -> None:
        self._screen = screen
        self._classify = classify
        self._extract_facts = extract_facts
        self._database = database

    # ---- segmentation and screening (fan-out 1) ---- #

    def split_document(self, state: RuleExtractionState) -> dict[str, Any]:
        """Split the contract markdown into small, section-aligned screening units. Pure."""
        units = split_into_screening_units(state["retailer_agreement_text"])
        logger.info(
            "Split contract (%d chars) into %d screening units",
            len(state.get("retailer_agreement_text", "")),
            len(units),
        )
        return {"screening_units": [asdict(u) for u in units]}

    def route_after_split_document(self, state: RuleExtractionState) -> list[Send]:
        """Fan out one screening call per screening unit."""
        return [Send("screen_unit", {"unit": unit}) for unit in state["screening_units"]]

    def screen_unit(self, arg: ScreenUnitInput) -> dict[str, Any]:
        """Screen one unit for candidate penalty clauses.

        A failed unit is recorded into `extraction_errors` and never raised: this lane
        has no session to log a `processing_error` row directly, so `stage_rules`
        flushes it later.
        """
        unit = arg["unit"]
        logger.info(
            "Screening unit %s: '%s' (%d chars)",
            unit.get("index"),
            unit.get("section_path"),
            len(unit.get("text", "")),
        )
        try:
            result = self._screen(ScreeningUnitContext(label=unit["section_path"], unit_text=unit["text"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Screening unit %s ('%s') failed: %s",
                unit.get("index"),
                unit.get("section_path"),
                exc,
            )
            return {
                "extraction_errors": [
                    _extraction_error(
                        "RULE_EXTRACTION_SCREENING_FAILED",
                        str(exc),
                        "screen_unit",
                        {"unit_index": unit["index"], "section_path": unit["section_path"]},
                    )
                ]
            }
        candidates = [
            {
                "section_title": strip_nul_bytes(clause.section_title),
                "excerpt": strip_nul_bytes(clause.excerpt),
                "reason": strip_nul_bytes(clause.reason),
                "unit": strip_nul_bytes(unit),
            }
            for clause in result.clauses
        ]
        if candidates:
            logger.info(
                "Screening unit %s found %d candidate clause(s)",
                unit.get("index"),
                len(candidates),
            )
        return {"screened_candidates": candidates}

    # ---- excerpt resolution (fan-in 1) ---- #

    def resolve_candidates(self, state: RuleExtractionState) -> dict[str, Any]:
        """Confirm every screened excerpt against the source text and collapse overlapping finds. Pure."""
        source_text = state["retailer_agreement_text"]
        candidates_in = state.get("screened_candidates", [])
        logger.info("Resolving %d screened candidate clause(s)...", len(candidates_in))
        pairs = [
            (candidate, match_excerpt(source_text, candidate["excerpt"], ScreeningUnit(**candidate["unit"])))
            for candidate in candidates_in
        ]
        deduplicated = deduplicate_overlaps([matched for _candidate, matched in pairs])
        kept_ids = {id(matched) for matched in deduplicated}
        clauses = [
            {
                "section_title": strip_nul_bytes(candidate.get("section_title")),
                "clause_text": strip_nul_bytes(matched.text),
                "match_kind": strip_nul_bytes(matched.match_kind),
                "reason": strip_nul_bytes(candidate.get("reason")),
            }
            for candidate, matched in pairs
            if id(matched) in kept_ids
        ]
        logger.info("Deduplicated to %d unique candidate clause(s) for classification", len(clauses))
        return {"candidate_clauses": clauses}

    def route_after_resolve_candidates(
        self, state: RuleExtractionState
    ) -> list[Send] | Literal["stage_rules"]:
        """Fan out one classify/extract pass per candidate clause, or go straight to staging when there are none."""
        clauses = state.get("candidate_clauses") or []
        if not clauses:
            return "stage_rules"
        return [Send("process_clause", {"clause": clause}) for clause in clauses]

    # ---- classification and fact extraction (fan-out 2) ---- #

    def process_clause(self, arg: ProcessClauseInput) -> dict[str, Any]:
        """Classify one clause, extract its facts, and build its draft.

        Bundled clauses are split into per-category excerpts before processing.
        A single-category clause uses the original text unchanged.
        """
        clause = arg["clause"]
        clause_text = clause["clause_text"]
        section_title = clause.get("section_title")
        sub_excerpts = split_bundled_clause(clause_text) or [clause_text]
        logger.info(
            "Processing candidate clause '%s' (%d sub-excerpt(s))",
            section_title or clause_text[:40].replace("\n", " "),
            len(sub_excerpts),
        )

        drafts: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for excerpt_text in sub_excerpts:
            outcome = classify_and_extract(
                classify=self._classify,
                extract_facts=self._extract_facts,
                section_title=section_title,
                clause_text=excerpt_text,
            )
            if "error" in outcome:
                errors.append(strip_nul_bytes(outcome["error"]))
            elif "skipped" in outcome:
                skipped = outcome["skipped"]
                logger.info(
                    "Dropped non-penalty clause '%s': %s",
                    section_title or excerpt_text[:40].replace("\n", " "),
                    skipped.get("review_notes"),
                )
            else:
                drafts.append(strip_nul_bytes(outcome["draft"]))

        result: dict[str, Any] = {}
        if drafts:
            result["drafts"] = drafts
        if errors:
            result["extraction_errors"] = errors
        return result

    # ---- staging (fan-in 2) ---- #

    def stage_rules(self, state: RuleExtractionState) -> dict[str, Any]:
        """Insert every drafted rule and its facts as PENDING_REVIEW, and flush any lane failures.

        The last node in the graph: review of staged rules happens out-of-band, through
        the review API against the database, not as a further graph step.
        """
        logger.info(
            "Staging %d extracted penalty rule(s) to database (agent_run_id=%s)",
            len(state.get("drafts", [])),
            state.get("run_id"),
        )
        with self._database.session() as session:
            extracted_rules = ExtractedPenaltyRuleRepository(session)
            processing_errors = ProcessingErrorRepository(session)

            for error in state.get("extraction_errors", []):
                processing_errors.log(
                    error["error_code"],
                    agent_run_id=state["run_id"],
                    error_message=strip_nul_bytes(error["message"]),
                    node_name=error["node_name"],
                    raw_error_detail=strip_nul_bytes(error["detail"]),
                )

            staged_ids: list[UUID] = []
            for draft in state.get("drafts", []):
                master = draft["master"]
                clause_text = strip_nul_bytes(draft["clause_text"])
                try:
                    # Use a savepoint so one duplicate fingerprint does not poison
                    # the session and prevent later rules or errors from being saved.
                    with session.begin_nested():
                        rule = extracted_rules.add_extracted_rule(
                            retailer_agreement_id=state["retailer_agreement_id"],
                            agent_run_id=state["run_id"],
                            clause_text=clause_text,
                            clause_fingerprint=hashlib.md5(clause_text.encode("utf-8")).hexdigest(),
                            penalty_category=master["penalty_category"],
                            calc_type=master["calc_type"],
                            pricing_readiness=draft.get("pricing_readiness", "AWAITING_DATA"),
                            confidence=master.get("confidence", 0.5),
                            section=strip_nul_bytes(draft.get("section_title")),
                            po_shortage_flag=bool(master.get("po_shortage_flag")),
                            po_delay_flag=bool(master.get("po_delay_flag")),
                            status="PENDING_REVIEW",
                            review_notes=strip_nul_bytes(
                                _combine_notes(
                                    master, draft.get("readiness_notes", []), draft.get("issues", [])
                                )
                            ),
                            extra=strip_nul_bytes(
                                {**_master_extra(master), "computed_summary": draft.get("computed_summary")}
                            ),
                            attributes=strip_nul_bytes(
                                [_attribute_to_db_dict(a) for a in draft.get("attributes", [])]
                            ),
                        )
                except Exception as exc:  # noqa: BLE001
                    processing_errors.log(
                        "RULE_EXTRACTION_PERSIST_FAILED",
                        agent_run_id=state["run_id"],
                        error_message=strip_nul_bytes(str(exc)),
                        node_name="stage_rules",
                        raw_error_detail={"penalty_category": master.get("penalty_category")},
                    )
                    continue
                staged_ids.append(rule.id)

        return {"staged_rule_ids": staged_ids}
