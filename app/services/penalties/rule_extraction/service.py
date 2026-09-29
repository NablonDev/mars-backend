"""Runs the rule extraction graph and publishes a retailer agreement's approved rules.

Extraction is probabilistic and human-reviewed (review itself lives in mars-bff);
publication consumes only approved staged rules and remains deterministic.

Entry points:
    start_extraction (POST /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extract)
    publish (POST /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/publish)

Retailer agreement upload/list/get, extraction status, extraction runs, extracted-rule
reads, and review moved to mars-bff; see `docs/API.md` "Retailer agreements and rule
extraction" for their new paths. Only an endpoint that runs an agent/LLM stays in this
service, plus `publish` (the deterministic rule compiler, an engine-work exception).

Reviewer revisions are handled by
`app.services.penalties.rule_extraction.revision.RuleRevisionService` through the
corresponding `.../extracted-rules/{extracted_rule_id}/revisions` endpoints. That service
shares `latest_completed_run_id` and `REVISION_STALE_AFTER` with this service; the
dependency is intentionally one-way because this module does not import `revision.py`.

This service owns the transaction boundaries between extraction and publication,
including which run's approved rules are authoritative for a retailer agreement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from app.agents.penalties.rule_extraction.prompts.v2 import (
    PENALTY_CLASSIFICATION_SYSTEM_PROMPT,
    PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT,
    PROMPT_VERSION,
    SECTION_SCREENING_SYSTEM_PROMPT,
)
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.penalties import RulePublication
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    ExtractedPenaltyRuleRevisionRepository,
    RulePublicationRepository,
    published_rule_insert_kwargs,
)
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.services.penalties.rule_extraction.publisher import PenaltyRulePublisher
from app.services.penalties.rule_extraction.types import RejectedPublication, RejectionReason
from app.utils.clock import business_today
from app.utils.ids import new_id

logger = logging.getLogger(__name__)

_AGENT_CODE = "penalty_rule_extractor"
_AGENT_NAME = "Penalty Rule Extraction"
_SCREENING_AGENT_CODE = "penalty_rule_screening"
_CLASSIFICATION_AGENT_CODE = "penalty_rule_classification"
_FACT_EXTRACTION_AGENT_CODE = "penalty_rule_fact_extraction"
_PROMPT_VERSION = PROMPT_VERSION
_RUN_TYPE = "PENALTY_RULE_EXTRACTION"

_COMPLETED_STATUS = "completed"

# How long an open (QUEUED/RUNNING) reviewer revision can sit idle before
# `ExtractedPenaltyRuleRevisionRepository.fail_stale` treats it as abandoned. Lives here,
# not in `revision.py`, so `RuleRevisionService` can reference the one constant without a
# circular import between the two modules. mars-bff's review endpoint keeps its own copy
# of this value (10 minutes; see the split's global constraints).
REVISION_STALE_AFTER = timedelta(minutes=10)


def latest_completed_run_id(agent_runs: AgentRunRepository, retailer_agreement_id: UUID) -> UUID | None:
    """The newest run tagged with this retailer agreement whose `status == "completed"`, or None.

    Shared by `PenaltyRuleExtractionService` and `RuleRevisionService`: both need "the
    latest run" to mean the same run, and there is exactly one definition of it.
    """
    runs = agent_runs.list_by_metadata(_RUN_TYPE, "retailer_agreement_id", str(retailer_agreement_id))
    return next((run["id"] for run in runs if run["status"] == _COMPLETED_STATUS), None)


@dataclass
class ExtractionStartResult:
    """Identifiers a caller needs to follow one extraction run."""

    job_run_id: UUID | None
    agent_run_id: UUID
    staged_count: int


@dataclass
class PublicationResult:
    """Outcome of publishing one retailer agreement's approved rules, accepted and rejected alike."""

    published_count: int
    rejected_count: int
    agent_run_id: UUID
    outcomes: list[RulePublication] = field(default_factory=list)


