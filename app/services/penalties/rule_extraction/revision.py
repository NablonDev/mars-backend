"""Reviewer revision loop: a human's instruction on one staged rule, re-run through the
classify/extract pipeline and recorded as a versioned revision row.

Entry points:
    request_revision (POST /api/v1/penalties/retailer-agreements/{id}/extracted-rules/{rule_id}/revisions)
    run_rule_revision (background task, scheduled by the POST route above via `BackgroundTasks`)

Listing a rule's revision history moved to mars-bff; see `docs/API.md` "Retailer
agreements and rule extraction" for its new path.

`RuleRevisionService` owns the request surface and its transaction boundaries, the
same way `PenaltyRuleExtractionService` owns `start_extraction`'s. `run_rule_revision`
is a separate, module-level function rather than a method: it runs after the request
that scheduled it has already returned, so it takes its own `Database` and opens its own
scoped sessions, mirroring how `RuleExtractionNodes` opens one per graph node
(`app/agents/penalties/rule_extraction/nodes.py`) instead of holding one across the LLM
calls in between.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.agents.penalties.rule_extraction.context import ClauseClassificationContext, RuleFactContext
from app.agents.penalties.rule_extraction.pipeline import (
    ClauseFeedback,
    _attribute_to_db_dict,
    _combine_notes,
    _master_extra,
    classify_and_extract,
)
from app.agents.penalties.rule_extraction.schema import PenaltyFactList, PenaltyRuleExtraction
from app.core.exceptions import ConflictError, NotFoundError
from app.db.session import Database
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    ExtractedPenaltyRuleRevisionRepository,
    rule_snapshot,
)
from app.repositories.process.agent_registry import AgentRunRepository
from app.services.penalties.rule_extraction.service import REVISION_STALE_AFTER, latest_completed_run_id
from app.utils.sanitize import strip_nul_bytes

logger = logging.getLogger(__name__)


class RuleRevisionService:
    """Queues a reviewer's revision instruction for one staged rule."""

    def __init__(
        self,
        *,
        retailer_agreements: RetailerAgreementRepository,
        extracted_rules: ExtractedPenaltyRuleRepository,
        revisions: ExtractedPenaltyRuleRevisionRepository,
        agent_runs: AgentRunRepository,
        session: Session,
    ) -> None:
        self._retailer_agreements = retailer_agreements
        self._extracted_rules = extracted_rules
        self._revisions = revisions
        self._agent_runs = agent_runs
        # Committed explicitly at the end of `request_revision`, the same documented
        # reason `PenaltyRuleExtractionService.start_extraction` commits
        # its own `agent_run` row: `run_rule_revision` reads this revision back from a
        # different session, in a background task that runs after this request returns.
        self._session = session

    def request_revision(
        self,
        retailer_agreement_id: UUID,
        extracted_rule_id: UUID,
        *,
        instruction: str,
        requested_by: str | None,
    ) -> dict[str, Any]:
        """Queue one revision for a staged rule, failing on a stale run or an already-running revision."""
        self._require_retailer_agreement(retailer_agreement_id)
        rule = self._require_rule(retailer_agreement_id, extracted_rule_id)
        if rule["agent_run_id"] != latest_completed_run_id(self._agent_runs, retailer_agreement_id):
            raise ConflictError(
                code="RULE_NOT_IN_LATEST_RUN",
                message="This rule belongs to a superseded extraction run; only rules from the latest "
                "run can be revised.",
            )

        self._revisions.fail_stale(extracted_rule_id, older_than=datetime.now(UTC) - REVISION_STALE_AFTER)
        if self._revisions.open_for_rule(extracted_rule_id) is not None:
            raise ConflictError(
                code="RULE_REVISION_IN_PROGRESS",
                message="A revision is already running for this rule.",
            )

        revision = self._revisions.create(
            extracted_rule_id,
            instruction=instruction,
            requested_by=requested_by,
            before_snapshot=rule_snapshot(rule),
        )
        self._session.commit()
        return revision

    def _require_retailer_agreement(self, retailer_agreement_id: UUID) -> dict[str, Any]:
        """Fetch a retailer agreement or raise the API's standard not-found error."""
        retailer_agreement = self._retailer_agreements.get(retailer_agreement_id)
        if retailer_agreement is None:
            raise NotFoundError(
                code="RETAILER_AGREEMENT_NOT_FOUND",
                message=f"Retailer agreement {retailer_agreement_id} does not exist.",
            )
        return retailer_agreement

    def _require_rule(self, retailer_agreement_id: UUID, extracted_rule_id: UUID) -> dict[str, Any]:
        """Fetch an extracted rule scoped to its retailer agreement, or raise not-found."""
        rule = self._extracted_rules.get_with_attributes(extracted_rule_id)
        if rule is None or rule["retailer_agreement_id"] != retailer_agreement_id:
            raise NotFoundError(
                code="EXTRACTED_RULE_NOT_FOUND",
                message=f"No extracted rule found with id={extracted_rule_id} for retailer agreement "
                f"{retailer_agreement_id}.",
            )
        return rule


