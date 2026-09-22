"""Repository for shared process.agent, agent_run, and agent_trace tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Agent, AgentRun, AgentTrace
from app.utils.pagination import next_cursor_from_page, parse_cursor
from app.utils.sanitize import strip_nul_bytes


def _agent_to_dict(row: Agent) -> dict:
    """Project an `Agent` row onto the plain dict shape returned to callers."""
    return {
        "id": row.id,
        "agent_code": row.agent_code,
        "agent_name": row.agent_name,
        "domain": row.domain,
        "prompt_version": row.prompt_version,
        "system_prompt": row.system_prompt,
        "description": row.description,
        "is_active": row.is_active,
    }


def _agent_run_to_dict(row: AgentRun) -> dict:
    """Project an `AgentRun` row onto the plain dict shape returned to callers."""
    return {
        "id": row.id,
        "job_item_id": row.job_item_id,
        "workflow_thread_id": row.workflow_thread_id,
        "agent_id": row.agent_id,
        "status": row.status,
        "run_type": row.run_type,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "error": row.error,
        "metadata_json": row.metadata_json,
        "updated_at": row.updated_at,
    }


def _agent_trace_to_dict(row: AgentTrace) -> dict:
    """Project an `AgentTrace` row onto the plain dict shape returned to callers."""
    return {
        "id": row.id,
        "agent_run_id": row.agent_run_id,
        "node_name": row.node_name,
        "status": row.status,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "duration_ms": row.duration_ms,
        "input_snapshot": row.input_snapshot,
        "output_snapshot": row.output_snapshot,
        "error": row.error,
    }


class AgentRegistryRepository:
    """Repository for the merged agent/prompt-version registry."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure_registered(
        self,
        agent_code: str,
        prompt_version: str,
        system_prompt: str,
        agent_name: str,
        domain: str,
        description: str | None = None,
        is_active: bool = True,
    ) -> UUID:
        """Idempotently register one (agent_code, prompt_version) row.

        Register-once rather than upsert: a second call carrying a different
        `system_prompt` or `is_active` for the same pair returns the existing id and
        changes nothing. Bumping a prompt means bumping `prompt_version`, which
        registers as a fresh row.
        """
        row = self._session.scalars(
            select(Agent).where(Agent.agent_code == agent_code, Agent.prompt_version == prompt_version)
        ).first()

        if row is None:
            if is_active:
                self._session.execute(
                    update(Agent)
                    .where(Agent.agent_code == agent_code, Agent.is_active.is_(True))
                    .values(is_active=False)
                )
            row = Agent(
                agent_code=agent_code,
                agent_name=agent_name,
                domain=domain,
                prompt_version=prompt_version,
                system_prompt=system_prompt,
                description=description,
                is_active=is_active,
            )
            self._session.add(row)
            self._session.flush()

        return row.id

    def get_by_code_version(self, agent_code: str, prompt_version: str) -> dict | None:
        """Return the exact (agent_code, prompt_version) row, or None if it isn't registered."""
        row = self._session.scalars(
            select(Agent).where(Agent.agent_code == agent_code, Agent.prompt_version == prompt_version)
        ).first()
        return _agent_to_dict(row) if row is not None else None

    def get_active(self, agent_code: str) -> dict | None:
        """Return the one `is_active` row for `agent_code`, or None if no version is currently active."""
        row = self._session.scalars(
            select(Agent).where(Agent.agent_code == agent_code, Agent.is_active.is_(True))
        ).first()
        return _agent_to_dict(row) if row is not None else None


