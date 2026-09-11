"""Repository for process schema workflow and error tracking tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models import HumanAction, ProcessingError, WorkflowThread, WorkflowThreadSubject
from app.utils.pagination import next_cursor_from_page, parse_cursor


def _thread_to_dict(thread: WorkflowThread, subject: WorkflowThreadSubject | None) -> dict:
    """Project a `WorkflowThread` row and its 1:1 subject row onto one caller-facing dict."""
    return {
        "id": thread.id,
        "job_item_id": thread.job_item_id,
        "status": thread.status,
        "stage": thread.stage,
        "current_node": thread.current_node,
        "completed_at": thread.completed_at,
        "error": thread.error,
        "metadata_json": thread.metadata_json,
        "subject_type": subject.subject_type if subject is not None else None,
        "subject_id": subject.subject_id if subject is not None else None,
        "updated_at": thread.updated_at,
    }


def _human_action_to_dict(row: HumanAction) -> dict:
    """Project a `HumanAction` row onto the plain dict shape returned to callers."""
    return {
        "id": row.id,
        "job_item_id": row.job_item_id,
        "workflow_thread_id": row.workflow_thread_id,
        "agent_run_id": row.agent_run_id,
        "action_type": row.action_type,
        "interrupt_type": row.interrupt_type,
        "request_payload": row.request_payload,
        "state_snapshot": row.state_snapshot,
        "status": row.status,
        "response_payload": row.response_payload,
        "decision": row.decision,
        "reason": row.reason,
        "actor": row.actor,
        "requested_at": row.requested_at,
        "responded_at": row.responded_at,
    }


def _processing_error_to_dict(row: ProcessingError) -> dict:
    """Project a `ProcessingError` row onto the plain dict shape returned to callers."""
    return {
        "id": row.id,
        "job_item_id": row.job_item_id,
        "agent_run_id": row.agent_run_id,
        "purchase_order_line_id": row.purchase_order_line_id,
        "error_type": row.error_type,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "node_name": row.node_name,
        "raw_error_detail": row.raw_error_detail,
        "occurred_at": row.occurred_at,
        "resolved": row.resolved,
        "resolved_at": row.resolved_at,
        "resolved_by": row.resolved_by,
    }


class WorkflowThreadRepository:
    """One LangGraph run's persisted thread state, shared by CMIR, PO validation, and rule extraction.

    A thread's identity (which email, PO line, or retailer agreement it concerns) lives
    in the 1:1 `WorkflowThreadSubject` row rather than on `WorkflowThread` itself, as a
    polymorphic `subject_type`/`subject_id` pair rather than a typed FK per domain.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _get_subject(self, workflow_thread_id: UUID) -> WorkflowThreadSubject | None:
        """Fetch a thread's 1:1 subject row, looked up manually since it is no relationship."""
        return self._session.get(WorkflowThreadSubject, workflow_thread_id)

    def create(
        self,
        stage: str,
        *,
        subject_type: str,
        subject_id: UUID,
        job_item_id: UUID | None = None,
        status: str = "running",
        current_node: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Create a thread and its 1:1 subject row together."""
        thread = WorkflowThread(
            job_item_id=job_item_id,
            status=status,
            stage=stage,
            current_node=current_node,
            metadata_json=metadata or {},
        )
        self._session.add(thread)
        self._session.flush()

        subject = WorkflowThreadSubject(
            workflow_thread_id=thread.id,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        self._session.add(subject)
        self._session.flush()

        return _thread_to_dict(thread, subject)

    def get_by_id(self, workflow_thread_id: UUID) -> dict | None:
        """Return a thread by id joined with its subject, or None if it doesn't exist."""
        thread = self._session.get(WorkflowThread, workflow_thread_id)
        if thread is None:
            return None
        return _thread_to_dict(thread, self._get_subject(workflow_thread_id))

    def get_latest_by_subject(self, subject_type: str, subject_id: UUID) -> dict | None:
        """Return the most recently updated thread for `(subject_type, subject_id)`, or None."""
        # SQLite's func.now() has only second resolution, so threads created within
        # one second tie on updated_at; the id is a time-ordered UUIDv7, which breaks
        # the tie deterministically.
        row = self._session.scalars(
            select(WorkflowThreadSubject)
            .join(WorkflowThread, WorkflowThread.id == WorkflowThreadSubject.workflow_thread_id)
            .where(
                WorkflowThreadSubject.subject_type == subject_type,
                WorkflowThreadSubject.subject_id == subject_id,
            )
            .order_by(WorkflowThread.updated_at.desc(), WorkflowThread.id.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        thread = self._session.get(WorkflowThread, row.workflow_thread_id)
        return _thread_to_dict(thread, row) if thread is not None else None

    def list_threads(
        self,
        *,
        status: str | None = None,
        stage: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Page through threads, filtered by status and/or stage, newest-updated first.

        Returns the page alongside a cursor for the next page, or `None`
        once the page is short of `limit` (no more results).
        """
        stmt = select(WorkflowThread)
        if status is not None:
            stmt = stmt.where(WorkflowThread.status == status)
        if stage is not None:
            stmt = stmt.where(WorkflowThread.stage == stage)
        if cursor is not None:
            stmt = stmt.where(WorkflowThread.updated_at < parse_cursor(cursor))
        stmt = stmt.order_by(WorkflowThread.updated_at.desc(), WorkflowThread.id.desc()).limit(limit)

        rows = self._session.scalars(stmt).all()
        items = [_thread_to_dict(r, self._get_subject(r.id)) for r in rows]
        next_cursor = next_cursor_from_page(items, limit)
        return items, next_cursor

    def update_status(
        self,
        workflow_thread_id: UUID,
        *,
        status: str,
        stage: str,
        current_node: str | None = None,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> None:
        """Unconditionally advance a thread's status/stage; a no-op if the thread no longer exists.

        Unlike `update_if_current`, this does not guard against a concurrent writer
        having moved the thread, so use it only where the caller already owns the
        thread exclusively, such as the single worker driving one LangGraph run.
        """
        thread = self._session.get(WorkflowThread, workflow_thread_id)
        if thread is None:
            return

        thread.status = status
        thread.stage = stage
        if completed:
            thread.current_node = None
            thread.completed_at = func.now()
        elif current_node is not None:
            thread.current_node = current_node
        if metadata is not None:
            thread.metadata_json = metadata
        if error is not None:
            thread.error = error
        self._session.flush()

    def update_if_current(
        self,
        workflow_thread_id: UUID,
        *,
        expected_updated_at: datetime,
        status: str,
        stage: str,
        current_node: str | None = None,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> bool:
        """Optimistic-concurrency update, guarded on `updated_at`.

        The `UPDATE` only matches a row whose `updated_at` still equals
        `expected_updated_at`; returns `True` if it hit exactly one row and
        `False` if another writer already advanced the thread since the
        caller last read it (or the thread doesn't exist). Callers that get
        `False` back are expected to re-read and retry rather than assume
        the update went through, unlike `update_status`, which always writes.
        """
        values: dict[str, Any] = {"status": status, "stage": stage, "updated_at": func.now()}
        if completed:
            values["current_node"] = None
            values["completed_at"] = func.now()
        elif current_node is not None:
            values["current_node"] = current_node
        if metadata is not None:
            values["metadata_json"] = metadata
        if error is not None:
            values["error"] = error

        result = cast(
            CursorResult,
            self._session.execute(
                update(WorkflowThread)
                .where(
                    WorkflowThread.id == workflow_thread_id,
                    WorkflowThread.updated_at == expected_updated_at,
                )
                .values(**values)
            ),
        )
        self._session.flush()
        return result.rowcount == 1

    def get_stage(self, workflow_thread_id: UUID) -> dict | None:
        """Alias for `get_by_id`, kept as its own name for call sites that only care about stage/status."""
        return self.get_by_id(workflow_thread_id)

    def get_snapshot(self, workflow_thread_id: UUID) -> dict | None:
        """Return a thread's current state plus its full human-action history, oldest first."""
        thread = self._session.get(WorkflowThread, workflow_thread_id)
        if thread is None:
            return None

        subject = self._get_subject(workflow_thread_id)
        history_rows = self._session.scalars(
            select(HumanAction)
            .where(HumanAction.workflow_thread_id == workflow_thread_id)
            .order_by(HumanAction.requested_at.asc())
        ).all()

        return {
            **_thread_to_dict(thread, subject),
            "history": [
                {
                    "actor": row.actor,
                    "action_type": row.action_type,
                    "decision": row.decision,
                    "response_payload": row.response_payload,
                    "responded_at": row.responded_at,
                }
                for row in history_rows
            ],
        }


class HumanActionRepository:
    """Human-in-the-loop interrupts and their responses.

    One row is both the pending request and, once answered, the audit-trail entry.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_open(
        self,
        interrupt_type: str,
        request_payload: dict[str, Any],
        *,
        job_item_id: UUID | None = None,
        workflow_thread_id: UUID | None = None,
        agent_run_id: UUID | None = None,
        action_type: str | None = None,
        state_snapshot: dict[str, Any] | None = None,
    ) -> UUID:
        """Open a pending human action awaiting a response, returning its id."""
        row = HumanAction(
            job_item_id=job_item_id,
            workflow_thread_id=workflow_thread_id,
            agent_run_id=agent_run_id,
            action_type=action_type,
            interrupt_type=interrupt_type,
            request_payload=request_payload,
            state_snapshot=state_snapshot,
            status="open",
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def complete(
        self,
        action_id: UUID,
        *,
        response_payload: dict[str, Any],
        actor: str,
        decision: str | None = None,
        reason: str | None = None,
    ) -> dict | None:
        """Answer an open human action, returning None if it's already been completed or doesn't exist."""
        row = self._session.scalars(
            select(HumanAction).where(HumanAction.id == action_id, HumanAction.status == "open")
        ).first()
        if row is None:
            return None

        row.status = "completed"
        row.response_payload = response_payload
        row.decision = decision
        row.reason = reason
        row.actor = actor
        row.responded_at = func.now()
        self._session.flush()
        return _human_action_to_dict(row)

    def get_open_for_thread(self, workflow_thread_id: UUID) -> dict | None:
        """Return the one still-open human action for a thread, or None if there isn't one."""
        row = self._session.scalars(
            select(HumanAction).where(
                and_(HumanAction.workflow_thread_id == workflow_thread_id, HumanAction.status == "open")
            )
        ).first()
        return _human_action_to_dict(row) if row is not None else None

    def list_for_thread(self, workflow_thread_id: UUID) -> list[dict]:
        """Return every human action (open and completed) for a thread, in the order they were requested."""
        rows = self._session.scalars(
            select(HumanAction)
            .where(HumanAction.workflow_thread_id == workflow_thread_id)
            .order_by(HumanAction.requested_at.asc())
        ).all()
        return [_human_action_to_dict(r) for r in rows]

    def apply_human_action(
        self,
        *,
        pending_action_id: UUID,
        workflow_thread_id: UUID,
        response_payload: dict[str, Any],
        actor: str,
        decision: str | None = None,
        reason: str | None = None,
        action_type: str | None = None,
        next_status: str,
        next_stage: str,
        next_current_node: str | None = None,
        next_metadata: dict[str, Any] | None = None,
        next_pending_interrupt_type: str | None = None,
        next_pending_request_payload: dict[str, Any] | None = None,
        next_pending_state_snapshot: dict[str, Any] | None = None,
        completed: bool = False,
    ) -> UUID | None:
        """Complete a thread's open action, optionally open the next, and transition it.

        Completing the row is itself the audit-trail entry, so no separate audit write
        happens. Raises `ValueError` when no open action matches
        `(pending_action_id, workflow_thread_id)`, leaving the thread untouched.
        """
        result = cast(
            CursorResult,
            self._session.execute(
                update(HumanAction)
                .where(
                    and_(
                        HumanAction.id == pending_action_id,
                        HumanAction.workflow_thread_id == workflow_thread_id,
                        HumanAction.status == "open",
                    )
                )
                .values(
                    status="completed",
                    response_payload=response_payload,
                    decision=decision,
                    reason=reason,
                    actor=actor,
                    action_type=action_type,
                    responded_at=func.now(),
                )
            ),
        )
        if result.rowcount != 1:
            raise ValueError(
                f"Open human action {pending_action_id} not found for thread {workflow_thread_id}"
            )

        new_pending_action_id: UUID | None = None
        if next_pending_interrupt_type is not None:
            if next_pending_request_payload is None:
                raise ValueError("Next pending action requires a request payload.")
            pending_row = HumanAction(
                workflow_thread_id=workflow_thread_id,
                interrupt_type=next_pending_interrupt_type,
                request_payload=next_pending_request_payload,
                state_snapshot=next_pending_state_snapshot,
                status="open",
            )
            self._session.add(pending_row)
            self._session.flush()
            new_pending_action_id = pending_row.id

        thread = self._session.get(WorkflowThread, workflow_thread_id)
        if thread is not None:
            thread.status = next_status
            thread.stage = next_stage
            if completed:
                thread.current_node = None
                thread.completed_at = func.now()
            elif next_current_node is not None:
                thread.current_node = next_current_node
            if next_metadata is not None:
                thread.metadata_json = next_metadata

        self._session.flush()
        return new_pending_action_id


class ProcessingErrorRepository:
    """Errors surfaced during job, agent, and workflow processing.

    Kept independent of `HumanAction`, since not every error causes an interrupt.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def log(
        self,
        error_type: str,
        *,
        job_item_id: UUID | None = None,
        agent_run_id: UUID | None = None,
        purchase_order_line_id: UUID | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        node_name: str | None = None,
        raw_error_detail: dict[str, Any] | None = None,
    ) -> dict:
        """Record one processing error, optionally scoped to a job item, agent run, and/or PO line."""
        row = ProcessingError(
            job_item_id=job_item_id,
            agent_run_id=agent_run_id,
            purchase_order_line_id=purchase_order_line_id,
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
            node_name=node_name,
            raw_error_detail=raw_error_detail,
        )
        self._session.add(row)
        self._session.flush()
        return _processing_error_to_dict(row)

    def list_for_job_item(self, job_item_id: UUID) -> list[dict]:
        """Return every error logged against `job_item_id`, most recent first."""
        rows = self._session.scalars(
            select(ProcessingError)
            .where(ProcessingError.job_item_id == job_item_id)
            .order_by(ProcessingError.occurred_at.desc())
        ).all()
        return [_processing_error_to_dict(r) for r in rows]

    def list_for_agent_run(self, agent_run_id: UUID) -> list[dict]:
        """Return every error logged against `agent_run_id`, most recent first."""
        rows = self._session.scalars(
            select(ProcessingError)
            .where(ProcessingError.agent_run_id == agent_run_id)
            .order_by(ProcessingError.occurred_at.desc())
        ).all()
        return [_processing_error_to_dict(r) for r in rows]

    def list_for_purchase_order_line(self, purchase_order_line_id: UUID) -> list[dict]:
        """Return errors for one PO line, including those logged before any interrupt."""
        rows = self._session.scalars(
            select(ProcessingError)
            .where(ProcessingError.purchase_order_line_id == purchase_order_line_id)
            .order_by(ProcessingError.occurred_at.desc())
        ).all()
        return [_processing_error_to_dict(r) for r in rows]

    def mark_resolved(self, processing_error_id: UUID, resolved_by: str) -> dict | None:
        """Mark an error resolved and stamp who resolved it; returns None if the error doesn't exist."""
        row = self._session.get(ProcessingError, processing_error_id)
        if row is None:
            return None

        row.resolved = True
        row.resolved_by = resolved_by
        row.resolved_at = func.now()
        self._session.flush()
        return _processing_error_to_dict(row)