def run_rule_revision(
    revision_id: UUID,
    *,
    database: Database,
    classify: Callable[[ClauseClassificationContext], PenaltyRuleExtraction],
    extract_facts: Callable[[RuleFactContext], PenaltyFactList],
) -> None:
    """Re-run classify+extract for one staged rule's clause, feeding the reviewer's instruction back in.

    Every database step opens its own `database.session()` block, closed before the next
    one opens, so the LLM calls in the middle never hold a connection idle. A missing or
    non-QUEUED revision is a silent no-op: another worker already claimed it, or it was
    never queued (see `RuleRevisionService.request_revision`'s stale/in-progress guards).
    """
    with database.session() as session:
        revisions = ExtractedPenaltyRuleRevisionRepository(session)
        revision = revisions.get(revision_id)
        if revision is None or revision["status"] != "QUEUED":
            logger.info("Skipping rule revision %s: missing or not QUEUED", revision_id)
            return
        revisions.mark_running(revision_id)

    with database.session() as session:
        extracted_rules = ExtractedPenaltyRuleRepository(session)
        revisions = ExtractedPenaltyRuleRevisionRepository(session)
        rule = extracted_rules.get_with_attributes(revision["extracted_rule_id"])
        if rule is None:
            revisions.mark_failed(revision_id, error="Extracted rule no longer exists.")
            return
        revision_history = [
            {"instruction": row["instruction"], "reply": row["agent_reply"] or ""}
            for row in revisions.list_for_rule(revision["extracted_rule_id"])
            if row["status"] == "COMPLETED" and row["id"] != revision_id
        ]
        snapshot = rule_snapshot(rule)

    try:
        outcome = classify_and_extract(
            classify=classify,
            extract_facts=extract_facts,
            section_title=rule["section"],
            clause_text=rule["clause_text"],
            keep_non_penalty=True,
            feedback=ClauseFeedback(
                reviewer_instruction=revision["instruction"],
                current_rule={k: v for k, v in snapshot.items() if k != "attributes"},
                current_facts=snapshot["attributes"],
                revision_history=revision_history,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rule revision %s failed: %s", revision_id, exc)
        with database.session() as session:
            ExtractedPenaltyRuleRevisionRepository(session).mark_failed(revision_id, error=str(exc))
        return

    if "error" in outcome:
        with database.session() as session:
            ExtractedPenaltyRuleRevisionRepository(session).mark_failed(
                revision_id, error=outcome["error"]["message"]
            )
        return

    draft = outcome["draft"]
    master = draft["master"]
    with database.session() as session:
        extracted_rules = ExtractedPenaltyRuleRepository(session)
        revisions = ExtractedPenaltyRuleRevisionRepository(session)
        extracted_rules.apply_revision(
            revision["extracted_rule_id"],
            penalty_category=master["penalty_category"],
            calc_type=master["calc_type"],
            pricing_readiness=draft.get("pricing_readiness", "AWAITING_DATA"),
            confidence=master.get("confidence", 0.5),
            po_shortage_flag=bool(master.get("po_shortage_flag")),
            po_delay_flag=bool(master.get("po_delay_flag")),
            review_notes=strip_nul_bytes(
                _combine_notes(master, draft.get("readiness_notes", []), draft.get("issues", []))
            ),
            extra=strip_nul_bytes(
                {**_master_extra(master), "computed_summary": draft.get("computed_summary")}
            ),
            attributes=strip_nul_bytes([_attribute_to_db_dict(a) for a in draft.get("attributes", [])]),
        )
        updated_rule = extracted_rules.get_with_attributes(revision["extracted_rule_id"])
        assert updated_rule is not None  # apply_revision above just wrote this row
        revisions.mark_completed(
            revision_id,
            agent_reply=master.get("reviewer_reply"),
            after_snapshot=rule_snapshot(updated_rule),
        )