class AgentRunRepository:
    """Repository for one independent agent execution."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def start(
        self,
        agent_id: UUID,
        run_type: str,
        job_item_id: UUID | None = None,
        workflow_thread_id: UUID | None = None,
    ) -> UUID:
        """Open a new agent run in `running` status, returning its id."""
        row = AgentRun(
            agent_id=agent_id,
            run_type=run_type,
            job_item_id=job_item_id,
            workflow_thread_id=workflow_thread_id,
            status="running",
            metadata_json={},
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def update_status(
        self,
        run_id: UUID,
        status: str,
        *,
        error: str | None = None,
        completed: bool = False,
    ) -> None:
        """Update an agent run's status, optionally recording an error and/or stamping `completed_at`.

        Wrapped in its own savepoint: this session backs CMIR, PO-validation, and
        rule-extraction runs simultaneously (`app/core/container.py`), so a write failure
        here (for example a NUL byte in `error`) must roll back only this update, not
        every other run's uncommitted work on the same shared session.
        """
        row = self._session.get(AgentRun, run_id)
        if row is None:
            return

        row.status = status
        if error is not None:
            row.error = strip_nul_bytes(error)
        if completed:
            from sqlalchemy import func

            row.completed_at = func.now()
        with self._session.begin_nested():
            self._session.flush()

    def get(self, run_id: UUID) -> dict | None:
        """Return the agent run `run_id`, or None if it doesn't exist."""
        row = self._session.get(AgentRun, run_id)
        return _agent_run_to_dict(row) if row is not None else None

    def list_runs(
        self,
        *,
        job_item_id: UUID | None = None,
        workflow_thread_id: UUID | None = None,
        agent_id: UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Page through agent runs, filtered by any combination of job item, thread, agent, and status.

        Ordered newest-first on `updated_at` with id as a tiebreaker; returns
        the page alongside a cursor for the next page, or `None` once the
        page is short of `limit` (no more results).
        """
        stmt = select(AgentRun)
        if job_item_id is not None:
            stmt = stmt.where(AgentRun.job_item_id == job_item_id)
        if workflow_thread_id is not None:
            stmt = stmt.where(AgentRun.workflow_thread_id == workflow_thread_id)
        if agent_id is not None:
            stmt = stmt.where(AgentRun.agent_id == agent_id)
        if status is not None:
            stmt = stmt.where(AgentRun.status == status)
        if cursor is not None:
            stmt = stmt.where(AgentRun.updated_at < parse_cursor(cursor))
        # Tiebreaker on id (see workflow.py's list_threads for why).
        stmt = stmt.order_by(AgentRun.updated_at.desc(), AgentRun.id.desc()).limit(limit)

        rows = self._session.scalars(stmt).all()
        items = [_agent_run_to_dict(r) for r in rows]
        next_cursor = next_cursor_from_page(items, limit)
        return items, next_cursor


class AgentTraceRepository:
    """Repository for one LangGraph node execution."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def log(
        self,
        agent_run_id: UUID,
        node_name: str,
        status: str,
        started_at: datetime,
        completed_at: datetime | None,
        duration_ms: int | None,
        input_snapshot: dict[str, Any] | None,
        output_snapshot: dict[str, Any] | None,
        error: str | None,
    ) -> dict:
        """Record one LangGraph node execution for an agent run.

        The insert runs inside its own savepoint, not the bare session: this session
        backs CMIR, PO-validation, and rule-extraction runs simultaneously (see
        `app/core/container.py`), so a write failure here (a NUL byte in a snapshot or
        error field raises ValueError client-side or an invalid-byte-sequence error
        server-side) must roll back only this trace insert, not a different concurrent
        run's uncommitted work on the same shared session.
        """
        row = AgentTrace(
            agent_run_id=agent_run_id,
            node_name=node_name,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            input_snapshot=input_snapshot,
            output_snapshot=output_snapshot,
            error=error,
        )
        with self._session.begin_nested():
            self._session.add(row)
            self._session.flush()
        return _agent_trace_to_dict(row)

    def list_for_run(self, agent_run_id: UUID) -> list[dict]:
        """Return every node execution trace for `agent_run_id`, in execution order."""
        rows = self._session.scalars(
            select(AgentTrace)
            .where(AgentTrace.agent_run_id == agent_run_id)
            .order_by(AgentTrace.started_at.asc())
        ).all()
        return [_agent_trace_to_dict(r) for r in rows]
