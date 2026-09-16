"""Integration test for the `PENALTY_MITIGATION_BATCH` addition's real-Postgres
wiring: the edited `ck_job_item_item_type` CHECK constraint actually accepting
the new `MITIGATION_RUN` value at the DB level (not just SQLite, which
`tests/unit/db/test_migration_parity.py` never checks -- see that suite's own
caveat), and the `MITIGATION_RUN` worker path
(`app.workers.penalty_mitigation.run_mitigation`) round-tripping mitigation
options through a real Postgres `penalties.mitigation_option` table.

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a reachable
Postgres instance -- same convention as
tests/integration/test_job_queue_postgres.py.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import get_settings
from app.db.session import Database
from app.queue.types import ClaimedJob
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.mitigation import MitigationOptionRepository
from app.repositories.penalties.projection import PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.projection.service import ProjectionService
from app.services.seeding.master_data import ensure_placeholder_retailer_agreement
from app.workers.penalty_mitigation import run_mitigation


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
            "No reachable Postgres DATABASE_URL configured -- skipping "
            "PENALTY_MITIGATION_BATCH Postgres integration tests."
        )
    yield db
    db.dispose()


@pytest.fixture
def job_run(pg_database: Database):
    """A fresh job_run for one test, deleted (with its job_items) afterward
    -- same fixture shape as tests/integration/test_job_queue_postgres.py."""
    session = pg_database.new_session()
    repo = JobQueueRepository(session)
    run = repo.create_run(job_type="PENALTY_MITIGATION_BATCH", trigger_type="MANUAL_BATCH")
    session.commit()
    session.close()

    yield run

    cleanup = pg_database.new_session()
    cleanup.execute(text("DELETE FROM process.job_item WHERE job_run_id = :id"), {"id": run["id"]})
    cleanup.execute(text("DELETE FROM process.job_run WHERE id = :id"), {"id": run["id"]})
    cleanup.commit()
    cleanup.close()


def test_ck_job_item_item_type_accepts_mitigation_run(pg_database: Database, job_run: dict):
    """Proves the edited `ck_job_item_item_type` CHECK constraint
    (alembic/versions/ff53dabe6e4c_initial_process_schema.py) actually accepts
    `MITIGATION_RUN` against real Postgres -- the one thing
    tests/unit/db/test_migration_parity.py structurally cannot check (table/
    column names only, no constraint bodies)."""
    session = pg_database.new_session()
    try:
        item_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO process.job_item "
                "(id, job_run_id, item_type, dedupe_key, status, attempt_count, max_attempts, metadata) "
                "VALUES (:id, :run_id, 'MITIGATION_RUN', :dedupe_key, 'PENDING', 0, 5, '{}'::jsonb)"
            ),
            {"id": item_id, "run_id": job_run["id"], "dedupe_key": f"CK-TEST-{item_id}"},
        )
        session.commit()

        row = session.execute(
            text("SELECT item_type FROM process.job_item WHERE id = :id"), {"id": item_id}
        ).first()
        assert row is not None
        assert row[0] == "MITIGATION_RUN"
    finally:
        session.close()


def test_ck_job_item_item_type_still_rejects_unknown_values(pg_database: Database, job_run: dict):
    """Regression guard: adding `MITIGATION_RUN` to the CHECK list must not
    accidentally have widened it into a no-op."""
    session = pg_database.new_session()
    try:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO process.job_item "
                    "(id, job_run_id, item_type, dedupe_key, status, attempt_count, max_attempts, metadata) "
                    "VALUES (:id, :run_id, 'NOT_A_REAL_ITEM_TYPE', :dedupe_key, 'PENDING', 0, 5, '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "run_id": job_run["id"], "dedupe_key": "CK-TEST-REJECT"},
            )
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def purchase_order_with_projection(pg_database: Database):
    """A real purchase order, with an active penalty rule and one persisted
    `penalties.penalty_projection` row -- eligible for `MITIGATION_RUN`, the
    same precondition `MitigationService.run_for_purchase_order` itself
    checks. Deleted (with its master data) afterward."""
    session = pg_database.new_session()
    master_data = MasterDataRepository(session)
    rules = PenaltyRuleRepository(session)
    purchase_orders = PurchaseOrderRepository(session)

    retailer = master_data.add_retailer("RET-MIT-BATCH-IT", "Mitigation Batch Integration Test", None, "SUM")
    material = master_data.add_material("MAT-MIT-BATCH-IT", None)
    plant = master_data.add_plant("PLANT-MIT-BATCH-IT", None, None)
    purchase_order = purchase_orders.create_purchase_order(
        purchase_order_number="PO-MIT-BATCH-IT-1",
        retailer_id=retailer["id"],
        order_date=date(2026, 1, 1),
        requested_delivery_date=date(2026, 1, 10),
        required_ship_date=date(2026, 1, 8),
    )
    purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=5.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    retailer_agreement_id = ensure_placeholder_retailer_agreement(
        RetailerAgreementRepository(session), retailer
    )
    rules.add_rule(
        rule_code="RULE-MIT-BATCH-IT",
        violation_type="OTIF_LATE",
        calc_type="FLAT_FEE",
        rate=50.0,
        penalty_category="OTIF_LATE",
        retailer_agreement_id=retailer_agreement_id,
    )

    projection_date = date(2026, 1, 5)
    projection_service = ProjectionService(
        purchase_orders=purchase_orders,
        fulfillment=FulfillmentRepository(session),
        rules=rules,
        master_data=master_data,
        projections=PenaltyProjectionRepository(session),
    )
    projection_service.run_for_purchase_order(purchase_order["id"], projection_date)
    session.commit()
    session.close()

    yield purchase_order["id"], projection_date

    cleanup = pg_database.new_session()
    cleanup.execute(
        text("DELETE FROM penalties.mitigation_option WHERE purchase_order_id = :id"),
        {"id": purchase_order["id"]},
    )
    cleanup.execute(
        text("DELETE FROM penalties.mitigation_input WHERE purchase_order_id = :id"),
        {"id": purchase_order["id"]},
    )
    cleanup.execute(
        text("DELETE FROM penalties.penalty_job_item_context WHERE purchase_order_id = :id"),
        {"id": purchase_order["id"]},
    )
    cleanup.execute(
        text("DELETE FROM penalties.penalty_projection WHERE purchase_order_id = :id"),
        {"id": purchase_order["id"]},
    )
    cleanup.execute(
        text("DELETE FROM purchase_order_line WHERE purchase_order_id = :id"), {"id": purchase_order["id"]}
    )
    cleanup.execute(text("DELETE FROM purchase_order WHERE id = :id"), {"id": purchase_order["id"]})
    cleanup.execute(
        text("DELETE FROM penalties.penalty_rule WHERE rule_code = :rule_code"),
        {"rule_code": "RULE-MIT-BATCH-IT"},
    )
    cleanup.execute(
        text("DELETE FROM retailer_agreement WHERE retailer_id = :id"), {"id": retailer["id"]}
    )
    cleanup.execute(text("DELETE FROM retailer WHERE id = :id"), {"id": retailer["id"]})
    cleanup.execute(text("DELETE FROM material WHERE id = :id"), {"id": material["id"]})
    cleanup.execute(text("DELETE FROM plant WHERE id = :id"), {"id": plant["id"]})
    cleanup.commit()
    cleanup.close()


def test_mitigation_run_worker_persists_options_against_real_postgres(
    pg_database: Database, purchase_order_with_projection: tuple
):
    """End-to-end for the new `MITIGATION_RUN` worker path
    (`app.workers.penalty_mitigation.run_mitigation`, dispatched from
    `JobTaskType.MITIGATION_RUN` in `app/workers/dispatch.py`): enqueue a
    real `process.job_item`/`penalties.penalty_job_item_context` pair the
    same way `_trigger_penalty_mitigation_batch` does, execute the worker
    function against real Postgres, and confirm mitigation options actually
    land in `penalties.mitigation_option`."""
    purchase_order_id, projection_date = purchase_order_with_projection

    session = pg_database.new_session()
    job_queue = JobQueueRepository(session)
    job_context = PenaltyJobItemContextRepository(session)
    run = job_queue.create_run(job_type="PENALTY_MITIGATION_BATCH", trigger_type="MANUAL_BATCH")
    dedupe_key = f"{purchase_order_id}:{projection_date.isoformat()}:MITIGATION_RUN"
    item = job_queue.enqueue(run["id"], item_type="MITIGATION_RUN", dedupe_key=dedupe_key, max_attempts=5)
    assert item is not None
    job_context.create(
        job_item_id=item["id"],
        purchase_order_id=purchase_order_id,
        projection_date=projection_date,
        task_type="MITIGATION_RUN",
    )
    session.commit()
    session.close()

    job = ClaimedJob(
        job_item_id=item["id"],
        job_run_id=run["id"],
        item_type="MITIGATION_RUN",
        dedupe_key=dedupe_key,
        attempt_count=1,
        max_attempts=5,
    )

    try:
        run_mitigation(job, pg_database)

        verify_session = pg_database.new_session()
        try:
            options = MitigationOptionRepository(verify_session).list_for_date(
                purchase_order_id, projection_date
            )
        finally:
            verify_session.close()

        assert options, "mitigation options should have been persisted against real Postgres"
    finally:
        cleanup = pg_database.new_session()
        # penalty_job_item_context FKs into job_item -- must go first.
        cleanup.execute(
            text("DELETE FROM penalties.penalty_job_item_context WHERE job_item_id = :id"),
            {"id": item["id"]},
        )
        cleanup.execute(text("DELETE FROM process.job_item WHERE job_run_id = :id"), {"id": run["id"]})
        cleanup.execute(text("DELETE FROM process.job_run WHERE id = :id"), {"id": run["id"]})
        cleanup.commit()
        cleanup.close()