class PenaltyRuleExtractionService:
    """Drives the extraction graph and publishes a retailer agreement's approved rules."""

    def __init__(
        self,
        *,
        retailer_agreements: RetailerAgreementRepository,
        extracted_rules: ExtractedPenaltyRuleRepository,
        publications: RulePublicationRepository,
        rules: PenaltyRuleRepository,
        agent_registry: AgentRegistryRepository,
        agent_runs: AgentRunRepository,
        master_data: MasterDataRepository,
        revisions: ExtractedPenaltyRuleRevisionRepository,
        session: Session,
        graph: CompiledStateGraph | None = None,
        publisher: PenaltyRulePublisher | None = None,
    ) -> None:
        self._retailer_agreements = retailer_agreements
        self._extracted_rules = extracted_rules
        self._publications = publications
        self._rules = rules
        self._agent_registry = agent_registry
        self._agent_runs = agent_runs
        self._master_data = master_data
        self._revisions = revisions
        self._graph = graph
        self._publisher = publisher or PenaltyRulePublisher()
        # Needed only to make `agent_run` durable before `graph.invoke` runs (see
        # `start_extraction`); every other write goes through a repository. This is the
        # request-scoped session `get_session` commits at the end of the request, so
        # nothing here calls `.commit()` except those two documented spots.
        self._session = session

    def start_extraction(self, retailer_agreement_id: UUID) -> ExtractionStartResult:
        """Run the extraction graph over a retailer agreement, staging every rule it finds for review.

        Review of staged rules happens out-of-band, through the review API against the
        database, not as a further graph step: the graph ends at `stage_rules`, so this
        returns once the run has finished and every rule it found is staged as
        PENDING_REVIEW. `agent_run` is committed immediately after it opens, before
        `graph.invoke` runs: the graph's nodes open their own sessions against a
        different connection, and an uncommitted `agent_run` row would be invisible to
        them, failing `agent_trace`'s foreign key mid-run.
        """
        retailer_agreement = self._require_retailer_agreement(retailer_agreement_id)
        if not retailer_agreement["markdown_text"]:
            raise ValidationError(
                code="RETAILER_AGREEMENT_HAS_NO_TEXT",
                message=(
                    f"Retailer agreement {retailer_agreement['contract_code']!r} has no markdown_text "
                    "to extract from."
                ),
            )
        if self._graph is None:
            raise ConflictError(
                code="EXTRACTION_GRAPH_UNAVAILABLE",
                message="The penalty rule extraction graph is not wired in this process.",
            )

        agent_id = self._ensure_registered()
        run_id = self._agent_runs.start(
            agent_id=agent_id,
            run_type=_RUN_TYPE,
            metadata={
                "retailer_agreement_id": str(retailer_agreement_id),
                "prompt_version": _PROMPT_VERSION,
            },
        )
        self._session.commit()
        checkpoint_thread_id = self._new_checkpoint_thread_id()
        logger.info(
            "Starting penalty rule extraction for retailer_agreement_id=%s (agent_run_id=%s, checkpoint_thread=%s)",
            retailer_agreement_id,
            run_id,
            checkpoint_thread_id,
        )

        try:
            self._graph.invoke(
                {
                    "retailer_agreement_id": retailer_agreement_id,
                    "run_id": run_id,
                    "retailer_id": retailer_agreement["retailer_id"],
                    "retailer_agreement_text": retailer_agreement["markdown_text"],
                },
                config=self._thread_config(checkpoint_thread_id),
            )
        except Exception as exc:
            logger.error("Penalty rule extraction failed for run_id=%s: %s", run_id, exc)
            self._agent_runs.update_status(run_id, "failed", error=str(exc), completed=True)
            # Committed explicitly: `get_session`'s rollback on the propagating exception
            # would otherwise discard this status update along with it.
            self._session.commit()
            raise

        self._agent_runs.update_status(run_id, _COMPLETED_STATUS, completed=True)
        staged = self._extracted_rules.list_for_retailer_agreement(retailer_agreement_id, agent_run_id=run_id)
        logger.info(
            "Penalty rule extraction finished for retailer_agreement_id=%s: staged %d rules (run_id=%s)",
            retailer_agreement_id,
            len(staged),
            run_id,
        )
        return ExtractionStartResult(
            job_run_id=None,
            agent_run_id=run_id,
            staged_count=len(staged),
        )

    def publish(self, retailer_agreement_id: UUID) -> PublicationResult:
        """Turn a retailer agreement's approved extracted rules into live penalty rules.

        Every staged rule produces a `rule_publication` row whether it publishes or not,
        so the set the engine will never see stays countable and carries its reason.
        """
        retailer_agreement = self._require_retailer_agreement(retailer_agreement_id)
        run_id = self._latest_run_id(retailer_agreement_id)

        retailer = self._master_data.get_retailer(retailer_agreement["retailer_id"])
        if retailer is None:
            raise NotFoundError(
                code="RETAILER_NOT_FOUND",
                message=f"Retailer agreement {retailer_agreement['contract_code']!r} references an unknown retailer.",
            )
        retailer_code = retailer["retailer_code"]
        effective_date = retailer_agreement["effective_date"] or business_today()

        outcomes: list[RulePublication] = []
        published = 0
        rejected = 0

        for staged in self._extracted_rules.list_publishable(retailer_agreement_id, run_id):
            result = self._publisher.publish(staged, retailer_code, effective_date)
            if isinstance(result, RejectedPublication):
                outcomes.append(
                    self._publications.record(
                        extracted_rule_id=UUID(staged.id),
                        agent_run_id=run_id,
                        outcome="REJECTED",
                        reason_code=result.reason_code.value,
                        reason_detail=result.reason_detail,
                    )
                )
                rejected += 1
                continue

            if self._rules.get_by_rule_code(result.rule_code) is not None:
                # `rule_code` is deterministic from retailer/category/fingerprint, so a
                # second publish over an already-published run would otherwise hit
                # `penalty_rule`'s unique constraint as a raw IntegrityError.
                outcomes.append(
                    self._publications.record(
                        extracted_rule_id=UUID(staged.id),
                        agent_run_id=run_id,
                        outcome="REJECTED",
                        reason_code=RejectionReason.ALREADY_PUBLISHED.value,
                        reason_detail=f"rule_code={result.rule_code!r} is already published.",
                    )
                )
                rejected += 1
                continue

            rule = self._rules.add_rule(**published_rule_insert_kwargs(result))
            outcomes.append(
                self._publications.record(
                    extracted_rule_id=UUID(staged.id),
                    agent_run_id=run_id,
                    outcome="PUBLISHED",
                    penalty_rule_id=rule["id"],
                )
            )
            published += 1

        return PublicationResult(
            published_count=published, rejected_count=rejected, agent_run_id=run_id, outcomes=outcomes
        )

    def _require_retailer_agreement(self, retailer_agreement_id: UUID) -> dict:
        """Fetch a retailer agreement or raise the API's standard not-found error."""
        retailer_agreement = self._retailer_agreements.get(retailer_agreement_id)
        if retailer_agreement is None:
            raise NotFoundError(
                code="RETAILER_AGREEMENT_NOT_FOUND",
                message=f"Retailer agreement {retailer_agreement_id} does not exist.",
            )
        return retailer_agreement

    def _ensure_registered(self) -> UUID:
        """Ensure the run-owner and per-stage prompt rows all exist, returning the run-owner id.

        `process.agent_run.agent_id` points at `penalty_rule_extractor`; the three stage
        codes are registered here too since nothing else does, and
        `app.agents.penalties.rule_extraction.adapter._active_prompt` looks each one up by
        `agent_code` alone.
        """
        self._agent_registry.ensure_registered(
            agent_code=_SCREENING_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=SECTION_SCREENING_SYSTEM_PROMPT,
            agent_name="Penalty Rule Screening",
            domain="penalties",
        )
        self._agent_registry.ensure_registered(
            agent_code=_CLASSIFICATION_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=PENALTY_CLASSIFICATION_SYSTEM_PROMPT,
            agent_name="Penalty Rule Classification",
            domain="penalties",
        )
        self._agent_registry.ensure_registered(
            agent_code=_FACT_EXTRACTION_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT,
            agent_name="Penalty Rule Fact Extraction",
            domain="penalties",
        )
        return self._agent_registry.ensure_registered(
            agent_code=_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=PENALTY_CLASSIFICATION_SYSTEM_PROMPT,
            agent_name=_AGENT_NAME,
            domain="penalties",
        )

    def _latest_run_id_or_none(self, retailer_agreement_id: UUID) -> UUID | None:
        """The newest completed extraction run for this retailer agreement, or None if there isn't one."""
        return self._latest_completed_run_id(retailer_agreement_id)

    def _latest_completed_run_id(self, retailer_agreement_id: UUID) -> UUID | None:
        """The newest run tagged with this retailer agreement whose `status == "completed"`, or None.

        Does not check that the retailer agreement itself exists; callers that need that
        guarantee call `_require_retailer_agreement` first.
        """
        return latest_completed_run_id(self._agent_runs, retailer_agreement_id)

    def _latest_run_id(self, retailer_agreement_id: UUID) -> UUID:
        """The run that staged the most recent rules for this retailer agreement."""
        run_id = self._latest_run_id_or_none(retailer_agreement_id)
        if run_id is None:
            raise ValidationError(
                code="NO_EXTRACTION_RUN",
                message="This retailer agreement has no extracted rules to publish. Run extraction first.",
            )
        return run_id

    @staticmethod
    def _thread_config(checkpoint_thread_id: str) -> RunnableConfig:
        """Build the LangGraph `config` dict that pins a graph call to one checkpoint thread."""
        return {"configurable": {"thread_id": checkpoint_thread_id}}

    @staticmethod
    def _new_checkpoint_thread_id() -> str:
        """Generate a fresh, unique LangGraph checkpoint thread id for a new run."""
        return new_id("thread_rule_extraction")
