"""Integration tests for the B (TOCTOU) narrow fix -- proves that when two
concurrent requests race to resolve the SAME open human_action (the same
pending decision on the same thread), against REAL Postgres, through the
REAL CmirService/PoValidationService paths:

- exactly one succeeds
- the other receives a clean, expected ConflictError/HTTP 409 -- not the
  previous bare ValueError/ExternalServiceError (WORKFLOW_RESUME_FAILED,
  5xx). The loser's exact code legitimately varies with timing: it is
  THREAD_STALE if both callers reach `HumanActionRepository
  .apply_human_action`'s guard before either commits, or
  THREAD_NOT_WAITING if the winner fully commits before the loser's own
  `_ensure_current`/status check even runs (that earlier check then sees
  the thread already in its new, non-waiting terminal status) -- see
  `run_service.py::_thread_not_waiting`/`submit_decision`'s status guard.
  Both are `ConflictError` subclasses with `status_code == 409`; this test
  intentionally accepts either, and only either -- never a 5xx.
- the final DB state shows exactly one completed human_action for that
  pending_action_id and a single coherent final workflow_thread status --
  not a duplicate/inconsistent state

Mirrors tests/integration/test_po_ingestion_concurrency_postgres.py's
`threading.Barrier` + `threading.Thread` pattern for genuine concurrent
execution, and tests/integration/test_cmir_conflict_postgres.py's use of the
REAL `Container` wiring with a deterministic stub graph for CMIR (the graph's
own logic is not what this test targets).
"""

from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.container import Container
from app.core.exceptions import ConflictError, ExternalServiceError
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
            "No reachable Postgres DATABASE_URL configured -- skipping B (TOCTOU) "
            "concurrent-decision integration tests."
        )
    yield db
    db.dispose()


def _run_concurrently(fns: list[callable]) -> tuple[list, list[BaseException]]:
    """Runs each zero-arg callable in its own thread, released simultaneously
    via a Barrier -- genuine concurrent execution, not sequential calls."""
    n = len(fns)
    barrier = threading.Barrier(n)
    results: list = [None] * n
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[idx] = fns[idx]()
        except BaseException as exc:  # noqa: BLE001 -- surfaced via `errors`, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results, errors


class _FakeInterrupt:
    def __init__(self, value: dict) -> None:
        self.value = value


class _StubGraph:
    """Deterministic stand-in for the compiled LangGraph graph -- avoids a
    real Azure OpenAI/extraction call (out of scope here); every other layer
    (Session, repositories, transaction boundary) is real. Each concurrent
    caller gets its OWN instance (not shared), so there is no thread-safety
    concern in the stub itself -- only the real repositories/Session are
    actually exercised concurrently."""

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


