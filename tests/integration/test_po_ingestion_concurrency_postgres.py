"""Integration tests for the four PO-ingestion get-or-create races, against
a real Postgres instance -- covers exactly what the SQLite unit suite
(tests/unit/repositories/test_master_data_get_or_create.py,
test_purchase_order_get_or_create.py) cannot: genuine cross-connection
concurrency, using real threads and independent Sessions racing the same
unique key, with the Postgres `ON CONFLICT ... DO NOTHING` path actually
exercised (SQLite falls back to a plain, non-atomic check-then-insert that
only proves single-threaded idempotency, never real concurrency safety).

Mirrors tests/integration/test_job_queue_postgres.py's pattern exactly
(same `_connect_or_none`/skip-cleanly-if-unreachable shape, same
`threading.Barrier` + `threading.Thread` structure for true simultaneous
execution -- not sequential Session-A-then-Session-B calls).

Also includes one test that goes through the REAL production
`PoValidationService` built via `app.api.dependencies.build_po_validation_service()`
(the same `Container`-backed `po_validation_unit_of_work()` factory the
live API route uses) rather than a standalone repository/session, to prove
the fix works inside the actual per-request unit-of-work -- not just in
isolated repository tests.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository


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
            "No reachable Postgres DATABASE_URL configured -- skipping PO-ingestion "
            "concurrency integration tests."
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


def test_get_or_create_retailer_concurrent_race_resolves_to_one_row(pg_database: Database):
    retailer_code = f"RACE-RETAILER-{uuid.uuid4().hex[:10]}"

    def call():
        session = pg_database.new_session()
        try:
            repo = MasterDataRepository(session)
            row = repo.get_or_create_retailer(retailer_code, retailer_code, None)
            session.commit()
            return row["id"]
        finally:
            session.close()

    results, errors = _run_concurrently([call, call])

    assert not errors, errors
    assert results[0] == results[1], "both callers must resolve to the same retailer row"

    verify = pg_database.new_session()
    count = verify.execute(
        text("SELECT count(*) FROM common.retailer WHERE retailer_code = :code"), {"code": retailer_code}
    ).scalar()
    verify.close()
    assert count == 1


def test_get_or_create_plant_concurrent_race_resolves_to_one_row(pg_database: Database):
    plant_code = f"RACE-PLANT-{uuid.uuid4().hex[:10]}"

    def call():
        session = pg_database.new_session()
        try:
            repo = MasterDataRepository(session)
            row = repo.get_or_create_plant(plant_code)
            session.commit()
            return row["id"]
        finally:
            session.close()

    results, errors = _run_concurrently([call, call])

    assert not errors, errors
    assert results[0] == results[1]

    verify = pg_database.new_session()
    count = verify.execute(
        text("SELECT count(*) FROM common.plant WHERE plant_code = :code"), {"code": plant_code}
    ).scalar()
    verify.close()
    assert count == 1


def test_get_or_create_purchase_order_concurrent_race_resolves_to_one_row(pg_database: Database):
    po_number = f"RACE-PO-{uuid.uuid4().hex[:10]}"

    setup = pg_database.new_session()
    retailer_id = MasterDataRepository(setup).add_retailer(
        f"RACE-PO-RETAILER-{uuid.uuid4().hex[:8]}", "Race PO Retailer", None
    )["id"]
    setup.commit()
    setup.close()

    def call():
        session = pg_database.new_session()
        try:
            repo = PurchaseOrderRepository(session)
            row = repo.get_or_create_purchase_order(
                po_number, retailer_id=retailer_id, order_date=datetime.now(UTC).date()
            )
            session.commit()
            return row["id"]
        finally:
            session.close()

    results, errors = _run_concurrently([call, call])

    assert not errors, errors
    assert results[0] == results[1]

    verify = pg_database.new_session()
    count = verify.execute(
        text("SELECT count(*) FROM common.purchase_order WHERE purchase_order_number = :n"),
        {"n": po_number},
    ).scalar()
    verify.close()
    assert count == 1


def test_get_or_create_line_concurrent_race_resolves_to_one_row(pg_database: Database):
    setup = pg_database.new_session()
    retailer_id = MasterDataRepository(setup).add_retailer(
        f"RACE-LINE-RETAILER-{uuid.uuid4().hex[:8]}", "Race Line Retailer", None
    )["id"]
    po_id = PurchaseOrderRepository(setup).create_purchase_order(
        f"RACE-LINE-PO-{uuid.uuid4().hex[:10]}", retailer_id=retailer_id, order_date=datetime.now(UTC).date()
    )["id"]
    setup.commit()
    setup.close()

    def call():
        session = pg_database.new_session()
        try:
            repo = PurchaseOrderRepository(session)
            row = repo.get_or_create_line(
                po_id, "10", 100.0, retailer_material_code="MAT-RACE", unit_price=0.0, line_status="NEW"
            )
            session.commit()
            return row["id"]
        finally:
            session.close()

    results, errors = _run_concurrently([call, call])

    assert not errors, errors
    assert results[0] == results[1]

    verify = pg_database.new_session()
    count = verify.execute(
        text(
            "SELECT count(*) FROM common.purchase_order_line "
            "WHERE purchase_order_id = :po_id AND line_number = :line_number"
        ),
        {"po_id": po_id, "line_number": "10"},
    ).scalar()
    verify.close()
    assert count == 1


def test_real_uow_concurrent_ingest_po_lines_no_duplicates(pg_database: Database):
    """Exercises the REAL production PoValidationService (built via
    app.api.dependencies.build_po_validation_service(), the same
    Container-backed po_validation_unit_of_work() factory the live API
    route uses) -- not a standalone repository/session -- with two
    concurrent, identical ingest_po_lines() calls."""
    from app.api.dependencies import build_po_validation_service

    service = build_po_validation_service()

    retailer_code = f"RACE-UOW-CUST-{uuid.uuid4().hex[:8]}"
    plant = f"RACE-UOW-PLANT-{uuid.uuid4().hex[:8]}"
    po_number = f"RACE-UOW-PO-{uuid.uuid4().hex[:10]}"
    payload = {
        "po_number": po_number,
        "po_line_number": "10",
        "retailer_code": retailer_code,
        "retailer_material_code": "MAT-UOW-RACE",
        "plant": plant,
        "order_quantity": 42,
    }

    def call():
        return service.ingest_po_lines([dict(payload)])

    results, errors = _run_concurrently([call, call])

    assert not errors, errors
    for result in results:
        assert result["total_lines"] == 1
        assert result["lines"][0]["po_number"] == po_number

    verify = pg_database.new_session()
    retailer_count = verify.execute(
        text("SELECT count(*) FROM common.retailer WHERE retailer_code = :c"), {"c": retailer_code}
    ).scalar()
    plant_count = verify.execute(
        text("SELECT count(*) FROM common.plant WHERE plant_code = :p"), {"p": plant}
    ).scalar()
    po_count = verify.execute(
        text("SELECT count(*) FROM common.purchase_order WHERE purchase_order_number = :n"),
        {"n": po_number},
    ).scalar()
    line_count = verify.execute(
        text(
            "SELECT count(*) FROM common.purchase_order_line l "
            "JOIN common.purchase_order po ON po.id = l.purchase_order_id "
            "WHERE po.purchase_order_number = :n"
        ),
        {"n": po_number},
    ).scalar()
    verify.close()

    assert retailer_count == 1, "concurrent identical ingest must not create two retailers"
    assert plant_count == 1, "concurrent identical ingest must not create two plants"
    assert po_count == 1, "concurrent identical ingest must not create two purchase orders"
    assert line_count == 1, "concurrent identical ingest must not create two purchase order lines"
