"""Coordinates CMIR resolution workflow across repositories and LangGraph.

Entry points:
    start_email_ingest (POST /api/v1/cmir/email-events)
    process_queued_email (POST /api/v1/internal/process-email)
    list_runs (GET /api/v1/workflow-threads)
    get_stage (GET /api/v1/workflow-threads/{thread_id})
    get_snapshot (GET /api/v1/workflow-threads/{thread_id})
    submit_missing_fields (POST /api/v1/workflow-threads/{thread_id}/missing-fields)
    update_draft (PATCH /api/v1/workflow-threads/{thread_id}/draft)
    submit_decision (POST /api/v1/workflow-threads/{thread_id}/decisions)
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any
from uuid import UUID

from langgraph.types import Command

from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.enums import JobRunType, JobTaskType, WorkflowThreadSubjectType
from app.repositories.cmir.cmir_record import CmirRecordRepository
from app.repositories.cmir.email import EmailRepository
from app.repositories.cmir.job_context import CmirJobItemContextRepository, CmirJobRunContextRepository
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.repositories.process.workflow import HumanActionRepository, WorkflowThreadRepository
from app.schemas.cmir import CMIR_CONTENT_FIELDS, Cmir, EmailMessage
from app.services.cmir.merge import merge_with_active
from app.services.cmir.validation import CmirValidator
from app.services.email_reader import GmailImapReader
from app.utils.ids import new_id

logger = logging.getLogger(__name__)

INTERRUPT_KEY = "__interrupt__"

EDITABLE_FIELDS = set(CMIR_CONTENT_FIELDS)

STAGE_BY_INTERRUPT = {
    "missing_mandatory_fields": ("AWAITING_MISSING_FIELDS", "waiting_missing_fields"),
    "approval_required": ("AWAITING_APPROVAL", "waiting_approval"),
}

NODE_BY_INTERRUPT = {
    "missing_mandatory_fields": "collect_missing_fields",
    "approval_required": "review_extracted_cmir",
}

FINAL_STAGE_BY_DECISION = {
    "approve": ("COMPLETED_APPROVED", "completed_approved"),
    "reject": ("COMPLETED_REJECTED", "completed_rejected"),
}

# Reached when persist_cmir's supersede_and_insert loses to a concurrent write
# (see app.repositories.cmir.cmir_record.CmirVersionConflict). The thread closes
# out rather than silently reopening for retry: the reviewer-facing retry UX is
# still an open product question, so this fails loud instead of guessing.
CONFLICT_STAGE = ("COMPLETED_CONFLICT", "completed_conflict")

_AGENT_CODE = "cmir_extractor"
_AGENT_NAME = "CMIR Extraction & Review"
_PROMPT_VERSION = "v1"
_SYSTEM_PROMPT = (
    "Extract structured CMIR fields (sender_type, customer_identity, material_identity, "
    "intent_phrase, existing_cmir_ref, brand, site, target_grd_code, "
    "target_customer_material_ref, effective_date, reason) from an inbound CMIR email."
)


class CmirService:
    """Coordinates PRD API workflows across repositories and LangGraph."""

    def __init__(
        self,
        *,
        email_reader: GmailImapReader,
        graph: Any,
        agent_registry: AgentRegistryRepository,
        agent_runs: AgentRunRepository,
        workflow_threads: WorkflowThreadRepository,
        human_actions: HumanActionRepository,
        cmir_records: CmirRecordRepository,
        job_queue: JobQueueRepository,
        job_run_context: CmirJobRunContextRepository,
        job_item_context: CmirJobItemContextRepository,
        email_repository: EmailRepository | None = None,
        validator: CmirValidator | None = None,
    ) -> None:
        self._email_reader = email_reader
        self._graph = graph
        self._agent_registry = agent_registry
        self._agent_runs = agent_runs
        self._workflow_threads = workflow_threads
        self._human_actions = human_actions
        self._cmir_records = cmir_records
        self._job_queue = job_queue
        self._job_run_context = job_run_context
        self._job_item_context = job_item_context
        self._email_repository = email_repository
        self._validator = validator or CmirValidator()

    def start_email_ingest(
        self,
        *,
        max_workers: int = 4,
        subject_contains: str | None = None,
        unread_only: bool = True,
    ) -> dict[str, Any]:
        """Persist matching emails as queueable rows for the enqueuer function."""
        if self._email_repository is None:
            raise ExternalServiceError(
                code="QUEUE_NOT_CONFIGURED",
                message="Email queue ingestion requires email repository.",
            )

        emails = self._email_reader.fetch_unread(
            subject_contains=subject_contains,
            unread_only=unread_only,
        )
        logger.info("Starting email ingest with %s fetched emails", len(emails))

        run = self._job_queue.create_run(
            job_type=JobTaskType.EMAIL_INGEST,
            trigger_type=JobRunType.ON_DEMAND,
            requested_item_count=len(emails),
        )
        self._job_run_context.create(job_run_id=run["id"], source_type="gmail")

        settings = get_settings()
        threads = []
        enqueued_count = 0
        for email in emails:
            row = self._email_repository.save_for_queue(
                sender=email.sender,
                subject=email.subject,
                raw_content=email.body,
                source_message_id=email.source_message_id,
                source_imap_id=email.imap_id,
            )
            item = self._job_queue.enqueue(
                run["id"],
                item_type=JobTaskType.EMAIL_INGEST,
                dedupe_key=str(row["id"]),
                max_attempts=settings.job_queue.max_attempts,
            )
            if item is not None:
                self._job_item_context.create(job_item_id=item["id"], email_event_id=row["id"])
                enqueued_count += 1
            threads.append(self._queue_summary(str(run["id"]), row, row["queue_status"]))

        self._job_queue.set_requested_item_count(run["id"], enqueued_count)
        has_new = any(thread["status"] == "new" for thread in threads)

        return {
            "batch_id": str(run["id"]),
            "status": "ready_for_queue" if has_new else "no_new_emails",
            "total_threads": len(threads),
            "threads": threads,
        }

    def process_queued_email(
        self,
        *,
        batch_id: str,
        email: dict[str, Any],
        queue_message_id: str,
        email_id: Any | None = None,
    ) -> dict[str, Any]:
        """Process one Service Bus message through the existing CMIR workflow."""
        if email_id is None:
            email_id = email.get("email_id")
        if email_id is None:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="Queued email payload must include email_id.",
                details={"queue_message_id": queue_message_id},
            )
        if not isinstance(email_id, UUID):
            try:
                email_id = UUID(str(email_id))
            except ValueError as exc:
                raise ValidationError(
                    code="VALIDATION_ERROR",
                    message="email_id must be a valid UUID.",
                    details={"email_id": str(email_id), "queue_message_id": queue_message_id},
                ) from exc

        if self._email_repository is not None:
            queue_state = self._email_repository.get_queue_state(email_id)
            if queue_state is None:
                raise NotFoundError(
                    code="EMAIL_NOT_FOUND",
                    message="Unknown email_id.",
                    details={"email_id": str(email_id), "queue_message_id": queue_message_id},
                )
            if queue_state.get("queue_status") == "processed":
                logger.info("Skipping already processed queued email %s", email_id)
                existing_thread = self._workflow_threads.get_latest_by_subject(
                    WorkflowThreadSubjectType.EMAIL_EVENT, email_id
                )
                if existing_thread is not None:
                    return self.get_stage(existing_thread["id"])
                return self._already_processed_summary(batch_id, email_id)
            existing_thread = self._workflow_threads.get_latest_by_subject(
                WorkflowThreadSubjectType.EMAIL_EVENT, email_id
            )
            if existing_thread is not None and existing_thread["status"] != "failed":
                self._email_repository.mark_queue_processed(email_id)
                return self.get_stage(existing_thread["id"])
            self._email_repository.mark_processing(email_id, queue_message_id)

        email_message = self._email_from_payload(email, fallback_imap_id=str(email_id))
        try:
            result = self._process_email_thread(
                batch_id,
                email_message,
                existing_email_id=email_id,
                raise_on_error=True,
            )
            if self._email_repository is not None:
                self._email_repository.mark_queue_processed(email_id)
            return result
        except Exception as exc:
            if self._email_repository is not None:
                self._email_repository.mark_queue_failed(email_id, str(exc), retryable=False)
            raise

    def list_runs(
        self,
        *,
        view: str = "threads",
        status: str | None = None,
        stage: str | None = None,
        agent_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return reviewer thread rows or agent-run summaries.

        `view='threads'` lists `workflow_thread` rows (the reviewer-facing
        queue); `view='agents'` lists `agent_run` rows for one agent instead.
        `cursor` is an opaque ISO 8601 timestamp echoed back from a prior call's
        `next_cursor` for keyset pagination; a malformed one raises
        `ValidationError` rather than a raw `ValueError`. `view='batches'` is
        rejected: batches have no dedicated repository in the current model.
        """
        try:
            if view == "threads":
                items, next_cursor = self._workflow_threads.list_threads(
                    status=status,
                    stage=stage,
                    limit=limit,
                    cursor=cursor,
                )
                return {"items": items, "next_cursor": next_cursor}
            if view == "agents":
                items, next_cursor = self._agent_runs.list_runs(
                    agent_id=agent_id,
                    status=status,
                    limit=limit,
                    cursor=cursor,
                )
                return {"items": items, "next_cursor": next_cursor}
        except ValueError as exc:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="cursor must be an ISO 8601 timestamp, as returned in next_cursor.",
                details={"cursor": cursor},
            ) from exc
        if view == "batches":
            raise ValidationError(
                code="VIEW_NOT_SUPPORTED",
                message=(
                    "view='batches' has no repository support in the process-schema restructure. "
                    "List items for one known job_run_id instead."
                ),
                details={"view": view},
            )
        raise ValidationError(
            code="VALIDATION_ERROR",
            message="view must be 'threads' or 'agents'.",
            details={"view": view},
        )

    def get_stage(self, thread_id: UUID) -> dict[str, Any]:
        """Return current UI stage for one thread.

        Raises `NotFoundError` when `thread_id` doesn't match any
        `workflow_thread` row, so callers never have to null-check the result.
        """
        stage = self._workflow_threads.get_stage(thread_id)
        if stage is None:
            raise self._thread_not_found(thread_id)
        return stage

    def get_snapshot(self, thread_id: UUID) -> dict[str, Any]:
        """Return the CMIR draft the reviewer UI renders while a thread is paused.

        Reflects the thread's most recent interrupt or edit, and carries a diff
        against the current active record once one is available.
        """
        snapshot = self._workflow_threads.get_snapshot(thread_id)
        if snapshot is None:
            raise self._thread_not_found(thread_id)
        return snapshot

    def submit_missing_fields(
        self,
        thread_id: UUID,
        *,
        actor: str,
        fields: dict[str, Any],
        expected_updated_at: str,
    ) -> dict[str, Any]:
        """Resume a thread waiting for mandatory CMIR fields.

        Validates `fields` against the editable-field allowlist and the
        thread's expected `updated_at` (optimistic concurrency) before
        resuming the LangGraph run with them as the interrupt's answer. Raises
        `ConflictError` if the thread has moved on since `expected_updated_at`,
        or isn't actually waiting on this interrupt.
        """
        self._validate_fields(fields)
        stage = self._ensure_current(thread_id, expected_updated_at)
        if stage["status"] != "waiting_missing_fields":
            raise self._thread_not_waiting(thread_id, "waiting_missing_fields", stage["status"])

        pending = self._require_open_pending(thread_id, "missing_mandatory_fields")
        checkpoint_thread_id = self._checkpoint_thread_id(stage)
        try:
            state = self._graph.invoke(
                Command(resume=fields), config=self._thread_config(checkpoint_thread_id)
            )
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc
        try:
            return self._handle_graph_state(
                self._agent_run_id(stage),
                stage["metadata_json"].get("batch_id"),
                checkpoint_thread_id,
                state,
                resume_context={
                    "workflow_thread_id": thread_id,
                    "pending_action_id": pending["id"],
                    "answer": fields,
                    "actor": actor,
                    "action_type": "field_update",
                },
            )
        except AppError:
            raise
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc

    def update_draft(
        self,
        thread_id: UUID,
        *,
        actor: str,
        fields: dict[str, Any],
        expected_updated_at: str,
    ) -> dict[str, Any]:
        """Save reviewer edits while keeping the thread in approval review.

        Re-runs merge_with_active against a freshly-fetched active record on every
        call, not just once at the original interrupt: if this edit touches the
        identity fields, or if the active record changed underneath this thread
        since the diff was last computed, both the diff shown next and the version
        token persist_cmir will later check against need to reflect current reality,
        not a stale snapshot from before this edit.
        """
        self._validate_fields(fields)
        stage = self._ensure_current(thread_id, expected_updated_at)
        if stage["status"] != "waiting_approval":
            raise self._thread_not_waiting(thread_id, "waiting_approval", stage["status"])

        metadata = stage["metadata_json"] or {}
        existing_draft = (metadata.get("latest_snapshot") or {}).get("cmir", {})
        proposed = Cmir(**{**existing_draft, **fields})

        current = self._cmir_records.get_current(
            proposed.customer_identity, proposed.target_customer_material_ref
        )
        merged, diff = merge_with_active(current, proposed)
        validated = self._validator.validate(merged)
        updated_cmir = validated.model_dump()

        checkpoint_thread_id = self._checkpoint_thread_id(stage)
        if hasattr(self._graph, "update_state"):
            self._graph.update_state(
                self._thread_config(checkpoint_thread_id),
                {
                    "cmir": updated_cmir,
                    "existing_cmir": current,
                    "cmir_diff": diff,
                    "cmir_version_token": current["id"] if current else None,
                },
            )
        else:
            logger.warning("Graph does not expose update_state; draft update saved only in repository")

        pending = self._require_open_pending(thread_id, "approval_required")
        new_metadata = {
            **metadata,
            "cmir_status": updated_cmir.get("status"),
            "latest_snapshot": {"cmir": updated_cmir, "existing_cmir": current, "diff": diff},
        }
        try:
            new_action_id = self._human_actions.apply_human_action(
                pending_action_id=pending["id"],
                workflow_thread_id=thread_id,
                response_payload={"fields": fields},
                actor=actor,
                action_type="field_update",
                next_status="waiting_approval",
                next_stage="AWAITING_APPROVAL",
                next_current_node="review_extracted_cmir",
                next_metadata=new_metadata,
                next_pending_interrupt_type="approval_required",
                next_pending_request_payload={
                    "reason": "approval_required",
                    "email_id": str(stage["subject_id"]) if stage["subject_id"] else None,
                    "cmir": updated_cmir,
                    "existing_cmir": current,
                    "diff": diff,
                },
                next_pending_state_snapshot={"cmir": updated_cmir},
            )
        except Exception as exc:
            raise ExternalServiceError(
                code="WORKFLOW_RESUME_FAILED",
                message="Unable to save reviewer update safely.",
                details={
                    "thread_id": str(thread_id),
                    "pending_action_id": str(pending["id"]),
                    "error": str(exc),
                },
            ) from exc
        return {
            "agent_run_id": self._agent_run_id(stage),
            "thread_id": str(thread_id),
            "stage": "AWAITING_APPROVAL",
            "status": "waiting_approval",
            "pending_action_id": new_action_id,
            "message": "Draft saved. Review again.",
        }

    def submit_decision(
        self,
        thread_id: UUID,
        *,
        actor: str,
        decision: str,
        expected_updated_at: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Approve or reject a thread waiting for approval.

        A rejection requires a non-empty `reason`. Resumes the LangGraph run with
        the decision as the interrupt's answer, then persists the terminal thread
        state: COMPLETED_APPROVED, COMPLETED_REJECTED, or COMPLETED_CONFLICT if a
        concurrent write beat this approval to the active CMIR record.
        """
        if decision not in {"approve", "reject"}:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="decision must be 'approve' or 'reject'.",
                details={"decision": decision},
            )
        if decision == "reject" and not reason.strip():
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="Reject requires reason.",
                details={"thread_id": str(thread_id)},
            )

        stage = self._ensure_current(thread_id, expected_updated_at)
        if stage["status"] != "waiting_approval":
            raise self._thread_not_waiting(thread_id, "waiting_approval", stage["status"])

        pending = self._require_open_pending(thread_id, "approval_required")
        answer: dict[str, Any] = {"decision": decision}
        if reason:
            answer["reason"] = reason
        checkpoint_thread_id = self._checkpoint_thread_id(stage)
        try:
            state = self._graph.invoke(
                Command(resume=answer), config=self._thread_config(checkpoint_thread_id)
            )
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc
        try:
            return self._handle_graph_state(
                self._agent_run_id(stage),
                stage["metadata_json"].get("batch_id"),
                checkpoint_thread_id,
                state,
                resume_context={
                    "workflow_thread_id": thread_id,
                    "pending_action_id": pending["id"],
                    "answer": answer,
                    "actor": actor,
                    "action_type": "decision",
                    "decision": decision,
                    "reason": reason or None,
                },
            )
        except AppError:
            raise
        except Exception as exc:
            raise self._resume_failed(thread_id, pending["id"], exc) from exc

    @staticmethod
    def _queue_summary(batch_id: str, row: dict[str, Any], status: str) -> dict[str, Any]:
        """Build the per-email summary row `start_email_ingest` returns for one queued email."""
        stage_by_status = {
            "new": "NEW",
            "queued": "QUEUED",
            "enqueueing": "ENQUEUEING",
            "processing": "PROCESSING",
            "processed": "ALREADY_PROCESSED",
            "failed": "FAILED",
            "queue_failed": "QUEUE_FAILED",
        }
        return {
            "batch_id": batch_id,
            "agent_run_id": None,
            "thread_id": None,
            "email_id": str(row["id"]),
            "source_message_id": row.get("source_message_id"),
            "sender": row.get("sender"),
            "subject": row.get("subject"),
            "stage": stage_by_status.get(status, "INGESTED"),
            "status": status,
            "current_node": None,
            "pending_action_id": None,
            "updated_at": None,
        }

    @staticmethod
    def _already_processed_summary(batch_id: str, email_id: Any) -> dict[str, Any]:
        """Build the summary row returned for an email whose queue state is already 'processed'."""
        return {
            "batch_id": batch_id,
            "agent_run_id": None,
            "thread_id": None,
            "email_id": str(email_id),
            "stage": "ALREADY_PROCESSED",
            "status": "already_processed",
            "current_node": None,
            "pending_action_id": None,
            "updated_at": None,
        }

    @staticmethod
    def _email_from_payload(payload: dict[str, Any], *, fallback_imap_id: str | None = None) -> EmailMessage:
        """Build an `EmailMessage` from a queued Service Bus payload.

        `imap_id` is resolved from whichever of `imap_id`/`source_imap_id`/
        `email_id` is present, falling back to `fallback_imap_id`, since the
        Service Bus consumer's nested `email` payload doesn't always carry the
        same key the top-level request does.
        """
        # email_id is a required top-level field of the request; the nested copy
        # inside `email` is a convenience the Service Bus consumer adds, so fall
        # back to the authoritative value rather than KeyError-ing on its absence.
        imap_id = (
            payload.get("imap_id")
            or payload.get("source_imap_id")
            or payload.get("email_id")
            or fallback_imap_id
        )
        if imap_id is None:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="Queued email payload must include imap_id, source_imap_id, or email_id.",
            )
        return EmailMessage(
            imap_id=str(imap_id),
            sender=payload["sender"],
            subject=payload["subject"],
            body=payload.get("body") or payload.get("raw_content") or "",
            source_message_id=payload.get("source_message_id"),
            mark_read=bool(payload.get("mark_read", True)),
        )

    def _process_email_thread(
        self,
        batch_id: str,
        email: Any,
        *,
        existing_email_id: Any | None = None,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Run one email through its independent agent and LangGraph thread."""
        checkpoint_thread_id = self._new_checkpoint_thread_id()
        agent_id = self._ensure_registered()
        run_id = self._agent_runs.start(agent_id=agent_id, run_type="CMIR_EMAIL_INGEST")
        try:
            email_payload = self._email_to_payload(email)
            initial_state = {
                "batch_id": batch_id,
                "email": email_payload,
                "cmir": {},
                "decision": None,
                "run_id": run_id,
                "thread_id": checkpoint_thread_id,
            }
            if existing_email_id is not None:
                initial_state["email_id"] = existing_email_id
            state = self._graph.invoke(
                initial_state,
                config=self._thread_config(checkpoint_thread_id),
            )
            return self._handle_graph_state(run_id, batch_id, checkpoint_thread_id, state)
        except Exception as exc:
            logger.exception("Workflow run %s failed during ingest", run_id)
            self._agent_runs.update_status(run_id, "failed", error=str(exc), completed=True)
            if raise_on_error:
                raise
            return {
                "batch_id": batch_id,
                "agent_run_id": run_id,
                "thread_id": None,
                "email_id": "",
                "stage": "FAILED",
                "status": "failed",
                "current_node": None,
                "pending_action_id": None,
                "updated_at": None,
            }

    @staticmethod
    def _new_checkpoint_thread_id() -> str:
        """Generate a fresh, unique LangGraph checkpoint thread id for a new run."""
        return new_id("thread")

    def _ensure_registered(self) -> UUID:
        """Ensure the `cmir_extractor` agent row exists for this prompt version and return its id."""
        return self._agent_registry.ensure_registered(
            agent_code=_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=_SYSTEM_PROMPT,
            agent_name=_AGENT_NAME,
            domain="cmir",
        )

    @staticmethod
    def _email_to_payload(email: Any) -> dict[str, Any]:
        """Normalize an `EmailMessage` dataclass or plain dict into a dict for graph state."""
        if isinstance(email, dict):
            return email
        return asdict(email)

    @staticmethod
    def _thread_config(checkpoint_thread_id: str) -> dict[str, Any]:
        """Build the LangGraph `config` dict that pins a graph call to one checkpoint thread."""
        return {"configurable": {"thread_id": checkpoint_thread_id}}

    def _handle_graph_state(
        self,
        run_id: UUID,
        batch_id: str | None,
        checkpoint_thread_id: str,
        state: dict[str, Any],
        *,
        resume_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist thread status after a graph invoke or resume.

        On a fresh interrupt, either creates a new `workflow_thread` or records the
        human action against the existing one, moving the thread into the
        interrupt's waiting stage. Otherwise the graph has run to completion and
        the thread is closed out as approved, rejected, or conflicted. A version
        conflict on `persist_cmir` surfaces as `ConflictError` even though the
        thread bookkeeping itself committed cleanly.
        """
        if state.get(INTERRUPT_KEY):
            payload = state[INTERRUPT_KEY][0].value
            reason = payload["reason"]
            stage, status = STAGE_BY_INTERRUPT[reason]
            email_event_id = state.get("email_id")
            assert email_event_id is not None

            if resume_context is None:
                created = self._workflow_threads.create(
                    stage=stage,
                    subject_type=WorkflowThreadSubjectType.EMAIL_EVENT,
                    subject_id=email_event_id,
                    status=status,
                    current_node=NODE_BY_INTERRUPT[reason],
                    metadata={
                        "checkpoint_thread_id": checkpoint_thread_id,
                        "agent_run_id": str(run_id),
                        "batch_id": batch_id,
                        "cmir_status": state.get("cmir", {}).get("status"),
                        "latest_snapshot": {
                            "cmir": state.get("cmir", {}),
                            "existing_cmir": state.get("existing_cmir"),
                            "diff": state.get("cmir_diff", {}),
                        },
                    },
                )
                workflow_thread_id = created["id"]
                self._human_actions.create_open(
                    reason,
                    payload,
                    workflow_thread_id=workflow_thread_id,
                    agent_run_id=run_id,
                    state_snapshot=self._snapshot_state(state),
                )
                self._agent_runs.update_status(run_id, status)
            else:
                workflow_thread_id = resume_context["workflow_thread_id"]
                thread = self._workflow_threads.get_by_id(workflow_thread_id)
                metadata = {
                    **((thread or {}).get("metadata_json") or {}),
                    "cmir_status": state.get("cmir", {}).get("status"),
                    "latest_snapshot": {
                        "cmir": state.get("cmir", {}),
                        "existing_cmir": state.get("existing_cmir"),
                        "diff": state.get("cmir_diff", {}),
                    },
                }
                self._human_actions.apply_human_action(
                    pending_action_id=resume_context["pending_action_id"],
                    workflow_thread_id=workflow_thread_id,
                    response_payload=resume_context["answer"],
                    actor=resume_context["actor"],
                    decision=resume_context.get("decision"),
                    reason=resume_context.get("reason"),
                    action_type=resume_context["action_type"],
                    next_status=status,
                    next_stage=stage,
                    next_current_node=NODE_BY_INTERRUPT[reason],
                    next_metadata=metadata,
                    next_pending_interrupt_type=reason,
                    next_pending_request_payload=payload,
                    next_pending_state_snapshot=self._snapshot_state(state),
                )
                self._agent_runs.update_status(run_id, status)
            return self.get_stage(workflow_thread_id)

        conflict = state.get("cmir_write_result") == "conflict"
        if conflict:
            stage, status = CONFLICT_STAGE
        else:
            decision = state.get("decision") or ""
            stage, status = FINAL_STAGE_BY_DECISION.get(
                decision, ("COMPLETED_APPROVED", "completed_approved")
            )

        if resume_context is None:
            # Reached only if the graph completes without ever interrupting, which
            # the current shape cannot do because it always interrupts at
            # human_approval. Kept defensive, mirroring PoValidationService.
            self._agent_runs.update_status(run_id, status, completed=True)
            return {
                "batch_id": batch_id,
                "agent_run_id": run_id,
                "thread_id": None,
                "email_id": str(state.get("email_id") or ""),
                "stage": stage,
                "status": status,
                "current_node": None,
                "pending_action_id": None,
                "updated_at": None,
            }

        workflow_thread_id = resume_context["workflow_thread_id"]
        thread = self._workflow_threads.get_by_id(workflow_thread_id)
        metadata = {
            **((thread or {}).get("metadata_json") or {}),
            "cmir_status": state.get("cmir", {}).get("status"),
            "latest_snapshot": {"cmir": state.get("cmir", {})},
        }
        self._human_actions.apply_human_action(
            pending_action_id=resume_context["pending_action_id"],
            workflow_thread_id=workflow_thread_id,
            response_payload=resume_context["answer"],
            actor=resume_context["actor"],
            decision=resume_context.get("decision"),
            reason=resume_context.get("reason"),
            action_type=resume_context["action_type"],
            next_status=status,
            next_stage=stage,
            next_metadata=metadata,
            completed=True,
        )
        self._agent_runs.update_status(run_id, status, completed=True)

        if conflict:
            # The thread bookkeeping above is already committed and consistent. This
            # raise exists so the reviewer who just clicked approve is told their
            # approval did not commit, rather than getting a silent 200.
            raise ConflictError(
                code="CMIR_VERSION_CONFLICT",
                message=(
                    "Another update was approved for this customer/material while this "
                    "thread was pending. Refresh and re-review the current record."
                ),
                details={"thread_id": str(workflow_thread_id)},
            )
        return self.get_stage(workflow_thread_id)

    @staticmethod
    def _thread_not_found(thread_id: UUID) -> NotFoundError:
        """Build the `NotFoundError` raised for an unknown `thread_id`."""
        return NotFoundError(
            code="THREAD_NOT_FOUND",
            message="Unknown thread_id.",
            details={"thread_id": str(thread_id)},
        )

    def _validate_fields(self, fields: dict[str, Any]) -> None:
        """Raise `ValidationError` if `fields` contains a key outside `EDITABLE_FIELDS`."""
        invalid_fields = sorted(set(fields) - EDITABLE_FIELDS)
        if invalid_fields:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="Bad field name.",
                details={"invalid_fields": invalid_fields},
            )

    def _ensure_current(self, thread_id: UUID, expected_updated_at: str) -> dict[str, Any]:
        """Return the thread's current stage, or raise `ConflictError` if it has moved since `expected_updated_at`."""
        stage = self.get_stage(thread_id)
        if stage["updated_at"].isoformat() != expected_updated_at:
            raise ConflictError(
                code="THREAD_STALE",
                message="Thread was updated by another reviewer. Refresh snapshot and retry.",
                details={"thread_id": str(thread_id), "latest_updated_at": stage["updated_at"].isoformat()},
            )
        return stage

    @staticmethod
    def _thread_not_waiting(
        thread_id: UUID,
        expected: str,
        actual: str | None,
    ) -> ConflictError:
        """Build the `ConflictError` raised when a resume API is called against a thread not paused on that interrupt."""
        return ConflictError(
            code="THREAD_NOT_WAITING",
            message="Resume API called while thread is not paused for that action.",
            details={"thread_id": str(thread_id), "expected": expected, "actual": actual},
        )

    def _require_open_pending(self, thread_id: UUID, interrupt_type: str) -> dict[str, Any]:
        """Return the thread's open `human_action` row, or raise if it isn't waiting on `interrupt_type`."""
        pending = self._human_actions.get_open_for_thread(thread_id)
        if pending is None or pending["interrupt_type"] != interrupt_type:
            actual = None if pending is None else pending["interrupt_type"]
            raise self._thread_not_waiting(thread_id, interrupt_type, actual)
        return pending

    @staticmethod
    def _checkpoint_thread_id(stage: dict[str, Any]) -> str:
        """Read the LangGraph checkpoint thread id off a stage's metadata, or raise if it's missing."""
        checkpoint_thread_id = (stage["metadata_json"] or {}).get("checkpoint_thread_id")
        if checkpoint_thread_id is None:
            raise ExternalServiceError(
                code="WORKFLOW_STATE_CORRUPT",
                message="Thread has no checkpoint_thread_id recorded; cannot resume its LangGraph run.",
                details={"thread_id": str(stage["id"])},
            )
        return checkpoint_thread_id

    @staticmethod
    def _resume_failed(thread_id: UUID, pending_action_id: UUID, exc: Exception) -> ExternalServiceError:
        """Log and build the `ExternalServiceError` raised when resuming a thread's graph run fails unexpectedly."""
        logger.exception(
            "Failed to resume workflow thread %s from pending action %s",
            thread_id,
            pending_action_id,
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
    def _agent_run_id(stage: dict[str, Any]) -> UUID:
        """Read the originating agent run id off a stage's metadata, falling back to the thread's own id."""
        agent_run_id = (stage["metadata_json"] or {}).get("agent_run_id")
        return UUID(agent_run_id) if agent_run_id else stage["id"]

    @staticmethod
    def _snapshot_state(state: dict[str, Any]) -> dict[str, Any]:
        """Strip the LangGraph interrupt marker out of graph state before persisting it as a snapshot."""
        return {key: value for key, value in state.items() if key != INTERRUPT_KEY}
