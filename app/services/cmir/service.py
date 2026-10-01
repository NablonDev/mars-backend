"""Coordinates the CMIR resolution workflow's API surface across
repositories and LangGraph.

Entry points:
    start_email_ingest (POST /api/v1/cmir/email-events)
    process_queued_email (POST /api/v1/internal/process-email)
    list_runs (GET /api/v1/workflow-threads)
    get_stage (GET /api/v1/workflow-threads/{thread_id})
    get_snapshot (GET /api/v1/workflow-threads/{thread_id})
    submit_missing_fields (POST /api/v1/workflow-threads/{thread_id}/missing-fields)
    update_draft (PATCH /api/v1/workflow-threads/{thread_id}/draft)
    submit_decision (POST /api/v1/workflow-threads/{thread_id}/decisions)

Was `app/services/cmir_run_service.py` (825 lines). Per the approved plan
("just move it to `app/services/cmir/run_service.py` as-is
(behavior-preserving), do not attempt the internal ingest/thread-resolution
/decision-handling split the plan flags as a *separate* future refactor
call"), this move keeps the same method structure/responsibilities as
before -- it is NOT split into ingest/thread-resolution/decision-handling
files. What DOES have to change, unavoidably, is every repository call
site: `app.repositories.{observability,email,cmir}` (flat modules the old
service imported) no longer exist -- Phase 2 replaced them with
`app.repositories.{process.workflow,process.agent_registry,cmir.email,
cmir.cmir_record}`, a real schema shape change, not just a rename. See this
phase's report for the concrete, load-bearing design decisions this forced:

1. **Checkpoint thread id vs. reviewer-facing thread id.** The old schema's
   `workflow_thread.thread_id` was a single string serving BOTH as the
   LangGraph checkpointer's `configurable.thread_id` AND the reviewer-facing
   API handle. The new `process.workflow_thread.id` is a UUID surrogate
   created only once, lazily, on the thread's first human interrupt (see
   that model's docstring) -- but a LangGraph checkpointer needs a
   `configurable.thread_id` from the very first `graph.invoke()` call,
   before any interrupt (or `workflow_thread` row) exists. Resolution: a
   random checkpoint-thread-id string is generated up front for every graph
   run (`_new_checkpoint_thread_id`), used as the checkpointer key from the
   start; if/when the run's first interrupt fires, the `workflow_thread`
   row is created and the checkpoint string is stashed in its
   `metadata_json["checkpoint_thread_id"]`. Every resume call
   (`submit_missing_fields`/`update_draft`/`submit_decision`) looks the
   reviewer-facing `thread_id` (the row's UUID `id`) up, reads the
   checkpoint string back out of `metadata_json`, and uses THAT for the
   LangGraph `configurable.thread_id`. The two ids are never the same
   value; conflating them would either break the checkpointer (no id until
   an interrupt happens) or break the "lazy creation" model.
2. **`agent_run_id` bridging.** `process.agent_run.workflow_thread_id` is a
   FK set only at `AgentRun` creation time -- but the agent run starts
   BEFORE any `workflow_thread` row exists (same ordering problem as #1).
   Rather than adding a repository method to patch that FK in after the
   fact (out of scope for a services-only phase), the run id is likewise
   stashed in `metadata_json["agent_run_id"]` at thread-creation time and
   read back out on resume. Flagged as a real (if minor) loss versus a
   first-class FK: `AgentRun.workflow_thread_id` stays `NULL` for every
   CMIR run that reaches a thread, so a query keyed off that FK column
   alone won't find it -- only `metadata_json` does.
3. **Audit-trail fidelity.** The old `hitl_actions` table carried an
   explicit `field_changes` diff column per resume; the new merged
   `process.human_action` has no such column (see that model's docstring --
   completing a row already *is* the audit entry). `field_changes` is
   dropped from every resume call here; the diff is still computed and
   shown to the reviewer via `update_draft`'s response and the thread's
   `metadata_json["latest_snapshot"]["diff"]`, it is just no longer
   separately persisted per human-action row.
4. **`list_runs(view="batches")` has no repository support.** The old
   `batch_id` denormalization is gone (see `process.workflow.py`'s module
   docstring); nothing here groups job runs by a listable "batch" concept
   any more (`JobQueueRepository` can summarize/list items for ONE known
   `job_run_id`, but not list job runs themselves). `view="batches"` now
   raises `ValidationError(code="VIEW_NOT_SUPPORTED")` rather than silently
   returning nothing -- a real, flagged capability gap versus the old API,
   not a rename casualty.
5. **`start_email_ingest`/`process_queued_email` now create a real
   `process.job_run`/`job_item` per batch/email**, with the matching
   `cmir.cmir_job_item_context`/`cmir_job_run_context` row attached in the
   same transaction (design point #2) -- the old ad hoc `batch_id` string
   is now a real `job_run.id`.

Session lifecycle (session-lifecycle/persistence fix): every public method
below opens exactly one fresh unit of work -- `self._repos_factory()` for
read-only methods, `self._unit_of_work_factory()` for methods that touch
the LangGraph graph -- via `app.core.container.Container.cmir_repos`/
`cmir_unit_of_work`. That `with` block is the transaction boundary: it
commits on a normal return, rolls back on any exception, and always closes
its Session. No repository/graph reference is ever held on `self` across
calls -- this class holds no per-request state, so one shared instance
(the FastAPI app-lifetime singleton `get_service` returns) is safe to reuse
across concurrent requests. (Previously, `Container.build()` opened one
`Session` for the process's whole lifetime and never committed it --
mutations were visible only within that one never-committed transaction,
never durably persisted. See docs/ARCHITECTURE.md.)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from langgraph.types import Command

from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.enums import JobRunType, JobTaskType, WorkflowThreadSubjectType
from app.schemas.cmir import CMIR_CONTENT_FIELDS, Cmir, EmailMessage
from app.services.cmir.merge import merge_with_active
from app.services.cmir.validation import CmirValidator
from app.services.email_reader import GmailImapReader

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
# (see app.repositories.cmir.cmir_record.CmirVersionConflict). The thread still
# closes out (consistent with approve/reject) rather than silently reopening
# for retry -- the exact reviewer-facing retry UX is an open product question,
# so this is the minimal, safe behavior: fail loud, don't guess.
CONFLICT_STAGE = ("COMPLETED_CONFLICT", "completed_conflict")

_AGENT_CODE = "cmir_extractor"
_AGENT_NAME = "CMIR Extraction & Review"
_PROMPT_VERSION = "v1"
_SYSTEM_PROMPT = (
    "Extract structured CMIR fields (sender_type, customer_identity, material_identity, "
    "intent_phrase, existing_cmir_ref, brand, site, target_grd_code, "
    "target_customer_material_ref, effective_date, reason) from an inbound CMIR email."
)

UnitOfWorkFactory = Callable[[], AbstractContextManager[SimpleNamespace]]


class CmirService:
    """Coordinates PRD API workflows across repositories and LangGraph."""

    def __init__(
        self,
        *,
        email_reader: GmailImapReader,
        repos_factory: UnitOfWorkFactory,
        unit_of_work_factory: UnitOfWorkFactory,
        validator: CmirValidator | None = None,
    ) -> None:
        self._email_reader = email_reader
        self._repos_factory = repos_factory
        self._unit_of_work_factory = unit_of_work_factory
        self._validator = validator or CmirValidator()

    def _ensure_registered(self, agent_registry: Any) -> UUID:
        return agent_registry.ensure_registered(
            agent_code=_AGENT_CODE,
            prompt_version=_PROMPT_VERSION,
            system_prompt=_SYSTEM_PROMPT,
            agent_name=_AGENT_NAME,
            domain="cmir",
        )

    def start_email_ingest(
        self,
        *,
        max_workers: int = 4,
        subject_contains: str | None = None,
        unread_only: bool = True,
    ) -> dict[str, Any]:
        """Persist matching emails as queueable rows for the enqueuer function."""
        with self._unit_of_work_factory() as uow:
            if uow.email_repository is None:
                raise ExternalServiceError(
                    code="QUEUE_NOT_CONFIGURED",
                    message="Email queue ingestion requires email repository.",
                )

            emails = self._email_reader.fetch_unread(
                subject_contains=subject_contains,
                unread_only=unread_only,
            )
            logger.info("Starting email ingest with %s fetched emails", len(emails))

            run = uow.job_queue.create_run(
                job_type=JobTaskType.EMAIL_INGEST,
                trigger_type=JobRunType.ON_DEMAND,
                requested_item_count=len(emails),
            )
            uow.job_run_context.create(job_run_id=run["id"], source_type="gmail")

            settings = get_settings()
            threads = []
            enqueued_count = 0
            for email in emails:
                row = uow.email_repository.save_for_queue(
                    sender=email.sender,
                    subject=email.subject,
                    raw_content=email.body,
                    source_message_id=email.source_message_id,
                    source_imap_id=email.imap_id,
                )
                item = uow.job_queue.enqueue(
                    run["id"],
                    item_type=JobTaskType.EMAIL_INGEST,
                    dedupe_key=str(row["id"]),
                    max_attempts=settings.job_queue.max_attempts,
                )
                if item is not None:
                    uow.job_item_context.create(job_item_id=item["id"], email_event_id=row["id"])
                    enqueued_count += 1
                threads.append(self._queue_summary(str(run["id"]), row, row["queue_status"]))

            uow.job_queue.set_requested_item_count(run["id"], enqueued_count)
            has_new = any(thread["status"] == "new" for thread in threads)

            return {
                "batch_id": str(run["id"]),
                "status": "ready_for_queue" if has_new else "no_new_emails",
                "total_threads": len(threads),
                "threads": threads,
            }

    def list_pending_emails(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Real, currently-computable email queue -- see
        `EmailRepository.list_pending`. Read-only, so this uses
        `self._repos_factory()` like `get_health_snapshot`, not the
        graph-touching `unit_of_work_factory`."""
        with self._repos_factory() as repos:
            if repos.email_repository is None:
                raise ExternalServiceError(
                    code="QUEUE_NOT_CONFIGURED",
                    message="Email queue ingestion requires email repository.",
                )
            return repos.email_repository.list_pending(limit=limit)

    def process_pending_email(self, email_id: UUID) -> dict[str, Any]:
        """UI-triggered equivalent of the Service Bus consumer's
        `process_queued_email`, for a row already sitting in the queue
        (created by the real `start_email_ingest` Gmail read) -- lets a
        reviewer "unqueue"/process one from the UI instead of needing an
        actual Service Bus message to arrive.

        `batch_id` is always a stringified `process.job_run.id` elsewhere in
        this service (see `start_email_ingest`), never a fabricated string,
        so this creates one real, minimal job_run row (ON_DEMAND trigger,
        one requested item) rather than inventing an ad hoc identifier.
        Content (sender/subject/raw_content) is read from the row itself --
        the browser never needs to retype an email's content.
        """
        with self._repos_factory() as repos:
            if repos.email_repository is None:
                raise ExternalServiceError(
                    code="QUEUE_NOT_CONFIGURED",
                    message="Email queue ingestion requires email repository.",
                )
            row = repos.email_repository.get(email_id)
            if row is None:
                raise NotFoundError(
                    code="EMAIL_NOT_FOUND",
                    message="Unknown email_id.",
                    details={"email_id": str(email_id)},
                )
            run = repos.job_queue.create_run(
                job_type=JobTaskType.EMAIL_INGEST,
                trigger_type=JobRunType.ON_DEMAND,
                requested_item_count=1,
            )

        return self.process_queued_email(
            batch_id=str(run["id"]),
            email={
                "sender": row["sender"],
                "subject": row["subject"],
                "raw_content": row["raw_content"],
                "source_message_id": row["source_message_id"],
            },
            queue_message_id=f"manual-{email_id}",
            email_id=email_id,
        )

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

        with self._unit_of_work_factory() as uow:
            if uow.email_repository is not None:
                queue_state = uow.email_repository.get_queue_state(email_id)
                if queue_state is None:
                    raise NotFoundError(
                        code="EMAIL_NOT_FOUND",
                        message="Unknown email_id.",
                        details={"email_id": str(email_id), "queue_message_id": queue_message_id},
                    )
                if queue_state.get("queue_status") == "processed":
                    logger.info("Skipping already processed queued email %s", email_id)
                    existing_thread = uow.workflow_threads.get_latest_by_subject(
                        WorkflowThreadSubjectType.EMAIL_EVENT, email_id
                    )
                    if existing_thread is not None:
                        return self._get_stage(uow.workflow_threads, existing_thread["id"])
                    return self._already_processed_summary(batch_id, email_id)
                existing_thread = uow.workflow_threads.get_latest_by_subject(
                    WorkflowThreadSubjectType.EMAIL_EVENT, email_id
                )
                if existing_thread is not None and existing_thread["status"] != "failed":
                    uow.email_repository.mark_queue_processed(email_id)
                    return self._get_stage(uow.workflow_threads, existing_thread["id"])
                uow.email_repository.mark_processing(email_id, queue_message_id)

            email_message = self._email_from_payload(email, fallback_imap_id=str(email_id))
            try:
                result = self._process_email_thread(
                    uow,
                    batch_id,
                    email_message,
                    existing_email_id=email_id,
                    raise_on_error=True,
                )
                if uow.email_repository is not None:
                    uow.email_repository.mark_queue_processed(email_id)
                return result
            except Exception as exc:
                if uow.email_repository is not None:
                    uow.email_repository.mark_queue_failed(email_id, str(exc), retryable=False)
                raise

    def _process_email_thread(
        self,
        uow: SimpleNamespace,
        batch_id: str,
        email: Any,
        *,
        existing_email_id: Any | None = None,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Run one email through its independent agent and LangGraph thread."""
        checkpoint_thread_id = self._new_checkpoint_thread_id()
        agent_id = self._ensure_registered(uow.agent_registry)
        run_id = uow.agent_runs.start(agent_id=agent_id, run_type="CMIR_EMAIL_INGEST")
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
            state = uow.graph.invoke(
                initial_state,
                config=self._thread_config(checkpoint_thread_id),
            )
            result, conflict_error = self._handle_graph_state(
                uow, run_id, batch_id, checkpoint_thread_id, state
            )
            # resume_context is always None on this ingestion-only path, so
            # _handle_graph_state can never actually produce a conflict here --
            # this mirrors the caller contract for consistency, not a live path.
            if conflict_error is not None:
                raise conflict_error
            return result
        except Exception as exc:
            logger.exception("Workflow run %s failed during ingest", run_id)
            uow.agent_runs.update_status(run_id, "failed", error=str(exc), completed=True)
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
        """Return either reviewer thread rows or agent-run summaries.

        `view="batches"` is no longer supported -- see this module's
        docstring, point 4.
        """
        with self._repos_factory() as repos:
            try:
                if view == "threads":
                    items, next_cursor = repos.workflow_threads.list_threads(
                        status=status,
                        stage=stage,
                        limit=limit,
                        cursor=cursor,
                    )
                    return {"items": items, "next_cursor": next_cursor}
                if view == "agents":
                    items, next_cursor = repos.agent_runs.list_runs(
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
                        "view='batches' has no repository support in the process-schema restructure -- "
                        "list items for one known job_run_id instead."
                    ),
                    details={"view": view},
                )
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="view must be 'threads' or 'agents'.",
                details={"view": view},
            )

    def get_stage(self, thread_id: UUID) -> dict[str, Any]:
        """Return current UI stage for one thread."""
        with self._repos_factory() as repos:
            return self._get_stage(repos.workflow_threads, thread_id)

    def get_snapshot(self, thread_id: UUID) -> dict[str, Any]:
        """Return the latest review snapshot for one thread.

        `editable_fields` is the fixed CMIR content-field set (`CMIR_CONTENT_FIELDS`),
        not a per-stage value -- unlike PO-validation's interrupt-type-driven list,
        it's the same for every CMIR thread, so it's added here rather than stored.
        """
        with self._repos_factory() as repos:
            snapshot = repos.workflow_threads.get_snapshot(thread_id)
            if snapshot is None:
                raise self._thread_not_found(thread_id)
            return {**snapshot, "editable_fields": list(CMIR_CONTENT_FIELDS)}

    def get_health_snapshot(self, *, stale_days: int = 180, attention_limit: int = 50) -> dict[str, Any]:
        """Real, currently-computable CMIR table health -- see
        `CmirRecordRepository.get_health_snapshot`. Read-only, so this uses
        `self._repos_factory()` like `get_stage`/`get_snapshot` above, not
        the graph-touching `unit_of_work_factory`."""
        with self._repos_factory() as repos:
            return repos.cmir_records.get_health_snapshot(
                stale_days=stale_days, attention_limit=attention_limit
            )

    def get_health_trend(self, *, months: int = 6, stale_days: int = 180) -> list[dict[str, Any]]:
        """Real, currently-computable CMIR health trend -- see
        `CmirRecordRepository.get_health_trend`. Read-only, same
        `self._repos_factory()` convention as `get_health_snapshot`."""
        with self._repos_factory() as repos:
            return repos.cmir_records.get_health_trend(months=months, stale_days=stale_days)

    def get_housekeeping_audit_log(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Real, currently-computable CMIR housekeeping audit log -- see
        `CmirRecordRepository.get_housekeeping_audit_log`. Read-only, same
        `self._repos_factory()` convention as `get_health_snapshot`."""
        with self._repos_factory() as repos:
            return repos.cmir_records.get_housekeeping_audit_log(limit=limit)

    def submit_missing_fields(
        self,
        thread_id: UUID,
        *,
        actor: str,
        fields: dict[str, Any],
        expected_updated_at: str,
    ) -> dict[str, Any]:
        """Resume a thread waiting for mandatory CMIR fields."""
        self._validate_fields(fields)
        persist_args: dict[str, Any] | None = None
        result: dict[str, Any]
        conflict_error: ConflictError | None
        try:
            with self._unit_of_work_factory() as uow:
                stage = self._ensure_current(uow.workflow_threads, thread_id, expected_updated_at)
                if stage["status"] != "waiting_missing_fields":
                    raise self._thread_not_waiting(thread_id, "waiting_missing_fields", stage["status"])

                pending = self._require_open_pending(uow.human_actions, thread_id, "missing_mandatory_fields")
                checkpoint_thread_id = self._checkpoint_thread_id(stage)
                try:
                    state = uow.graph.invoke(
                        Command(resume=fields), config=self._thread_config(checkpoint_thread_id)
                    )
                except Exception as exc:
                    raise self._resume_failed(thread_id, pending["id"], exc) from exc

                persist_args = {
                    "run_id": self._agent_run_id(stage),
                    "batch_id": stage["metadata_json"].get("batch_id"),
                    "checkpoint_thread_id": checkpoint_thread_id,
                    "state": state,
                    "resume_context": {
                        "workflow_thread_id": thread_id,
                        "pending_action_id": pending["id"],
                        "answer": fields,
                        "actor": actor,
                        "action_type": "field_update",
                    },
                }
                # A.2: any exception here (other than the AppError codepaths
                # above) propagates through this ENTIRE `with` block, letting
                # Database.session()'s own rollback run on the original
                # Session BEFORE the retry below ever opens a fresh one --
                # never caught/retried from inside this block.
                result, conflict_error = self._handle_graph_state(uow, **persist_args)
        except AppError:
            raise
        except Exception as first_exc:  # noqa: BLE001 -- A.2 retry must catch any DB-layer failure type
            assert persist_args is not None  # graph.invoke() must have succeeded to reach here
            result, conflict_error = self._retry_persist_or_raise_corrupt(
                lambda fresh_uow: self._handle_graph_state(fresh_uow, **persist_args),
                thread_id=thread_id,
                pending_action_id=persist_args["resume_context"]["pending_action_id"],
                checkpoint_thread_id=persist_args["checkpoint_thread_id"],
                operation_name="submit_missing_fields",
                first_exc=first_exc,
            )

        if conflict_error is not None:
            raise conflict_error
        return result

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
        apply_kwargs: dict[str, Any] | None = None
        agent_run_id: UUID | None = None
        new_action_id: UUID | None = None
        try:
            with self._unit_of_work_factory() as uow:
                stage = self._ensure_current(uow.workflow_threads, thread_id, expected_updated_at)
                if stage["status"] != "waiting_approval":
                    raise self._thread_not_waiting(thread_id, "waiting_approval", stage["status"])

                metadata = stage["metadata_json"] or {}
                existing_draft = (metadata.get("latest_snapshot") or {}).get("cmir", {})
                proposed = Cmir(**{**existing_draft, **fields})

                current = uow.cmir_records.get_current(
                    proposed.customer_identity, proposed.target_customer_material_ref
                )
                merged, diff = merge_with_active(current, proposed)
                validated = self._validator.validate(merged)
                updated_cmir = validated.model_dump()

                checkpoint_thread_id = self._checkpoint_thread_id(stage)
                if hasattr(uow.graph, "update_state"):
                    uow.graph.update_state(
                        self._thread_config(checkpoint_thread_id),
                        {
                            "cmir": updated_cmir,
                            "existing_cmir": current,
                            "cmir_diff": diff,
                            "cmir_version_token": current["id"] if current else None,
                        },
                    )
                else:
                    logger.warning(
                        "Graph does not expose update_state; draft update saved only in repository"
                    )

                pending = self._require_open_pending(uow.human_actions, thread_id, "approval_required")
                new_metadata = {
                    **metadata,
                    "cmir_status": updated_cmir.get("status"),
                    "latest_snapshot": {
                        "cmir": updated_cmir,
                        "existing_cmir": current,
                        "diff": diff,
                        # No LangGraph state here (this is a pure metadata patch, not a graph
                        # invocation) -- carry the previously-stored email forward unchanged,
                        # since a draft edit never touches email data.
                        "email": (metadata.get("latest_snapshot") or {}).get("email"),
                    },
                }
                agent_run_id = self._agent_run_id(stage)
                apply_kwargs = {
                    "pending_action_id": pending["id"],
                    "workflow_thread_id": thread_id,
                    "response_payload": {"fields": fields},
                    "actor": actor,
                    "action_type": "field_update",
                    "next_status": "waiting_approval",
                    "next_stage": "AWAITING_APPROVAL",
                    "next_current_node": "review_extracted_cmir",
                    "next_metadata": new_metadata,
                    "next_pending_interrupt_type": "approval_required",
                    "next_pending_request_payload": {
                        "reason": "approval_required",
                        "email_id": str(stage["email_event_id"]) if stage["email_event_id"] else None,
                        "cmir": updated_cmir,
                        "existing_cmir": current,
                        "diff": diff,
                    },
                    "next_pending_state_snapshot": {"cmir": updated_cmir},
                }
                # A.2: `graph.update_state()` above has already committed the
                # checkpoint (autocommit) by this point -- any non-AppError
                # exception from apply_human_action must propagate through
                # this ENTIRE `with` block (correct rollback of the original
                # Session) rather than being caught here, so the retry below
                # never touches the failed Session.
                new_action_id = uow.human_actions.apply_human_action(**apply_kwargs)
        except AppError:
            # B fix (narrow scope): a concurrent-loser ConflictError from
            # apply_human_action must reach the caller as-is (clean 409).
            raise
        except Exception as first_exc:  # noqa: BLE001 -- A.2 retry must catch any DB-layer failure type
            assert apply_kwargs is not None  # update_state() must have succeeded to reach here
            new_action_id = self._retry_persist_or_raise_corrupt(
                lambda fresh_uow: fresh_uow.human_actions.apply_human_action(**apply_kwargs),
                thread_id=thread_id,
                pending_action_id=apply_kwargs["pending_action_id"],
                checkpoint_thread_id=checkpoint_thread_id,
                operation_name="update_draft",
                first_exc=first_exc,
            )

        return {
            "agent_run_id": agent_run_id,
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
        """Approve or reject a thread waiting for approval."""
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

        persist_args: dict[str, Any] | None = None
        result: dict[str, Any]
        conflict_error: ConflictError | None
        try:
            with self._unit_of_work_factory() as uow:
                stage = self._ensure_current(uow.workflow_threads, thread_id, expected_updated_at)
                if stage["status"] != "waiting_approval":
                    raise self._thread_not_waiting(thread_id, "waiting_approval", stage["status"])

                pending = self._require_open_pending(uow.human_actions, thread_id, "approval_required")
                answer: dict[str, Any] = {"decision": decision}
                if reason:
                    answer["reason"] = reason
                checkpoint_thread_id = self._checkpoint_thread_id(stage)
                try:
                    state = uow.graph.invoke(
                        Command(resume=answer), config=self._thread_config(checkpoint_thread_id)
                    )
                except Exception as exc:
                    raise self._resume_failed(thread_id, pending["id"], exc) from exc

                persist_args = {
                    "run_id": self._agent_run_id(stage),
                    "batch_id": stage["metadata_json"].get("batch_id"),
                    "checkpoint_thread_id": checkpoint_thread_id,
                    "state": state,
                    "resume_context": {
                        "workflow_thread_id": thread_id,
                        "pending_action_id": pending["id"],
                        "answer": answer,
                        "actor": actor,
                        "action_type": "decision",
                        "decision": decision,
                        "reason": reason or None,
                    },
                }
                # A.2: let any non-AppError exception propagate through this
                # ENTIRE `with` block (correct rollback of the original
                # Session) rather than catching it here -- retried outside.
                result, conflict_error = self._handle_graph_state(uow, **persist_args)
        except AppError:
            raise
        except Exception as first_exc:  # noqa: BLE001 -- A.2 retry must catch any DB-layer failure type
            assert persist_args is not None  # graph.invoke() must have succeeded to reach here
            result, conflict_error = self._retry_persist_or_raise_corrupt(
                lambda fresh_uow: self._handle_graph_state(fresh_uow, **persist_args),
                thread_id=thread_id,
                pending_action_id=persist_args["resume_context"]["pending_action_id"],
                checkpoint_thread_id=persist_args["checkpoint_thread_id"],
                operation_name="submit_decision",
                first_exc=first_exc,
            )

        # Outside the unit-of-work block, already committed -- raising here
        # (Design A.1) cannot roll back the bookkeeping just applied, unlike
        # raising from inside the `with` above, which would trigger a rollback.
        if conflict_error is not None:
            raise conflict_error
        return result

    def _handle_graph_state(
        self,
        uow: SimpleNamespace,
        run_id: UUID,
        batch_id: str | None,
        checkpoint_thread_id: str,
        state: dict[str, Any],
        *,
        resume_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], ConflictError | None]:
        """Persist thread status after a graph invoke or resume.

        Returns (result, pending_conflict). pending_conflict is None on every
        path except the CMIR SCD2-write-conflict branch below: the reviewer's
        thread/human_action/agent_run bookkeeping is a real, separate fact
        that must still commit (Design A.1 fix) even though the CMIR content
        write itself lost the race -- so the ConflictError is returned for the
        caller to raise AFTER its own unit-of-work has committed, instead of
        being raised from inside this method (which previously triggered a
        rollback of the bookkeeping that had just been applied, contradicting
        this function's own intent -- see docs/implementation-progress.md
        Phase 2).
        """
        if state.get(INTERRUPT_KEY):
            payload = state[INTERRUPT_KEY][0].value
            reason = payload["reason"]
            stage, status = STAGE_BY_INTERRUPT[reason]
            email_event_id = state.get("email_id")
            assert email_event_id is not None

            if resume_context is None:
                created = uow.workflow_threads.create(
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
                            "email": self._email_snapshot(state),
                        },
                    },
                )
                workflow_thread_id = created["id"]
                uow.human_actions.create_open(
                    reason,
                    payload,
                    workflow_thread_id=workflow_thread_id,
                    agent_run_id=run_id,
                    state_snapshot=self._snapshot_state(state),
                )
                uow.agent_runs.update_status(run_id, status)
            else:
                workflow_thread_id = resume_context["workflow_thread_id"]
                thread = uow.workflow_threads.get_by_id(workflow_thread_id)
                metadata = {
                    **((thread or {}).get("metadata_json") or {}),
                    "cmir_status": state.get("cmir", {}).get("status"),
                    "latest_snapshot": {
                        "cmir": state.get("cmir", {}),
                        "existing_cmir": state.get("existing_cmir"),
                        "diff": state.get("cmir_diff", {}),
                        "email": self._email_snapshot(state),
                    },
                }
                uow.human_actions.apply_human_action(
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
                uow.agent_runs.update_status(run_id, status)
            return self._get_stage(uow.workflow_threads, workflow_thread_id), None

        conflict = state.get("cmir_write_result") == "conflict"
        if conflict:
            stage, status = CONFLICT_STAGE
        else:
            decision = state.get("decision") or ""
            stage, status = FINAL_STAGE_BY_DECISION.get(
                decision, ("COMPLETED_APPROVED", "completed_approved")
            )

        if resume_context is None:
            # Reached only if the graph completes without ever interrupting
            # (not possible on the current graph shape, which always
            # interrupts at human_approval -- kept defensive, mirrors
            # PoValidationService's own "touchless path").
            uow.agent_runs.update_status(run_id, status, completed=True)
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
            }, None

        workflow_thread_id = resume_context["workflow_thread_id"]
        thread = uow.workflow_threads.get_by_id(workflow_thread_id)
        metadata = {
            **((thread or {}).get("metadata_json") or {}),
            "cmir_status": state.get("cmir", {}).get("status"),
            "latest_snapshot": {"cmir": state.get("cmir", {}), "email": self._email_snapshot(state)},
        }
        uow.human_actions.apply_human_action(
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
        uow.agent_runs.update_status(run_id, status, completed=True)

        result = self._get_stage(uow.workflow_threads, workflow_thread_id)
        if conflict:
            # The thread bookkeeping above is real and must commit (mirrors
            # approve/reject's own close-out) -- the reviewer's action happened
            # and is durable even though the CMIR content write itself lost the
            # SCD2 race. This ConflictError is returned, not raised here, so the
            # caller can raise it AFTER its own unit-of-work commits, giving the
            # reviewer honest feedback that the underlying record did not change,
            # without rolling back the bookkeeping just applied above.
            return result, ConflictError(
                code="CMIR_VERSION_CONFLICT",
                message=(
                    "Another update was approved for this customer/material while this "
                    "thread was pending. Refresh and re-review the current record."
                ),
                details={"thread_id": str(workflow_thread_id)},
            )
        return result, None

    def _get_stage(self, workflow_threads: Any, thread_id: UUID) -> dict[str, Any]:
        stage = workflow_threads.get_stage(thread_id)
        if stage is None:
            raise self._thread_not_found(thread_id)
        return stage

    def _require_open_pending(
        self, human_actions: Any, thread_id: UUID, interrupt_type: str
    ) -> dict[str, Any]:
        pending = human_actions.get_open_for_thread(thread_id)
        if pending is None or pending["interrupt_type"] != interrupt_type:
            actual = None if pending is None else pending["interrupt_type"]
            raise self._thread_not_waiting(thread_id, interrupt_type, actual)
        return pending

    def _ensure_current(
        self, workflow_threads: Any, thread_id: UUID, expected_updated_at: str
    ) -> dict[str, Any]:
        stage = self._get_stage(workflow_threads, thread_id)
        if stage["updated_at"].isoformat() != expected_updated_at:
            raise ConflictError(
                code="THREAD_STALE",
                message="Thread was updated by another reviewer. Refresh snapshot and retry.",
                details={"thread_id": str(thread_id), "latest_updated_at": stage["updated_at"].isoformat()},
            )
        return stage

    def _validate_fields(self, fields: dict[str, Any]) -> None:
        invalid_fields = sorted(set(fields) - EDITABLE_FIELDS)
        if invalid_fields:
            raise ValidationError(
                code="VALIDATION_ERROR",
                message="Bad field name.",
                details={"invalid_fields": invalid_fields},
            )

    @staticmethod
    def _checkpoint_thread_id(stage: dict[str, Any]) -> str:
        checkpoint_thread_id = (stage["metadata_json"] or {}).get("checkpoint_thread_id")
        if checkpoint_thread_id is None:
            raise ExternalServiceError(
                code="WORKFLOW_STATE_CORRUPT",
                message="Thread has no checkpoint_thread_id recorded; cannot resume its LangGraph run.",
                details={"thread_id": str(stage["id"])},
            )
        return checkpoint_thread_id

    @staticmethod
    def _agent_run_id(stage: dict[str, Any]) -> UUID:
        agent_run_id = (stage["metadata_json"] or {}).get("agent_run_id")
        return UUID(agent_run_id) if agent_run_id else stage["id"]

    @staticmethod
    def _thread_config(checkpoint_thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": checkpoint_thread_id}}

    @staticmethod
    def _email_to_payload(email: Any) -> dict[str, Any]:
        if isinstance(email, dict):
            return email
        return asdict(email)

    @staticmethod
    def _email_snapshot(state: dict[str, Any]) -> dict[str, Any]:
        """Reviewer-facing email fields for `metadata_json["latest_snapshot"]`.

        Deliberately excludes `email_id` -- `state["email"]` (`EmailMessage`) never
        carries it; the durable id is the sibling graph-state key `email_id`,
        surfaced to the API as `workflow_thread.email_event_id`. Also excludes
        `imap_id`/`body`/`mark_read`: `body` especially must not land in a JSONB
        column read by every list/snapshot query.
        """
        email = state.get("email") or {}
        return {
            "sender": email.get("sender"),
            "subject": email.get("subject"),
            "source_message_id": email.get("source_message_id"),
        }

    @staticmethod
    def _email_from_payload(payload: dict[str, Any], *, fallback_imap_id: str | None = None) -> EmailMessage:
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

    @staticmethod
    def _already_processed_summary(batch_id: str, email_id: Any) -> dict[str, Any]:
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
    def _queue_summary(batch_id: str, row: dict[str, Any], status: str) -> dict[str, Any]:
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
    def _new_checkpoint_thread_id() -> str:
        return f"thread_{uuid4().hex[:12]}"

    @staticmethod
    def _snapshot_state(state: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in state.items() if key != INTERRUPT_KEY}

    @staticmethod
    def _thread_not_found(thread_id: UUID) -> NotFoundError:
        return NotFoundError(
            code="THREAD_NOT_FOUND",
            message="Unknown thread_id.",
            details={"thread_id": str(thread_id)},
        )

    @staticmethod
    def _resume_failed(thread_id: UUID, pending_action_id: UUID, exc: Exception) -> ExternalServiceError:
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

    def _retry_persist_or_raise_corrupt(
        self,
        operation: Callable[[SimpleNamespace], Any],
        *,
        thread_id: UUID,
        pending_action_id: UUID,
        checkpoint_thread_id: str,
        operation_name: str,
        first_exc: Exception,
    ) -> Any:
        """A.2: `operation` is a DB-persistence-only step whose graph/checkpoint
        work has ALREADY succeeded and committed (via LangGraph's own
        autocommit connection) by the time this is called -- `operation` must
        never touch `uow.graph`/re-invoke the graph, only repositories.

        The caller's own `with self._unit_of_work_factory() as uow:` block has
        already let `first_exc` propagate all the way through it before
        reaching here, so `Database.session()`'s own `except Exception:
        session.rollback()` has already run on the ORIGINAL session -- this
        method never touches that session again, only opens a completely
        fresh one via `self._repos_factory()` (no graph needed) for the single
        retry attempt.

        If the retry also fails, raises `ExternalServiceError` with
        `code="WORKFLOW_STATE_CORRUPT"` (the A.2 invariant: an ordinary DB
        failure after a successful checkpoint write must be retried once and,
        if still unresolved, surfaced loudly and logged for manual
        reconciliation -- never silently, never as the generic
        WORKFLOW_RESUME_FAILED). Never claims true atomicity -- see
        docs/implementation-progress.md's A.2 section for the documented
        residual risk (a hard process crash between the checkpoint commit and
        the DB commit, which no in-process retry can address).

        An `AppError` from the retry (e.g. a genuine `ConflictError` from
        `HumanActionRepository.apply_human_action`'s guard, per B) is not this
        method's concern to retry against -- it propagates unchanged.
        """
        logger.error(
            "A.2: DB persistence failed after a successful graph invoke/checkpoint "
            "write; retrying once with a fresh Session (never re-invoking the graph). "
            "thread_id=%s pending_action_id=%s checkpoint_thread_id=%s operation=%s error=%s",
            thread_id,
            pending_action_id,
            checkpoint_thread_id,
            operation_name,
            first_exc,
        )
        try:
            with self._repos_factory() as fresh_uow:
                return operation(fresh_uow)
        except AppError:
            raise
        except Exception as retry_exc:
            logger.critical(
                "A.2: DB persistence retry ALSO failed after a successful graph invoke/"
                "checkpoint write -- workflow state has diverged from the checkpoint and "
                "requires manual reconciliation. thread_id=%s pending_action_id=%s "
                "checkpoint_thread_id=%s operation=%s first_error=%s retry_error=%s",
                thread_id,
                pending_action_id,
                checkpoint_thread_id,
                operation_name,
                first_exc,
                retry_exc,
            )
            raise ExternalServiceError(
                code="WORKFLOW_STATE_CORRUPT",
                message=(
                    "The reviewer's action was recorded in the workflow checkpoint, but the "
                    "application database could not be updated to match, even after a retry. "
                    "This thread's state may be inconsistent and requires manual reconciliation."
                ),
                details={
                    "thread_id": str(thread_id),
                    "pending_action_id": str(pending_action_id),
                    "checkpoint_thread_id": checkpoint_thread_id,
                    "operation": operation_name,
                },
            ) from retry_exc

    @staticmethod
    def _thread_not_waiting(
        thread_id: UUID,
        expected: str,
        actual: str | None,
    ) -> ConflictError:
        return ConflictError(
            code="THREAD_NOT_WAITING",
            message="Resume API called while thread is not paused for that action.",
            details={"thread_id": str(thread_id), "expected": expected, "actual": actual},
        )