def test_cmir_concurrent_decisions_on_same_pending_action_one_wins_one_conflicts(
    pg_database: Database,
) -> None:
    container = Container.build()

    with container.cmir_repos() as repos:
        email_id = repos.email_repository.save(
            sender="customer@example.com",
            subject="B concurrency test",
            raw_content="body",
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
            uow, "batch-b-concurrency", {"placeholder": True}, existing_email_id=email_id
        )
    thread_id: UUID = result["id"]
    stage = setup_service.get_stage(thread_id)
    expected_updated_at = stage["updated_at"].isoformat()

    def _call():
        graph = _StubGraph(
            [
                {
                    "decision": "approve",
                    "cmir": {"customer_identity": "Acme Manufacturing Ltd"},
                    "email_id": email_id,
                }
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
        return service.submit_decision(
            thread_id,
            actor="reviewer@company.com",
            decision="approve",
            expected_updated_at=expected_updated_at,
        )

    results, errors = _run_concurrently([_call, _call])

    successes = [r for r in results if r is not None]
    conflicts = [e for e in errors if isinstance(e, ConflictError)]
    unexpected = [e for e in errors if not isinstance(e, ConflictError)]

    assert not unexpected, f"expected only ConflictError for the loser, got: {unexpected}"
    assert len(successes) == 1, f"expected exactly one success, got {len(successes)}: {results}"
    assert len(conflicts) == 1, f"expected exactly one conflict, got {len(conflicts)}"
    # The B fix: the loser must get a clean, expected conflict -- not the
    # previous bare ValueError/ExternalServiceError(WORKFLOW_RESUME_FAILED).
    # Both THREAD_STALE (raced at the apply_human_action guard) and
    # THREAD_NOT_WAITING (raced earlier, at the status check, if the winner
    # already fully committed) are legitimate, timing-dependent outcomes --
    # accepting only these two specific codes, never any other/generic error.
    assert conflicts[0].code in {"THREAD_STALE", "THREAD_NOT_WAITING"}, conflicts[0].code
    assert conflicts[0].status_code == 409
    assert not any(isinstance(e, ExternalServiceError) for e in errors)

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
    finally:
        verify.close()

    # Exactly one human_action resolved for this thread -- not two, not zero.
    assert completed_count == 1
    assert thread_status == "completed_approved"


def test_po_concurrent_manual_cmir_entry_on_same_pending_action_one_wins_one_conflicts(
    pg_database: Database,
) -> None:
    from app.api.dependencies import build_po_validation_service

    service = build_po_validation_service()

    retailer_code = f"B-CONC-CUST-{threading.get_ident()}"
    plant = f"B-CONC-PLANT-{threading.get_ident()}"
    po_number = f"B-CONC-PO-{threading.get_ident()}"
    ingest_result = service.ingest_po_lines(
        [
            {
                "po_number": po_number,
                "po_line_number": "10",
                "retailer_code": retailer_code,
                "retailer_material_code": "B-CONC-MAT",
                "plant": plant,
                "order_quantity": 5,
            }
        ]
    )
    thread_id = UUID(ingest_result["lines"][0]["thread_id"])
    stage = service.get_stage(thread_id)
    expected_updated_at = stage["updated_at"].isoformat()

    # Seed a real material_master row so the manual-entry answer resolves cleanly.
    sap_material_number = f"B-CONC-SAP-{threading.get_ident()}"
    with service._unit_of_work_factory() as uow:
        plant_row = uow.master_data.get_or_create_plant(plant)
        material = uow.master_data.add_material(sap_material_number, "B concurrency test material")
        uow.master_data.add_material_master(
            material_id=material["id"],
            sap_material_number=sap_material_number,
            plant_id=plant_row["id"],
            available_quantity=1000,
        )

    def _call():
        return service.submit_manual_cmir_entry(
            thread_id,
            actor="reviewer@company.com",
            sap_material_number=sap_material_number,
            description="B concurrency test",
            expected_updated_at=expected_updated_at,
        )

    results, errors = _run_concurrently([_call, _call])

    successes = [r for r in results if r is not None]
    conflicts = [e for e in errors if isinstance(e, ConflictError)]
    unexpected = [e for e in errors if not isinstance(e, ConflictError)]

    assert not unexpected, f"expected only ConflictError for the loser, got: {unexpected}"
    assert len(successes) == 1, f"expected exactly one success, got {len(successes)}: {results}"
    assert len(conflicts) == 1, f"expected exactly one conflict, got {len(conflicts)}"
    # Both THREAD_STALE and THREAD_NOT_WAITING are legitimate, timing-dependent
    # outcomes for the loser -- see the module docstring and the CMIR test above.
    assert conflicts[0].code in {"THREAD_STALE", "THREAD_NOT_WAITING"}, conflicts[0].code
    assert conflicts[0].status_code == 409

    verify = pg_database.new_session()
    try:
        completed_count = verify.execute(
            text(
                "SELECT count(*) FROM process.human_action "
                "WHERE workflow_thread_id = :id AND status = 'completed'"
            ),
            {"id": thread_id},
        ).scalar()
    finally:
        verify.close()

    assert completed_count == 1
