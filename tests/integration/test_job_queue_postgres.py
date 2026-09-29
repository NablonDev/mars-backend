"""Integration tests for the job queue against a real Postgres instance.

Covers exactly what the SQLite unit suite
(tests/unit/repositories/test_job_queue_repository.py) cannot: real
cross-connection concurrency (`FOR UPDATE SKIP LOCKED` actually preventing
two workers from claiming the same row) and the migration-only partial
unique index (`uq_job_item_inflight`) actually being enforced at the DB
level, not just by the repository's own application-level pre-check.

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance -- this suite must never fail CI/local runs
that don't have Postgres available.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.process.job_queue import JobQueueRepository


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
            "No reachable Postgres DATABASE_URL configured -- skipping job-queue Postgres integration tests."
        )
    yield db
    db.dispose()


@pytest.fixture
def job_run(pg_database: Database):
    """A fresh job_run for one test, deleted (with its job_items) afterward."""
    session = pg_database.new_session()
    repo = JobQueueRepository(session)
    run = repo.create_run(job_type="PENALTY_PROJECTION_BATCH", trigger_type="MANUAL_BATCH")
    session.commit()
    session.close()

    yield run

    cleanup = pg_database.new_session()
    cleanup.execute(text("DELETE FROM process.job_item WHERE job_run_id = :id"), {"id": run["id"]})
    cleanup.execute(text("DELETE FROM process.job_run WHERE id = :id"), {"id": run["id"]})
    cleanup.commit()
    cleanup.close()


def test_claim_batch_concurrent_workers_never_claim_overlapping_rows(pg_database: Database, job_run: dict):
    n_items = 40
    n_workers = 8

    setup_session = pg_database.new_session()
    repo = JobQueueRepository(setup_session)
    enqueued_ids = []
    for i in range(n_items):
        result = repo.enqueue(
            job_run["id"],
            "PROJECTION_SUMMARY_REGEN",
            dedupe_key=f"CONCURRENCY-TEST-{i}",
            max_attempts=5,
        )
        assert result is not None
        enqueued_ids.append(result["id"])
    setup_session.commit()
    setup_session.close()

    barrier = threading.Barrier(n_workers)
    results: list[list[str]] = [[] for _ in range(n_workers)]
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        session = pg_database.new_session()
        try:
            repo = JobQueueRepository(session)
            barrier.wait(timeout=10)
            # Scoped to this test's own rows. An unscoped claim takes the
            # oldest eligible rows table-wide, which against a shared dev
            # database means claiming real queued work and stranding it in
            # RUNNING -- the concurrency guarantee under test is unaffected
            # either way, since SKIP LOCKED still arbitrates between these
            # eight workers over the same 40 rows.
            claimed = repo.claim_batch(f"worker-{idx}", limit=n_items, job_item_ids=enqueued_ids)
            session.commit()
            results[idx] = [str(item["id"]) for item in claimed]
        except BaseException as exc:  # noqa: BLE001 -- surfaced via `errors`, not swallowed
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors

    all_claimed = [item_id for worker_result in results for item_id in worker_result]
    assert set(all_claimed) == {str(i) for i in enqueued_ids}, (
        "workers did not claim exactly this test's own items"
    )
    assert len(all_claimed) == n_items, "not every enqueued item was claimed"
    assert len(all_claimed) == len(set(all_claimed)), (
        "two workers claimed the same job_item -- FOR UPDATE SKIP LOCKED failed"
    )


def test_partial_unique_index_rejects_second_inflight_row_but_allows_after_terminal(
    pg_database: Database, job_run: dict
):
    session = pg_database.new_session()
    item_type, dedupe_key = "ORDER_RUN", "INFLIGHT-DEDUPE-TEST"

    try:
        first = JobQueueRepository(session).enqueue(
            job_run["id"], item_type, dedupe_key=dedupe_key, max_attempts=5
        )
        session.commit()
        assert first is not None

        # Raw insert, deliberately bypassing the repository's own
        # application-level pre-check, to prove the DB-level partial
        # unique index (`uq_job_item_inflight`) itself is what rejects a
        # second in-flight row for the same (item_type, dedupe_key).
        # attempt_count/max_attempts/metadata are supplied explicitly (matching
        # the ORM-level defaults in app/models/process/job.py::JobItem) so this
        # insert reaches the partial unique index instead of failing the
        # NOT NULL constraint on those columns first -- those aren't
        # `server_default`s at the DB level, so a raw insert that omits them
        # would raise an IntegrityError of its own, for the wrong reason.
        with pytest.raises(IntegrityError) as exc_info:
            session.execute(
                text(
                    "INSERT INTO process.job_item "
                    "(id, job_run_id, item_type, dedupe_key, status, attempt_count, max_attempts, metadata) "
                    "VALUES (:id, :run_id, :item_type, :dedupe_key, 'PENDING', 0, 5, '{}'::jsonb)"
                ),
                {
                    "id": uuid.uuid4(),
                    "run_id": job_run["id"],
                    "item_type": item_type,
                    "dedupe_key": dedupe_key,
                },
            )
        assert "uq_job_item_inflight" in str(exc_info.value), (
            "IntegrityError was not raised by the uq_job_item_inflight partial "
            f"unique index: {exc_info.value}"
        )
        session.rollback()

        # Move the first row to a terminal state directly via raw SQL --
        # not through mark_dead, which requires locked_by to match a
        # worker_id, irrelevant to what this test is proving.
        session.execute(
            text("UPDATE process.job_item SET status = 'DEAD' WHERE id = :id"), {"id": first["id"]}
        )
        session.commit()

        # Now that the only prior row for this key is terminal, a second
        # in-flight row for the same key is permitted.
        second = JobQueueRepository(session).enqueue(
            job_run["id"], item_type, dedupe_key=dedupe_key, max_attempts=5
        )
        session.commit()
        assert second is not None
        assert second["id"] != first["id"]
    finally:
        session.close()
