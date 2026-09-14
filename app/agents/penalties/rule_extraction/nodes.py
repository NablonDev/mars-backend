"""LangGraph node implementations for the penalty rule extraction workflow.

Two `Send` fan-outs drive the per-unit screening and per-clause processing stages,
joining back into one fan-in node each (`resolve_candidates`, `stage_rules`). A `Send`
lane receives only its own `ScreenUnitInput`/`ProcessClauseInput` dict, never the full
state, so it structurally cannot reach `retailer_agreement_id`, `run_id`, or a session; every
database write lives in a fan-in node instead. A screening, classification, or
fact-extraction failure on one lane is carried home as a plain dict in `extraction_errors`
and flushed to `process.processing_error` by `stage_rules`; it never aborts the run. A
candidate whose facts still fail `consistency_checks.consistency_issues` after retrying is
not a failure: it stages normally, with the unresolved issues folded into its review notes.

No node closes over a long-lived session: each one that touches the database opens its
own scoped session via `self._database.session()`, mirroring
`app.workers.penalty_mitigation`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Literal
from uuid import UUID

from langgraph.types import Send, interrupt

from app.agents.penalties.rule_extraction.context import (
    ClauseClassificationContext,
    RuleFactContext,
    ScreeningUnitContext,
)
from app.agents.penalties.rule_extraction.schema import (
    CandidateClauseList,
    PenaltyFact,
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
    decide_po_scope,
    deduplicate_overlaps,
    match_excerpt,
)
from app.services.penalties.rule_extraction.consistency_checks import consistency_issues
from app.services.penalties.rule_extraction.fact_processing import evaluate_readiness, normalize_facts
from app.services.penalties.rule_extraction.segmentation import (
    ScreeningUnit,
    split_bundled_clause,
    split_into_screening_units,
)
from app.utils.sanitize import strip_nul_bytes

# The exact columns `ExtractedPenaltyRuleAttribute` accepts; anything else a fact row
# carries is folded into `extra` rather than raising on an unexpected constructor kwarg.
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

# Classification fields with no dedicated `extracted_penalty_rule` column: preserved for
# a reviewer, never read by the publisher.
_MASTER_EXTRA_FIELDS = (
    "economic_effect_type",
    "consequence_type",
    "tax_treatment",
    "settlement_method",
    "obligor_role",
    "beneficiary_role",
    "po_scope_reason",
)

# The only two decisions a reviewer can leave on a staged rule; anything else (most
# commonly PENDING_REVIEW, still undecided) is ignored rather than applied.
_DECISION_STATUSES = ("APPROVED", "REJECTED")

_MAX_FACT_EXTRACTION_ATTEMPTS = 3


def _extraction_error(
    error_code: str, message: str, node_name: str, detail: dict[str, Any]
) -> dict[str, Any]:
    """Build one JSON-safe error record, carried home in state until `stage_rules` flushes it."""
    return {"error_code": error_code, "message": message, "node_name": node_name, "detail": detail}


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
        try:
            result = self._screen(ScreeningUnitContext(label=unit["section_path"], unit_text=unit["text"]))
        except Exception as exc:  # noqa: BLE001
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
                "section_title": clause.section_title,
                "excerpt": clause.excerpt,
                "reason": clause.reason,
                "unit": unit,
            }
            for clause in result.clauses
        ]
        return {"screened_candidates": candidates}

    # ---- excerpt resolution (fan-in 1) ---- #

    def resolve_candidates(self, state: RuleExtractionState) -> dict[str, Any]:
        """Confirm every screened excerpt against the source text and collapse overlapping finds. Pure."""
        source_text = state["retailer_agreement_text"]
        pairs = [
            (candidate, match_excerpt(source_text, candidate["excerpt"], ScreeningUnit(**candidate["unit"])))
            for candidate in state.get("screened_candidates", [])
        ]
        deduplicated = deduplicate_overlaps([matched for _candidate, matched in pairs])
        kept_ids = {id(matched) for matched in deduplicated}
        clauses = [
            {
                "section_title": candidate.get("section_title"),
                "clause_text": matched.text,
                "match_kind": matched.match_kind,
                "reason": candidate.get("reason"),
            }
            for candidate, matched in pairs
            if id(matched) in kept_ids
        ]
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
        """Classify one clause, extract its facts, and normalize/validate them. Never touches the database.

        A clause bundling more than one distinct penalty category (see
        `segmentation.split_bundled_clause`) is split into per-category sub-excerpts
        first, each processed exactly like a standalone clause; a single-category clause
        takes the unchanged, unsplit path with no extra LLM call.
        """
        clause = arg["clause"]
        clause_text = clause["clause_text"]
        section_title = clause.get("section_title")
        sub_excerpts = split_bundled_clause(clause_text) or [clause_text]

        drafts: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for excerpt_text in sub_excerpts:
            outcome = self._classify_and_extract(section_title, excerpt_text)
            if "error" in outcome:
                errors.append(outcome["error"])
            else:
                drafts.append(outcome["draft"])

        result: dict[str, Any] = {}
        if drafts:
            result["drafts"] = drafts
        if errors:
            result["extraction_errors"] = errors
        return result

    # ---- staging (fan-in 2) ---- #

    def stage_rules(self, state: RuleExtractionState) -> dict[str, Any]:
        """Insert every drafted rule and its facts as PENDING_REVIEW, and flush any lane failures.

        The only node that writes staged rule rows: `human_review` and `apply_decisions`
        stay separate nodes precisely so a resume never re-runs this insert.
        """
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
                clause_text = draft["clause_text"]
                try:
                    # A savepoint, not the bare session: two drafts can carry the same
                    # clause_text (e.g. a `UNIT_FALLBACK` excerpt match staged from each of two
                    # overlapping screening units), which trips
                    # `uq_extracted_penalty_rule_run_fingerprint`. Without a savepoint to roll
                    # back to, that failed flush leaves the whole session unusable
                    # (`PendingRollbackError`) for every draft and processing_error log call
                    # after it, turning one duplicate clause into a lost run instead of one
                    # skipped rule.
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
                            section=draft.get("section_title"),
                            po_shortage_flag=bool(master.get("po_shortage_flag")),
                            po_delay_flag=bool(master.get("po_delay_flag")),
                            status="PENDING_REVIEW",
                            review_notes=_combine_notes(
                                master, draft.get("readiness_notes", []), draft.get("issues", [])
                            ),
                            extra=_master_extra(master),
                            attributes=[_attribute_to_db_dict(a) for a in draft.get("attributes", [])],
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

    # ---- human in the loop ---- #

    def human_review(self, state: RuleExtractionState) -> dict[str, Any]:
        """Pause for reviewer decisions while any staged rule from this run is still PENDING_REVIEW.

        Falls straight through with no interrupt when the queue is already empty, so an
        unattended run can finish. The resume value is a plain, JSON-safe signal that
        review is finished; a caller with nothing decided must still resume with a
        truthy sentinel such as `{"__ack__": "NO_DECISIONS"}`, never `None` or `{}`, or
        LangGraph re-raises the interrupt forever. `apply_decisions` never trusts this
        payload's content; it reads the reviewer's actual verdicts back from the
        database.
        """
        with self._database.session() as session:
            pending = ExtractedPenaltyRuleRepository(session).list_for_retailer_agreement(
                state["retailer_agreement_id"], status="PENDING_REVIEW", agent_run_id=state["run_id"]
            )
        if not pending:
            return {}
        resume = interrupt(
            {
                "reason": "rule_review_required",
                "run_id": str(state["run_id"]),
                "retailer_agreement_id": str(state["retailer_agreement_id"]),
                "pending_count": len(pending),
                "extracted_rule_ids": [str(rule.id) for rule in pending],
                "instructions": (
                    "Review each staged rule through the rule extraction review endpoint, then "
                    "resume this run. Resume with {'__ack__': 'NO_DECISIONS'} if nothing was decided."
                ),
            }
        )
        return {"resume_signal": resume if isinstance(resume, dict) else {}}

    def apply_decisions(self, state: RuleExtractionState) -> dict[str, Any]:
        """Finalize this run's reviewed rules, reading verdicts from the database, not the resume payload.

        A row whose status is not in `_DECISION_STATUSES` (still PENDING_REVIEW, most
        commonly a zero-decision resume) is ignored rather than treated as an error, so
        the run still reaches END.
        """
        with self._database.session() as session:
            rows = ExtractedPenaltyRuleRepository(session).list_for_retailer_agreement(
                state["retailer_agreement_id"], agent_run_id=state["run_id"]
            )
        applied = [str(row.id) for row in rows if row.status in _DECISION_STATUSES]
        return {"applied_rule_ids": applied}

    # ---- private helpers ---- #

    def _classify_and_extract(self, section_title: str | None, clause_text: str) -> dict[str, Any]:
        """Classify one excerpt, extract its facts if it is a penalty rule, and build its draft.

        Returns `{"draft": ...}` on success or `{"error": ...}` on a classification or
        fact-extraction failure, mirroring `process_clause`'s own error record shape. A
        candidate whose facts still fail `consistency_issues` after
        `_extract_facts_until_consistent` exhausts its retries is staged anyway, with the
        unresolved issues folded into `review_notes` for a human to resolve.
        """
        try:
            classification = self._classify(
                ClauseClassificationContext(section_title=section_title, clause_text=clause_text)
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
        normalized: list[dict[str, Any]] = []
        issues: list[str] = []
        if master.get("is_penalty_rule"):
            try:
                normalized, issues, _attempts = self._extract_facts_until_consistent(
                    clause_text, master["penalty_category"], master["calc_type"]
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
        }
        return {"draft": draft}

    def _extract_facts_until_consistent(
        self, clause_text: str, penalty_category: str, calc_type: str
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        """Extract one clause's facts, retrying up to `_MAX_FACT_EXTRACTION_ATTEMPTS` attempts total.

        Feeds the previous attempt's `consistency_issues` back into the next `RuleFactContext`
        as corrective context and stops at the first clean attempt, so the dominant,
        first-attempt-clean path makes exactly one `extract_facts` call. Any issues still
        outstanding after the last attempt are returned, not raised: `_classify_and_extract`
        stages the candidate regardless, with those issues folded into its review notes.
        """
        issues: list[str] = []
        normalized: list[dict[str, Any]] = []
        attempt = 0
        for attempt in range(1, _MAX_FACT_EXTRACTION_ATTEMPTS + 1):
            fact_list = self._extract_facts(
                RuleFactContext(
                    clause_text=clause_text,
                    penalty_category=penalty_category,
                    calc_type=calc_type,
                    previous_issues=issues or None,
                )
            )
            normalized = normalize_facts([_fact_to_dict(fact) for fact in fact_list.facts])
            issues = consistency_issues(normalized, calc_type)
            if not issues:
                break
        return normalized, issues, attempt
