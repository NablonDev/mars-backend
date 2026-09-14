"""Coordinates retailer agreement rule extraction across repositories, the LangGraph workflow, and the publisher.

Entry points:
    create_retailer_agreement (POST /api/v1/penalties/retailer-agreements)
    start_extraction (POST /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extract)
    get_extraction_status (GET /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extraction)
    list_extracted_rules (GET /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extracted-rules)
    get_extracted_rule (GET /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extracted-rules/{extracted_rule_id})
    submit_review (POST /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extracted-rules/{extracted_rule_id}/review)
    resume_review (POST /api/v1/workflow-threads/{thread_id}/decisions)
    publish (POST /api/v1/penalties/retailer-agreements/{retailer_agreement_id}/publish)

Extraction is probabilistic and reviewed by a person; publication is pure and deterministic.
This service owns the seam between them, including the transaction boundaries and which
run's approved rules are authoritative for a retailer agreement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.orm import Session

from app.agents.penalties.rule_extraction.prompts.v1 import (
    PENALTY_CLASSIFICATION_SYSTEM_PROMPT,
    PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT,
    PROMPT_VERSION,
    SECTION_SCREENING_SYSTEM_PROMPT,
)
from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.enums import WorkflowThreadSubjectType
from app.models.penalties.rule_extraction import RulePublication
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    RulePublicationRepository,
    published_rule_insert_kwargs,
)
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.repositories.process.workflow import HumanActionRepository, WorkflowThreadRepository
from app.services.penalties.rule_extraction.publisher import PenaltyRulePublisher
from app.services.penalties.rule_extraction.types import RejectedPublication, RejectionReason
from app.utils.clock import business_today
from app.utils.hashing import content_sha256
from app.utils.ids import new_id

logger = logging.getLogger(__name__)

_AGENT_CODE = "penalty_rule_extractor"
_AGENT_NAME = "Penalty Rule Extraction"
_SCREENING_AGENT_CODE = "penalty_rule_screening"
_CLASSIFICATION_AGENT_CODE = "penalty_rule_classification"
_FACT_EXTRACTION_AGENT_CODE = "penalty_rule_fact_extraction"
_PROMPT_VERSION = PROMPT_VERSION
_RUN_TYPE = "PENALTY_RULE_EXTRACTION"

INTERRUPT_KEY = "__interrupt__"

# `human_review`'s one interrupt reason (`app/agents/penalties/rule_extraction/nodes.py`)
# and the review-thread's stage/status/node while it waits on it.
_INTERRUPT_REASON = "rule_review_required"
_AWAITING_STAGE = "AWAITING_RULE_REVIEW"
_AWAITING_STATUS = "waiting_rule_review"
_REVIEW_NODE = "human_review"
_COMPLETED_STAGE = "RULE_REVIEW_COMPLETE"
_COMPLETED_STATUS = "completed"
_NOT_STARTED_STATUS = "NOT_STARTED"


@dataclass
class ExtractionStartResult:
    """Identifiers a caller needs to follow one extraction run and its review thread.

    `thread_id` is `None` when nothing in the retailer agreement needed review: `human_review`
    never interrupted, so no reviewer-facing `workflow_thread` was ever created.
    """

    job_run_id: UUID | None
    agent_run_id: UUID
    thread_id: UUID | None
    staged_count: int


@dataclass
class PublicationResult:
    """Outcome of publishing one retailer agreement's approved rules, accepted and rejected alike."""

    published_count: int
    rejected_count: int
    agent_run_id: UUID
    outcomes: list[RulePublication] = field(default_factory=list)


