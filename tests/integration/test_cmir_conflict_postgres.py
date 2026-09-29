"""Integration test for the A.1 CMIR conflict-rollback fix -- proves that
CmirService's own transaction/control-flow persists the reviewer's
thread/human_action/agent_run bookkeeping to REAL Postgres even when the
graph resume reports a CMIR SCD2 write conflict, verified via an
independent DB connection (not the one the service call itself used).

Mirrors tests/integration/test_po_ingestion_concurrency_postgres.py's
pattern (module-scoped `pg_database` fixture, skip cleanly if unreachable).

Uses the REAL `app.core.container.Container` wiring -- real
`Database.session()` commit/rollback semantics, real repositories, the
real per-invocation `cmir_unit_of_work()` factory -- with only the graph
swapped for a deterministic stub. The graph's own SCD2 conflict-detection
logic is a separate, already out-of-scope concern (see
tests/unit/services/test_cmir_run_service.py's own module docstring: "the
graph itself... is Phase 4's concern and stays out of scope here"); this
test targets exactly what the existing SQLite-backed
`test_submit_decision_conflict_closes_thread_and_raises_version_conflict`
cannot, because that test's `_build_service` wraps both `repos_factory`
and `unit_of_work_factory` in `contextlib.nullcontext`, sharing one Session
for the whole test with no real commit/rollback boundary -- so it could not
have caught the original A.1 bug (raising ConflictError from inside the
transactional `with` block rolled back the bookkeeping the code claimed
was "already committed and consistent").
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.container import Container
from app.core.exceptions import ConflictError
from app.db.session import Database
from app.services.cmir.service import CmirService


def _connect_or_none() -> Database | None:
    settings = get_settings()
    if not settings.database.url.startswith("postgresql"):
        return None

    db = Database(settings.database.url)
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        db.dispose()
        return None
    return db


@pytest.fixture(scope="module")
def pg_database():
    db = _connect_or_none()
    if db is None:
        pytest.skip(
            "No reachable Postgres DATABASE_URL configured -- skipping A.1 CMIR "
            "conflict-rollback integration test."
        )
    yield db
    db.dispose()


class _FakeInterrupt:
    def __init__(self, value: dict) -> None:
        self.value = value


class _StubGraph:
    """Deterministic stand-in for the compiled LangGraph graph -- avoids a
    real Azure OpenAI/extraction call (out of scope here), while every
    other layer (Session, repositories, transaction boundary) is real."""

    def __init__(self, invoke_plan: list[Any]) -> None:
        self.invoke_plan = invoke_plan
        self._call_count = 0

    def invoke(self, value, config=None):
        index = min(self._call_count, len(self.invoke_plan) - 1)
        self._call_count += 1
        result = self.invoke_plan[index]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def update_state(self, config, value):
        pass


def test_submit_decision_conflict_commits_bookkeeping_to_real_postgres(pg_database: Database) -> None:
    container = Container.build()

    with container.cmir_repos() as repos:
        email_id = repos.email_repository.save(
            sender="customer@example.com",
            subject="A.1 conflict-rollback test",
            raw_content="body",
        )

    graph = _StubGraph(
        [
            {
                "__interrupt__": [
                    _FakeInterrupt(
                        {"reason": "approval_required", "email_id": str(email_id), "cmir": {"brand": "Brand A"}}
                    )
                ],
                "email_id": email_id,
                "cmir": {"brand": "Brand A"},
            },
            {
                "decision": "approve",
                "cmir_write_result": "conflict",
                "cmir": {"customer_identity": "Acme Manufacturing Ltd"},
                "email_id": email_id,
            },
        ]
    )

    @contextmanager
    def uow_factory():
        with container.cmir_unit_of_work() as uow:
            uow.graph = graph
            yield uow

    service = CmirService(
        email_reader=container.email_reader,
        repos_factory=container.cmir_repos,
        unit_of_work_factory=uow_factory,
    )

    with service._unit_of_work_factory() as uow:
        result = service._process_email_thread(uow, "batch-a1-conflict", {"placeholder": True}, existing_email_id=email_id)
    thread_id: UUID = result["id"]

    stage = service.get_stage(thread_id)
    pending_before = None
    with container.cmir_repos() as repos:
        pending_before = repos.human_actions.get_open_for_thread(thread_id)
    assert pending_before is not None

    with pytest.raises(ConflictError) as raised:
        service.submit_decision(
            thread_id,
            actor="reviewer@company.com",
            decision="approve",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "CMIR_VERSION_CONFLICT"
    assert raised.value.status_code == 409

    # Independent DB connection/session -- NOT the one submit_decision just used --
    # to prove the bookkeeping is durably committed, not an artifact of a still-open
    # session that merely hasn't rolled back yet.
    verify = pg_database.new_session()
    try:
        thread_status = verify.execute(
            text("SELECT status FROM process.workflow_thread WHERE id = :id"), {"id": thread_id}
        ).scalar()
        action_status = verify.execute(
            text(
                "SELECT status FROM process.human_action WHERE id = :id"
            ),
            {"id": pending_before["id"]},
        ).scalar()
        agent_run_status = verify.execute(
            text(
                "SELECT ar.status FROM process.agent_run ar "
                "JOIN process.human_action ha ON ha.agent_run_id = ar.id "
                "WHERE ha.id = :id"
            ),
            {"id": pending_before["id"]},
        ).scalar()
    finally:
        verify.close()

    # This is the actual A.1 regression guard: before the fix, raising
    # ConflictError from inside the transactional `with` block rolled these
    # back to their pre-resume values (workflow_thread still "waiting_approval",
    # human_action still "open", agent_run still "running") even though the
    # code claimed they were "already committed and consistent".
    assert thread_status == "completed_conflict"
    assert action_status == "completed"
    assert agent_run_status == "completed_conflict"

    # Fresh GET after the conflict must reflect the same durable state, not a
    # stale in-memory view.
    fresh_stage = service.get_stage(thread_id)
    assert fresh_stage["status"] == "completed_conflict"
