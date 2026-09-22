"""Integration test for the `PENALTY_FULL_RUN_BATCH` addition's real-Postgres
wiring: the edited `ck_job_item_item_type` CHECK constraint actually
accepting the new `PENALTY_FULL_RUN` value at the DB level (not just
SQLite, which `tests/unit/db/test_migration_parity.py` never checks -- see
that suite's own caveat), the requested `steps` round-tripping through
`process.job_item.metadata` (the new `JobQueueRepository.enqueue` param),
and the `PENALTY_FULL_RUN` worker path (`app.workers.penalty_full_run.
run_full_run`) actually executing all four steps -- projection,
projection summary, mitigation, mitigation summary -- in order against a
real Postgres purchase order.

Mirrors `tests/integration/test_penalty_mitigation_batch_postgres.py`'s
shape exactly (fixtures, skip-if-no-Postgres convention, FK-ordered
cleanup).

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance -- same convention as
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
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.mitigation import MitigationOptionRepository
from app.repositories.penalties.projection import PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.seeding.master_data import ensure_placeholder_retailer_agreement
from app.workers.penalty_full_run import run_full_run


class _FakeAIMessage:
    def __init__(self, content: str | None, tool_calls: list[dict]) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChatClient:
    """Always answers immediately with no tool calls -- same duck-typed
    shape `tests/unit/workers/test_worker_dispatch.py`'s fake uses. No live
    Azure OpenAI call."""

    model_name = "fake-model"

    def __init__(self, final_content: str | None = "All clear.") -> None:
        self.final_content = final_content
        self.invocations: list[dict] = []

    def invoke(self, messages, *, tools=None):
        self.invocations.append({"messages": messages, "tools": tools})
        return _FakeAIMessage(content=self.final_content, tool_calls=[])


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
            "PENALTY_FULL_RUN_BATCH Postgres integration tests."
        )
    yield db
    db.dispose()


@pytest.fixture
def job_run(pg_database: Database):
    """A fresh job_run for one test, deleted (with its job_items) afterward
    -- same fixture shape as tests/integration/test_penalty_mitigation_batch_postgres.py."""
    session = pg_database.new_session()
    repo = JobQueueRepository(session)
    run = repo.create_run(job_type="PENALTY_FULL_RUN_BATCH", trigger_type="MANUAL_BATCH")
    session.commit()
    session.close()

    yield run

    cleanup = pg_database.new_session()
    cleanup.execute(text("DELETE FROM process.job_item WHERE job_run_id = :id"), {"id": run["id"]})
    cleanup.execute(text("DELETE FROM process.job_run WHERE id = :id"), {"id": run["id"]})
    cleanup.commit()
    cleanup.close()


def test_ck_job_item_item_type_accepts_penalty_full_run(pg_database: Database, job_run: dict):
    """Proves the edited `ck_job_item_item_type` CHECK constraint
    (alembic/versions/ff53dabe6e4c_initial_process_schema.py) actually accepts
    `PENALTY_FULL_RUN` against real Postgres -- the one thing
    tests/unit/db/test_migration_parity.py structurally cannot check
    (table/column names only, no constraint bodies)."""
    session = pg_database.new_session()
    try:
        item_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO process.job_item "
                "(id, job_run_id, item_type, dedupe_key, status, attempt_count, max_attempts, metadata) "
                "VALUES (:id, :run_id, 'PENALTY_FULL_RUN', :dedupe_key, 'PENDING', 0, 5, "
                '\'{"steps": ["projection"]}\'::jsonb)'
            ),
            {"id": item_id, "run_id": job_run["id"], "dedupe_key": f"CK-TEST-{item_id}"},
        )
        session.commit()

        row = session.execute(
            text("SELECT item_type, metadata FROM process.job_item WHERE id = :id"), {"id": item_id}
        ).first()
        assert row is not None
        assert row[0] == "PENALTY_FULL_RUN"
        assert row[1] == {"steps": ["projection"]}
    finally:
        session.close()


def test_ck_job_item_item_type_still_rejects_unknown_values(pg_database: Database, job_run: dict):
    """Regression guard: adding `PENALTY_FULL_RUN` to the CHECK list must
    not accidentally have widened it into a no-op."""
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
def open_purchase_order_with_rule(pg_database: Database):
    """A real OPEN purchase order with an active penalty rule and NO
    persisted projection yet -- eligible for `PENALTY_FULL_RUN` when
    `"projection"` is itself one of the requested `steps` (this run
    produces one). Deleted (with its master data) afterward."""
    session = pg_database.new_session()
    master_data = MasterDataRepository(session)
    rules = PenaltyRuleRepository(session)
    purchase_orders = PurchaseOrderRepository(session)

    retailer = master_data.add_retailer("RET-FULL-RUN-IT", "Full Run Integration Test", None, "SUM")
    material = master_data.add_material("MAT-FULL-RUN-IT", None)
    plant = master_data.add_plant("PLANT-FULL-RUN-IT", None, None)
    purchase_order = purchase_orders.create_purchase_order(
        purchase_order_number="PO-FULL-RUN-IT-1",
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
        rule_code="RULE-FULL-RUN-IT",
        violation_type="OTIF_LATE",
        calc_type="FLAT_FEE",
        rate=50.0,
        penalty_category="OTIF_LATE",
        retailer_agreement_id=retailer_agreement_id,
    )
    session.commit()
    session.close()

    yield purchase_order["id"]

    cleanup = pg_database.new_session()
    cleanup.execute(
        text("DELETE FROM penalties.penalty_summary WHERE purchase_order_id = :id"),
        {"id": purchase_order["id"]},
    )
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
        {"rule_code": "RULE-FULL-RUN-IT"},
    )
    cleanup.execute(text("DELETE FROM retailer_agreement WHERE retailer_id = :id"), {"id": retailer["id"]})
    cleanup.execute(text("DELETE FROM retailer WHERE id = :id"), {"id": retailer["id"]})
    cleanup.execute(text("DELETE FROM material WHERE id = :id"), {"id": material["id"]})
    cleanup.execute(text("DELETE FROM plant WHERE id = :id"), {"id": plant["id"]})
    cleanup.commit()
    cleanup.close()


def test_penalty_full_run_worker_executes_all_four_steps_against_real_postgres(
    pg_database: Database, open_purchase_order_with_rule
):
    """End-to-end for the new `PENALTY_FULL_RUN` worker path
    (`app.workers.penalty_full_run.run_full_run`, dispatched from
    `JobTaskType.PENALTY_FULL_RUN` in `app/workers/dispatch.py`): enqueue a
    real `process.job_item`/`penalties.penalty_job_item_context` pair with
    all four `steps` in `process.job_item.metadata` (the same way
    `_trigger_penalty_full_run_batch` would), confirm the job_run/job_item
    rows -- including `metadata` -- are correct, execute the worker
    function against real Postgres, and confirm all four steps actually
    ran: a projection, its summary, mitigation options, and a mitigation
    summary all land in their respective real Postgres tables."""
    purchase_order_id = open_purchase_order_with_rule
    projection_date = date(2026, 1, 5)
    steps = ["mitigation_summary", "mitigation", "projection_summary", "projection"]

    session = pg_database.new_session()
    job_queue = JobQueueRepository(session)
    job_context = PenaltyJobItemContextRepository(session)
    run = job_queue.create_run(job_type="PENALTY_FULL_RUN_BATCH", trigger_type="MANUAL_BATCH")
    dedupe_key = f"{purchase_order_id}:{projection_date.isoformat()}:PENALTY_FULL_RUN"
    item = job_queue.enqueue(
        run["id"],
        item_type="PENALTY_FULL_RUN",
        dedupe_key=dedupe_key,
        max_attempts=5,
        metadata={"steps": steps, "projection_date": projection_date.isoformat()},
    )
    assert item is not None
    assert item["metadata_json"] == {"steps": steps, "projection_date": projection_date.isoformat()}
    job_context.create(
        job_item_id=item["id"],
        purchase_order_id=purchase_order_id,
        projection_date=projection_date,
        task_type="PENALTY_FULL_RUN",
    )
    session.commit()
    session.close()

    # Confirm the job_run/job_item rows round-trip correctly, `metadata` included.
    verify_session = pg_database.new_session()
    try:
        persisted_item = JobQueueRepository(verify_session).get_item(item["id"])
        assert persisted_item is not None
        assert persisted_item["item_type"] == "PENALTY_FULL_RUN"
        assert persisted_item["metadata_json"]["steps"] == steps
    finally:
        verify_session.close()

    job = ClaimedJob(
        job_item_id=item["id"],
        job_run_id=run["id"],
        item_type="PENALTY_FULL_RUN",
        dedupe_key=dedupe_key,
        attempt_count=1,
        max_attempts=5,
    )
    llm = FakeChatClient()

    try:
        run_full_run(job, pg_database, llm, heartbeat=None)

        verify_session = pg_database.new_session()
        try:
            history = PenaltyProjectionRepository(verify_session).list_history(purchase_order_id)
            projection_summary = PenaltySummaryRepository(verify_session).get_by_key(
                purchase_order_id, "PROJECTION", projection_date
            )
            options = MitigationOptionRepository(verify_session).list_for_date(
                purchase_order_id, projection_date
            )
            mitigation_summary = PenaltySummaryRepository(verify_session).get_by_key(
                purchase_order_id, "MITIGATION", projection_date
            )
        finally:
            verify_session.close()

        assert history, "projection step should have persisted a projection against real Postgres"
        assert projection_summary is not None and projection_summary["status"] == "READY"
        assert options, "mitigation step should have persisted mitigation options against real Postgres"
        assert mitigation_summary is not None and mitigation_summary["status"] == "READY"
        assert len(llm.invocations) >= 2, "both summary steps should have called the LLM"
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