class PenaltyRuleExtractionService:
    """Drives the extraction graph, records review decisions, and publishes approved rules."""

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
        workflow_threads: WorkflowThreadRepository,
        human_actions: HumanActionRepository,
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
        self._workflow_threads = workflow_threads
        self._human_actions = human_actions
        self._graph = graph
        self._publisher = publisher or PenaltyRulePublisher()
        # Needed only to make `agent_run` durable before `graph.invoke` runs (see
        # `start_extraction`); every other write goes through a repository. This is the
        # request-scoped session `get_session` commits at the end of the request, so
        # nothing here calls `.commit()` except those two documented spots.
        self._session = session

    def create_retailer_agreement(
        self,
        retailer_id: UUID,
        contract_code: str,
        title: str,
        markdown_text: str,
        source_uri: str | None = None,
        effective_date: date | None = None,
        expiration_date: date | None = None,
    ) -> dict:
        """Store a retailer agreement, returning the existing row when the same markdown was already uploaded."""
        document_sha256 = content_sha256(markdown_text)
        existing = self._retailer_agreements.get_by_sha256(document_sha256)
        if existing is not None:
            return existing
        return self._retailer_agreements.add_retailer_agreement(
            retailer_id=retailer_id,
            contract_code=contract_code,
            title=title,
            document_sha256=document_sha256,
            source_uri=source_uri,
            markdown_text=markdown_text,
            effective_date=effective_date,
            expiration_date=expiration_date,
        )

    def start_extraction(self, retailer_agreement_id: UUID) -> ExtractionStartResult:
        """Run the extraction graph over a retailer agreement, staging every rule it finds for review.

        The graph interrupts at `human_review` whenever a staged rule needs a decision, so
        this returns once rules are staged rather than once they are approved. `agent_run`
        is committed immediately after it opens, before `graph.invoke` runs: the graph's
        nodes open their own sessions against a different connection, and an uncommitted
        `agent_run` row would be invisible to them, failing `agent_trace`'s foreign key
        mid-run.
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
        run_id = self._agent_runs.start(agent_id=agent_id, run_type=_RUN_TYPE)
        self._session.commit()
        checkpoint_thread_id = self._new_checkpoint_thread_id()

        try:
            state = self._graph.invoke(
                {
                    "retailer_agreement_id": retailer_agreement_id,
                    "run_id": run_id,
                    "retailer_id": retailer_agreement["retailer_id"],
                    "retailer_agreement_text": retailer_agreement["markdown_text"],
                },
                config=self._thread_config(checkpoint_thread_id),
            )
        except Exception as exc:
            self._agent_runs.update_status(run_id, "failed", error=str(exc), completed=True)
            # Committed explicitly: `get_session`'s rollback on the propagating exception
            # would otherwise discard this status update along with it.
            self._session.commit()
            raise

        result = self._handle_graph_state(
            retailer_agreement_id=retailer_agreement_id,
            run_id=run_id,
            checkpoint_thread_id=checkpoint_thread_id,
            state=state,
        )
        staged = self._extracted_rules.list_for_retailer_agreement(retailer_agreement_id, agent_run_id=run_id)
        return ExtractionStartResult(
            job_run_id=None,
            agent_run_id=run_id,
            thread_id=result.get("id"),
            staged_count=len(staged),
        )

    def get_extraction_status(self, retailer_agreement_id: UUID) -> dict[str, Any]:
        """Report the most recent extraction run's status for a retailer agreement.

        Prefers the reviewer-facing workflow thread when one exists, whether still
        awaiting review or resolved. Falls back to the staged rules themselves for a
        touchless run that never created a thread, and reports `NOT_STARTED` when
        extraction has never run at all.
        """
        self._require_retailer_agreement(retailer_agreement_id)
        thread = self._workflow_threads.get_latest_by_subject(
            WorkflowThreadSubjectType.RETAILER_AGREEMENT, retailer_agreement_id
        )
        if thread is not None:
            metadata = thread["metadata_json"] or {}
            raw_run_id = metadata.get("agent_run_id")
            return {
                "retailer_agreement_id": retailer_agreement_id,
                "agent_run_id": UUID(raw_run_id) if raw_run_id else None,
                "workflow_thread_id": thread["id"],
                "status": thread["status"],
                "stage": thread["stage"],
                "current_node": thread["current_node"],
                "completed_at": thread["completed_at"],
                "error": thread["error"],
            }

        run_id = self._latest_run_id_or_none(retailer_agreement_id)
        if run_id is None:
            return {
                "retailer_agreement_id": retailer_agreement_id,
                "agent_run_id": None,
                "workflow_thread_id": None,
                "status": _NOT_STARTED_STATUS,
                "stage": None,
                "current_node": None,
                "completed_at": None,
                "error": None,
            }

        return {
            "retailer_agreement_id": retailer_agreement_id,
            "agent_run_id": run_id,
            "workflow_thread_id": None,
            "status": _COMPLETED_STATUS,
            "stage": _COMPLETED_STAGE,
            "current_node": None,
            "completed_at": None,
            "error": None,
        }

    def resume_review(self, thread_id: UUID, *, actor: str, expected_updated_at: str) -> dict[str, Any]:
        """Resume a rule-extraction run paused at `human_review`.

        Carries no verdicts: `apply_decisions` re-reads each staged rule's current review
        status from the database rather than trusting this call. Always resumes with a
        truthy payload (`{"__ack__": "NO_DECISIONS"}` when nothing was decided), since
        LangGraph re-raises the interrupt forever on a falsy one.
        """
        if self._graph is None:
            raise ConflictError(
                code="EXTRACTION_GRAPH_UNAVAILABLE",
                message="The penalty rule extraction graph is not wired in this process.",
            )
        stage = self._ensure_current(thread_id, expected_updated_at)
        if stage["status"] != _AWAITING_STATUS:
            raise self._thread_not_waiting(thread_id, _AWAITING_STATUS, stage["status"])
        pending = self._require_open_pending(thread_id, _INTERRUPT_REASON)

        metadata = stage["metadata_json"] or {}
        retailer_agreement_id = UUID(metadata["retailer_agreement_id"])
        run_id = UUID(metadata["agent_run_id"])
        checkpoint_thread_id = metadata["checkpoint_thread_id"]

        decided_count = sum(
            1
            for row in self._extracted_rules.list_for_retailer_agreement(
                retailer_agreement_id, agent_run_id=run_id
            )
            if row.status in ("APPROVED", "REJECTED")
        )
        answer: dict[str, Any] = (
            {"decided_count": decided_count} if decided_count else {"__ack__": "NO_DECISIONS"}
        )

        try:
            state = self._graph.invoke(
                Command(resume=answer), config=self._thread_config(checkpoint_thread_id)
            )
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc

        return self._handle_graph_state(
            retailer_agreement_id=retailer_agreement_id,
            run_id=run_id,
            checkpoint_thread_id=checkpoint_thread_id,
            state=state,
            resume_context={
                "workflow_thread_id": thread_id,
                "pending_action_id": pending["id"],
                "answer": answer,
                "actor": actor,
                "action_type": "rule_review_resume",
            },
        )

    def list_extracted_rules(
        self, retailer_agreement_id: UUID, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List a retailer agreement's extracted rules with attributes, optionally filtered by status."""
        self._require_retailer_agreement(retailer_agreement_id)
        return self._extracted_rules.list_with_attributes_for_retailer_agreement(
            retailer_agreement_id, status=status
        )

    def get_extracted_rule(self, retailer_agreement_id: UUID, extracted_rule_id: UUID) -> dict[str, Any]:
        """Fetch one extracted rule with its attributes, scoped to its retailer agreement."""
        self._require_retailer_agreement(retailer_agreement_id)
        result = self._extracted_rules.get_with_attributes(extracted_rule_id)
        if result is None or result["retailer_agreement_id"] != retailer_agreement_id:
            raise NotFoundError(
                code="EXTRACTED_RULE_NOT_FOUND",
                message=f"No extracted rule found with id={extracted_rule_id} for retailer agreement "
                f"{retailer_agreement_id}.",
            )
        return result

    def submit_review(
        self,
        retailer_agreement_id: UUID,
        extracted_rule_id: UUID,
        status: str,
        review_notes: str | None = None,
    ) -> dict[str, Any]:
        """Record one reviewer decision and return the rule with its attributes.

        Only APPROVED and REJECTED are decisions. `status` membership is validated by
        `ExtractedPenaltyRuleRepository.set_review_decision`, the single site for that
        check; this method does not repeat it.
        """
        self._require_retailer_agreement(retailer_agreement_id)
        self._extracted_rules.set_review_decision(extracted_rule_id, status, review_notes=review_notes)
        return self.get_extracted_rule(retailer_agreement_id, extracted_rule_id)

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

            rule = self._rules.add_rule(
                **published_rule_insert_kwargs(result, retailer_agreement["retailer_id"])
            )
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
        """The run that staged the most recent rules for this retailer agreement, or None if there isn't one."""
        staged = self._extracted_rules.list_for_retailer_agreement(retailer_agreement_id)
        if not staged:
            return None
        return max(staged, key=lambda row: row.created_at).agent_run_id

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
    def _snapshot_state(state: dict[str, Any]) -> dict[str, Any]:
        """Strip the LangGraph interrupt marker out of graph state before persisting it as a snapshot."""
        return {key: value for key, value in state.items() if key != INTERRUPT_KEY}

    @staticmethod
    def _thread_not_found(thread_id: UUID) -> NotFoundError:
        """Build the `NotFoundError` raised for an unknown `thread_id`."""
        return NotFoundError(
            code="THREAD_NOT_FOUND", message="Unknown thread_id.", details={"thread_id": str(thread_id)}
        )

    @staticmethod
    def _resume_failed(thread_id: UUID, pending_action_id: UUID, exc: Exception) -> ExternalServiceError:
        """Log and build the `ExternalServiceError` raised when resuming a thread's graph run fails unexpectedly."""
        logger.exception(
            "Failed to resume rule extraction thread %s from pending action %s", thread_id, pending_action_id
        )
        return ExternalServiceError(
            code="WORKFLOW_RESUME_FAILED",
            message="Unexpected failure while resuming workflow.",
            details={
                "thread_id": str(thread_id),
                "pending_action_id": str(pending_action_id),
                "error": str(exc),
            },
        )

    @staticmethod
    def _thread_not_waiting(thread_id: UUID, expected: str, actual: str | None) -> ConflictError:
        """Build the `ConflictError` raised when a resume API is called against a thread not paused on that interrupt."""
        return ConflictError(
            code="THREAD_NOT_WAITING",
            message="Resume API called while thread is not paused for that action.",
            details={"thread_id": str(thread_id), "expected": expected, "actual": actual},
        )

    def _create_review_thread(self, *, retailer_agreement_id: UUID, metadata: dict[str, Any]) -> UUID:
        """Create the reviewer-facing workflow thread for one rule-extraction run."""
        thread = self._workflow_threads.create(
            _AWAITING_STAGE,
            subject_type=WorkflowThreadSubjectType.RETAILER_AGREEMENT,
            subject_id=retailer_agreement_id,
            status=_AWAITING_STATUS,
            current_node=_REVIEW_NODE,
            metadata=metadata,
        )
        return thread["id"]

    @staticmethod
    def _new_checkpoint_thread_id() -> str:
        """Generate a fresh, unique LangGraph checkpoint thread id for a new run."""
        return new_id("thread_rule_extraction")

    def _ensure_current(self, thread_id: UUID, expected_updated_at: str) -> dict[str, Any]:
        """Return the thread's current stage, or raise `ConflictError` if it has moved since `expected_updated_at`."""
        stage = self._workflow_threads.get_stage(thread_id)
        if stage is None:
            raise self._thread_not_found(thread_id)
        if stage["updated_at"].isoformat() != expected_updated_at:
            raise ConflictError(
                code="THREAD_STALE",
                message="Thread was updated by another reviewer. Refresh snapshot and retry.",
                details={"thread_id": str(thread_id), "latest_updated_at": stage["updated_at"].isoformat()},
            )
        return stage

    def _require_open_pending(self, thread_id: UUID, interrupt_type: str) -> dict[str, Any]:
        """Return the thread's open `human_action` row, or raise if it isn't waiting on `interrupt_type`."""
        pending = self._human_actions.get_open_for_thread(thread_id)
        if pending is None or pending["interrupt_type"] != interrupt_type:
            actual = None if pending is None else pending["interrupt_type"]
            raise self._thread_not_waiting(thread_id, interrupt_type, actual)
        return pending

    def _handle_graph_state(
        self,
        *,
        retailer_agreement_id: UUID,
        run_id: UUID,
        checkpoint_thread_id: str,
        state: dict[str, Any],
        resume_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist workflow_thread/human_action state after a rule-extraction graph invoke or resume."""
        if state.get(INTERRUPT_KEY):
            payload = state[INTERRUPT_KEY][0].value
            if resume_context is None:
                workflow_thread_id = self._create_review_thread(
                    retailer_agreement_id=retailer_agreement_id,
                    metadata={
                        "checkpoint_thread_id": checkpoint_thread_id,
                        "agent_run_id": str(run_id),
                        "retailer_agreement_id": str(retailer_agreement_id),
                        "latest_snapshot": {"pending_count": payload.get("pending_count")},
                    },
                )
                self._human_actions.create_open(
                    _INTERRUPT_REASON,
                    payload,
                    workflow_thread_id=workflow_thread_id,
                    agent_run_id=run_id,
                    state_snapshot=self._snapshot_state(state),
                )
            else:
                workflow_thread_id = resume_context["workflow_thread_id"]
                thread = self._workflow_threads.get_by_id(workflow_thread_id)
                metadata = {
                    **((thread or {}).get("metadata_json") or {}),
                    "latest_snapshot": {"pending_count": payload.get("pending_count")},
                }
                self._human_actions.apply_human_action(
                    pending_action_id=resume_context["pending_action_id"],
                    workflow_thread_id=workflow_thread_id,
                    response_payload=resume_context["answer"],
                    actor=resume_context["actor"],
                    action_type=resume_context["action_type"],
                    next_status=_AWAITING_STATUS,
                    next_stage=_AWAITING_STAGE,
                    next_current_node=_REVIEW_NODE,
                    next_metadata=metadata,
                    next_pending_interrupt_type=_INTERRUPT_REASON,
                    next_pending_request_payload=payload,
                    next_pending_state_snapshot=self._snapshot_state(state),
                )
            self._agent_runs.update_status(run_id, _AWAITING_STATUS)
            return self._workflow_threads.get_stage(workflow_thread_id) or {}

        # No interrupt: `apply_decisions` ran and the graph reached END.
        self._agent_runs.update_status(run_id, _COMPLETED_STATUS, completed=True)
        if resume_context is None:
            # Touchless path: nothing needed review, so no thread was ever created.
            return {
                "id": None,
                "agent_run_id": run_id,
                "stage": _COMPLETED_STAGE,
                "status": _COMPLETED_STATUS,
            }

        workflow_thread_id = resume_context["workflow_thread_id"]
        thread = self._workflow_threads.get_by_id(workflow_thread_id)
        metadata = {
            **((thread or {}).get("metadata_json") or {}),
            "applied_rule_ids": state.get("applied_rule_ids", []),
        }
        self._human_actions.apply_human_action(
            pending_action_id=resume_context["pending_action_id"],
            workflow_thread_id=workflow_thread_id,
            response_payload=resume_context["answer"],
            actor=resume_context["actor"],
            action_type=resume_context["action_type"],
            next_status=_COMPLETED_STATUS,
            next_stage=_COMPLETED_STAGE,
            next_metadata=metadata,
            completed=True,
        )
        return self._workflow_threads.get_stage(workflow_thread_id) or {}
