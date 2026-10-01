"""Integration tests for the A.2 DB-persistence retry mechanism.

Proves that when the DB-persistence step fails immediately after a
successful graph invoke/checkpoint write (LangGraph's PostgresSaver
connection is autocommit=True -- Phase 1's finding -- so the checkpoint is
already durably committed by this point), CmirService/PoValidationService:

- retry EXACTLY ONCE, with a completely fresh Session (never re-invoking
  the graph -- never re-running business logic),
- converge to a consistent final state if the retry succeeds,
- or surface a distinct, non-silent ExternalServiceError(code=
  "WORKFLOW_STATE_CORRUPT") -- never the generic WORKFLOW_RESUME_FAILED --
  with no partial/duplicate DB writes, if the retry also fails.

Per Phase 4's explicit instruction, the graph/checkpoint call itself is
NEVER made to fail in these tests -- only the DB-persistence step
(`HumanActionRepository.apply_human_action`) is deterministically failed,
via a monkeypatched instance method on a real repository object backed by a
real Session (the object itself is real; only its behavior for this one
call is overridden) -- not a fake/mock service.

Mirrors tests/integration/test_cmir_conflict_postgres.py's use of the REAL
`Container.build()` wiring with a deterministic stub graph for CMIR (the
graph's own SCD2/extraction logic is a separate, already-tested concern);
PO tests use the REAL, unstubbed PO graph (no LLM dependency there), which
also lets checkpoint state be verified via `graph.get_state()` -- an
existing, already-established LangGraph inspection API, not a new
mechanism.
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
from app.core.exceptions import ExternalServiceError
from app.db.session import Database
from app.services.cmir.service import CmirService

INJECTED_FAILURE_MESSAGE = "Injected DB-persistence failure (A.2 test)"


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
            "No reachable Postgres DATABASE_URL configured -- skipping A.2 "
            "persistence-retry integration tests."
        )
    yield db
    db.dispose()


class _FakeInterrupt:
    def __init__(self, value: dict) -> None:
        self.value = value


class _StubGraph:
    """Deterministic stand-in for the compiled LangGraph graph -- avoids a
    real Azure OpenAI/extraction call (out of scope here). ALWAYS succeeds --
    per Phase 4's instruction, these tests never fail the graph/checkpoint
    call, only the DB-persistence step downstream of it."""

    def __init__(self, invoke_plan: list[Any]) -> None:
        self.invoke_plan = invoke_plan
        self._call_count = 0

    def invoke(self, value, config=None):
        index = min(self._call_count, len(self.invoke_plan) - 1)
        self._call_count += 1
        return copy.deepcopy(self.invoke_plan[index])

    def update_state(self, config, value):
        pass


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError(INJECTED_FAILURE_MESSAGE)


def _seed_cmir_thread_awaiting_approval(container: Container) -> tuple[UUID, str, UUID]:
    """Real Postgres, real repositories, real per-invocation Session --
    only the graph is stubbed (no LLM dependency). Returns
    (thread_id, expected_updated_at, email_id)."""
    with container.cmir_repos() as repos:
        email_id = repos.email_repository.save(
            sender="customer@example.com", subject="A.2 retry test", raw_content="body"
        )

    setup_graph = _StubGraph(
        [
            {
                "__interrupt__": [
                    _FakeInterrupt(
                        {"reason": "approval_required", "email_id": str(email_id), "cmir": {"brand": "Brand A"}}
                    )
                ],
                "email_id": email_id,
                "cmir": {"brand": "Brand A"},
            }
        ]
    )

    @contextmanager
    def setup_uow_factory():
        with container.cmir_unit_of_work() as uow:
            uow.graph = setup_graph
            yield uow

    setup_service = CmirService(
        email_reader=container.email_reader,
        repos_factory=container.cmir_repos,
        unit_of_work_factory=setup_uow_factory,
    )
    with setup_service._unit_of_work_factory() as uow:
        result = setup_service._process_email_thread(
            uow, "batch-a2-retry", {"placeholder": True}, existing_email_id=email_id
        )
    thread_id: UUID = result["id"]
    stage = setup_service.get_stage(thread_id)
    return thread_id, stage["updated_at"].isoformat(), email_id


def _human_action_status(pg_database: Database, thread_id: UUID) -> tuple[int, str | None]:
    """Returns (completed_count, workflow_thread.status) via an independent
    connection -- not the one the service call itself used."""
    verify = pg_database.new_session()
    try:
        completed_count = verify.execute(
            text(
                "SELECT count(*) FROM process.human_action "
                "WHERE workflow_thread_id = :id AND status = 'completed'"
            ),
            {"id": thread_id},
        ).scalar()
        thread_status = verify.execute(
            text("SELECT status FROM process.workflow_thread WHERE id = :id"), {"id": thread_id}
        ).scalar()
        return completed_count, thread_status
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# TEST 1 -- DB persistence succeeds (sanity baseline for this file)
# ---------------------------------------------------------------------------


def test_db_persistence_succeeds_consistent_final_state(pg_database: Database) -> None:
    container = Container.build()
    thread_id, expected_updated_at, email_id = _seed_cmir_thread_awaiting_approval(container)

    resume_graph = _StubGraph(
        [{"decision": "approve", "cmir": {"customer_identity": "Acme Manufacturing Ltd"}, "email_id": email_id}]
    )

    @contextmanager
    def uow_factory():
        with container.cmir_unit_of_work() as uow:
            uow.graph = resume_graph
            yield uow

    service = CmirService(
        email_reader=container.email_reader,
        repos_factory=container.cmir_repos,
        unit_of_work_factory=uow_factory,
    )

    result = service.submit_decision(
        thread_id, actor="reviewer@company.com", decision="approve", expected_updated_at=expected_updated_at
    )

    assert result["status"] == "completed_approved"
    completed_count, thread_status = _human_action_status(pg_database, thread_id)
    assert completed_count == 1
    assert thread_status == "completed_approved"


# ---------------------------------------------------------------------------
# TEST 2 -- first DB persistence fails, retry (fresh Session) succeeds
# ---------------------------------------------------------------------------


def test_first_db_persistence_fails_retry_succeeds(pg_database: Database) -> None:
    container = Container.build()
    thread_id, expected_updated_at, email_id = _seed_cmir_thread_awaiting_approval(container)

    resume_graph = _StubGraph(
        [{"decision": "approve", "cmir": {"customer_identity": "Acme Manufacturing Ltd"}, "email_id": email_id}]
    )
    session_ids: dict[str, int] = {}

    @contextmanager
    def failing_once_uow_factory():
        with container.cmir_unit_of_work() as uow:
            uow.graph = resume_graph
            session_ids["first_attempt"] = id(uow.human_actions._session)
            uow.human_actions.apply_human_action = _boom
            yield uow

    def instrumented_repos_factory():
        # TEST 4 (fresh-session proof) folded in here: record the retry's
        # Session identity so we can assert it differs from the first
        # attempt's -- the real container.cmir_repos() is otherwise
        # completely unwrapped/real for this test (retry succeeds).
        cm = container.cmir_repos()
        real_repos = cm.__enter__()
        session_ids["retry_attempt"] = id(real_repos.human_actions._session)

        @contextmanager
        def _wrapper():
            try:
                yield real_repos
            finally:
                cm.__exit__(None, None, None)

        return _wrapper()

    service = CmirService(
        email_reader=container.email_reader,
        repos_factory=instrumented_repos_factory,
        unit_of_work_factory=failing_once_uow_factory,
    )

    result = service.submit_decision(
        thread_id, actor="reviewer@company.com", decision="approve", expected_updated_at=expected_updated_at
    )

    assert result["status"] == "completed_approved"
    completed_count, thread_status = _human_action_status(pg_database, thread_id)
    # Exactly one completed human_action -- not two (no duplicate business
    # action from the retry), not zero (the retry did persist it).
    assert completed_count == 1
    assert thread_status == "completed_approved"
    # TEST 4: the retry used a genuinely different Session, never the failed one.
    assert session_ids["first_attempt"] != session_ids["retry_attempt"]


# ---------------------------------------------------------------------------
# TEST 3 -- first DB persistence fails, retry ALSO fails
# ---------------------------------------------------------------------------


def test_first_db_persistence_fails_retry_also_fails_raises_workflow_state_corrupt(
    pg_database: Database,
) -> None:
    container = Container.build()
    thread_id, expected_updated_at, email_id = _seed_cmir_thread_awaiting_approval(container)

    resume_graph = _StubGraph(
        [{"decision": "approve", "cmir": {"customer_identity": "Acme Manufacturing Ltd"}, "email_id": email_id}]
    )

    @contextmanager
    def always_failing_uow_factory():
        with container.cmir_unit_of_work() as uow:
            uow.graph = resume_graph
            uow.human_actions.apply_human_action = _boom
            yield uow

    @contextmanager
    def always_failing_repos_factory():
        with container.cmir_repos() as repos:
            repos.human_actions.apply_human_action = _boom
            yield repos

    service = CmirService(
        email_reader=container.email_reader,
        repos_factory=always_failing_repos_factory,
        unit_of_work_factory=always_failing_uow_factory,
    )

    with pytest.raises(ExternalServiceError) as raised:
        service.submit_decision(
            thread_id, actor="reviewer@company.com", decision="approve", expected_updated_at=expected_updated_at
        )

    # Must be the distinct A.2 code, never the generic resume-failed one.
    assert raised.value.code == "WORKFLOW_STATE_CORRUPT"
    assert raised.value.code != "WORKFLOW_RESUME_FAILED"
    assert raised.value.status_code == 502

    # No partial/duplicate writes from either failed attempt -- the thread
    # must still show its ORIGINAL pre-decision state, not a half-applied one.
    completed_count, thread_status = _human_action_status(pg_database, thread_id)
    assert completed_count == 0
    assert thread_status == "waiting_approval"


# ---------------------------------------------------------------------------
# TEST 5 -- real CMIR HITL flow, end-to-end DB consistency
# ---------------------------------------------------------------------------


def test_cmir_real_hitl_flow_db_consistent_after_decision(pg_database: Database) -> None:
    """waiting_approval -> reviewer decision -> graph -> DB persistence,
    verifying final DB consistency (no persistence-retry infrastructure
    involved -- this is the plain happy path through the real service,
    proving A.2's changes didn't disturb ordinary CMIR HITL behavior)."""
    container = Container.build()
    thread_id, expected_updated_at, email_id = _seed_cmir_thread_awaiting_approval(container)

    resume_graph = _StubGraph(
        [{"decision": "reject", "cmir": {"customer_identity": "Acme Manufacturing Ltd"}, "email_id": email_id}]
    )

    @contextmanager
    def uow_factory():
        with container.cmir_unit_of_work() as uow:
            uow.graph = resume_graph
            yield uow

    service = CmirService(
        email_reader=container.email_reader,
        repos_factory=container.cmir_repos,
        unit_of_work_factory=uow_factory,
    )

    result = service.submit_decision(
        thread_id,
        actor="reviewer@company.com",
        decision="reject",
        reason="Not a valid CMIR request",
        expected_updated_at=expected_updated_at,
    )

    assert result["status"] == "completed_rejected"
    completed_count, thread_status = _human_action_status(pg_database, thread_id)
    assert completed_count == 1
    assert thread_status == "completed_rejected"

    # Fresh GET after the decision must reflect the same durable state.
    assert service.get_stage(thread_id)["status"] == "completed_rejected"


# ---------------------------------------------------------------------------
# TEST 6 -- real PO HITL flow, end-to-end DB + CHECKPOINT consistency
# (real, unstubbed PO graph -- no LLM dependency -- so checkpoint state can
# genuinely be inspected via the existing graph.get_state() API)
# ---------------------------------------------------------------------------


def test_po_real_hitl_flow_db_and_checkpoint_consistent(pg_database: Database) -> None:
    import uuid

    from app.api.dependencies import build_po_validation_service

    service = build_po_validation_service()

    unique = uuid.uuid4().hex[:10]
    retailer_code = f"A2-PO-CUST-{unique}"
    plant = f"A2-PO-PLANT-{unique}"
    po_number = f"A2-PO-NUMBER-{unique}"
    ingest_result = service.ingest_po_lines(
        [
            {
                "po_number": po_number,
                "po_line_number": "10",
                "retailer_code": retailer_code,
                "retailer_material_code": f"A2-PO-MAT-{unique}",
                "plant": plant,
                "order_quantity": 5,
            }
        ]
    )
    thread_id = UUID(ingest_result["lines"][0]["thread_id"])
    checkpoint_thread_id_before = None
    stage = service.get_stage(thread_id)
    expected_updated_at = stage["updated_at"].isoformat()
    checkpoint_thread_id_before = stage["metadata_json"]["checkpoint_thread_id"]

    # Seed a real material_master row so the manual-entry answer resolves cleanly.
    sap_material_number = f"A2-PO-SAP-{unique}"
    with service._unit_of_work_factory() as uow:
        plant_row = uow.master_data.get_or_create_plant(plant)
        material = uow.master_data.add_material(sap_material_number, "A.2 PO test material")
        uow.master_data.add_material_master(
            material_id=material["id"],
            sap_material_number=sap_material_number,
            plant_id=plant_row["id"],
            available_quantity=1000,
        )

    result = service.submit_manual_cmir_entry(
        thread_id,
        actor="reviewer@company.com",
        sap_material_number=sap_material_number,
        description="A.2 PO real-flow test",
        expected_updated_at=expected_updated_at,
    )

    assert result["status"] == "ready_for_so_creation"
    completed_count, thread_status = _human_action_status(pg_database, thread_id)
    assert completed_count == 1
    assert thread_status == "ready_for_so_creation"

    # Checkpoint-state verification via the existing, real graph.get_state()
    # API (not a new inspection mechanism) -- confirms the checkpoint has no
    # remaining pending interrupt, matching the DB's terminal status.
    with service._unit_of_work_factory() as uow:
        config = service._thread_config(checkpoint_thread_id_before)
        snapshot = uow.graph.get_state(config)
    assert snapshot.next == (), (
        f"checkpoint still shows pending graph steps {snapshot.next!r}; "
        "expected the graph to have fully resolved, matching the DB's terminal status"
    )
