"""Integration test for Gap 1/Gap 2's real-Postgres wiring: the
`PO_VALIDATION` job-queue trail (`process.job_run`/`job_item`/
`cmir.cmir_job_item_context`) and `process.processing_error.
purchase_order_line_id` actually round-tripping against live Postgres, not
just SQLite (see tests/unit/db/test_migration_parity.py's own caveat --
that suite proves table/column NAME parity only, never live constraint/FK
behavior).

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance -- same convention as
tests/integration/test_job_queue_postgres.py.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.cmir.job_context import CmirJobItemContextRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.repositories.process.workflow import ProcessingErrorRepository


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
            "No reachable Postgres DATABASE_URL configured -- skipping PO-validation "
            "job-queue Postgres integration tests."
        )
    yield db
    db.dispose()


@pytest.fixture
def purchase_order_line(pg_database: Database):
    """A real purchase_order_line row (public schema), deleted (with its
    header and master data) afterward."""
    session = pg_database.new_session()
    master_data = MasterDataRepository(session)
    purchase_orders = PurchaseOrderRepository(session)

    retailer = master_data.add_retailer("RET-POV-IT", "PO Validation Integration Test", None, "SUM")
    plant = master_data.add_plant("PLANT-POV-IT", None, None)
    purchase_order = purchase_orders.create_purchase_order(
        purchase_order_number="PO-POV-IT-1", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    line = purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
        line_status="READY_FOR_SO_CREATION",
    )
    session.commit()
    session.close()

    yield line

    cleanup = pg_database.new_session()
    cleanup.execute(
        text("DELETE FROM process.processing_error WHERE purchase_order_line_id = :id"), {"id": line["id"]}
    )
    cleanup.execute(
        text("DELETE FROM cmir.cmir_job_item_context WHERE purchase_order_line_id = :id"), {"id": line["id"]}
    )
    cleanup.execute(text("DELETE FROM purchase_order_line WHERE id = :id"), {"id": line["id"]})
    cleanup.execute(text("DELETE FROM purchase_order WHERE id = :id"), {"id": purchase_order["id"]})
    cleanup.execute(text("DELETE FROM retailer WHERE id = :id"), {"id": retailer["id"]})
    cleanup.execute(text("DELETE FROM plant WHERE id = :id"), {"id": plant["id"]})
    cleanup.commit()
    cleanup.close()


def test_po_validation_job_item_context_round_trips(pg_database: Database, purchase_order_line: dict):
    """Gap 1: a real process.job_run/job_item plus its
    cmir_job_item_context.purchase_order_line_id row, claimed and settled
    end to end against live Postgres."""
    session = pg_database.new_session()
    try:
        job_queue = JobQueueRepository(session)
        job_item_context = CmirJobItemContextRepository(session)

        run = job_queue.create_run(job_type="PO_VALIDATION_BATCH", trigger_type="ON_DEMAND")
        item = job_queue.enqueue(
            run["id"],
            item_type="PO_VALIDATION",
            dedupe_key=str(purchase_order_line["id"]),
            max_attempts=5,
        )
        assert item is not None
        job_item_context.create(job_item_id=item["id"], purchase_order_line_id=purchase_order_line["id"])
        session.commit()

        context = job_item_context.get(item["id"])
        assert context is not None
        assert context["purchase_order_line_id"] == purchase_order_line["id"]
        assert context["email_event_id"] is None

        claimed = job_queue.claim_batch("integration-test-worker", 1, job_item_ids=[item["id"]])
        assert len(claimed) == 1
        settled = job_queue.mark_succeeded(item["id"], "integration-test-worker")
        session.commit()
        assert settled["status"] == "SUCCEEDED"

        summary = job_queue.get_run_summary(run["id"])
        assert summary["counts"]["SUCCEEDED"] == 1
    finally:
        cleanup = pg_database.new_session()
        cleanup.execute(
            text("DELETE FROM cmir.cmir_job_item_context WHERE job_item_id = :id"), {"id": item["id"]}
        )
        cleanup.execute(text("DELETE FROM process.job_item WHERE job_run_id = :id"), {"id": run["id"]})
        cleanup.execute(text("DELETE FROM process.job_run WHERE id = :id"), {"id": run["id"]})
        cleanup.commit()
        cleanup.close()
        session.close()


def test_processing_error_purchase_order_line_id_round_trips(
    pg_database: Database, purchase_order_line: dict
):
    """Gap 2: process.processing_error.purchase_order_line_id -- the
    edited-in-place migration's new column and FK -- actually exist and are
    queryable against live Postgres."""
    session = pg_database.new_session()
    try:
        processing_errors = ProcessingErrorRepository(session)
        processing_errors.log(
            "LOOKUP_FAILURE",
            purchase_order_line_id=purchase_order_line["id"],
            node_name="check_material_master",
            error_message="no material_master row",
        )
        session.commit()

        errors = processing_errors.list_for_purchase_order_line(purchase_order_line["id"])
        assert len(errors) == 1
        assert errors[0]["purchase_order_line_id"] == purchase_order_line["id"]
    finally:
        session.close()


def test_list_lines_by_status_finds_seeded_line(pg_database: Database, purchase_order_line: dict):
    """Gap 3: PurchaseOrderRepository.list_lines_by_status against live
    Postgres."""
    session = pg_database.new_session()
    try:
        purchase_orders = PurchaseOrderRepository(session)
        items, _ = purchase_orders.list_lines_by_status("READY_FOR_SO_CREATION", limit=200)
        assert purchase_order_line["id"] in {row["id"] for row in items}
    finally:
        session.close()
