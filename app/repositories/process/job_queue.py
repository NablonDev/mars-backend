"""Repository for shared process.job_run and process.job_item batch queue."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import JobItem, JobRun
from app.models.enums import JobItemStatus
from app.utils.clock import utc_now_naive

# A job is never left in FAILED: retryable failures return to PENDING.
_INFLIGHT_STATUSES = (JobItemStatus.PENDING, JobItemStatus.RUNNING)
_ALL_STATUSES = tuple(JobItemStatus)

_CLAIM_BATCH_SQL = text(
    f"""
    UPDATE process.job_item
    SET status='{JobItemStatus.RUNNING.value}',
        locked_by=:worker_id, locked_at=now(), heartbeat_at=now(),
        attempt_count=attempt_count+1, updated_at=now()
    WHERE id IN (
        SELECT id
        FROM process.job_item
        WHERE status='{JobItemStatus.PENDING.value}'
          AND available_at <= now()
          AND (:ids_is_null OR id = ANY(CAST(:ids AS uuid[])))
        ORDER BY available_at, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, job_run_id, item_type, dedupe_key, status,
              attempt_count, max_attempts, available_at, locked_by, locked_at,
              heartbeat_at, dispatched_at, last_error, last_error_code,
              completed_at, metadata AS metadata_json, created_at, updated_at
    """
)


def _utcnow() -> datetime:
    return utc_now_naive()


def _to_dict(row: JobItem) -> dict:
    """Project a `JobItem` row onto the plain dict shape returned to callers."""
    return {
        "id": row.id,
        "job_run_id": row.job_run_id,
        "item_type": row.item_type,
        "dedupe_key": row.dedupe_key,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "max_attempts": row.max_attempts,
        "available_at": row.available_at,
        "locked_by": row.locked_by,
        "locked_at": row.locked_at,
        "heartbeat_at": row.heartbeat_at,
        "dispatched_at": row.dispatched_at,
        "last_error": row.last_error,
        "last_error_code": row.last_error_code,
        "completed_at": row.completed_at,
        "metadata_json": row.metadata_json,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _run_to_dict(row: JobRun) -> dict:
    """Project a `JobRun` row onto the plain dict shape returned to callers."""
    return {
        "id": row.id,
        "job_type": row.job_type,
        "trigger_type": row.trigger_type,
        "requested_item_count": row.requested_item_count,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "error": row.error,
        "metadata_json": row.metadata_json,
        "created_at": row.created_at,
    }


class JobQueueRepository:
    """The shared `job_run`/`job_item` batch queue behind the CMIR and PO-validation workers.

    Postgres gets the concurrency-safe paths (`SKIP LOCKED` claims, partial
    unique indexes for dedupe); SQLite, used only in unit tests, falls back
    to single-threaded equivalents that exercise the same happy-path
    semantics without the same concurrency guarantees.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _is_postgres(self) -> bool:
        """Branch point for the Postgres-only fast paths that have no portable SQLite equivalent."""
        return self._session.bind is not None and self._session.bind.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def create_run(
        self,
        job_type: str,
        trigger_type: str,
        requested_item_count: int = 0,
    ) -> dict:
        """Open a new job run."""
        row = JobRun(job_type=job_type, trigger_type=trigger_type, requested_item_count=requested_item_count)
        self._session.add(row)
        self._session.flush()
        return _run_to_dict(row)

    def set_requested_item_count(self, job_run_id: UUID, count: int) -> None:
        """Set the run count to the number of items actually enqueued.

        Called after enqueueing, since `create_run` usually records a placeholder
        count. A no-op when the run no longer exists.
        """
        run = self._session.get(JobRun, job_run_id)
        if run is not None:
            run.requested_item_count = count
            self._session.flush()

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def _find_inflight(self, dedupe_key: str) -> JobItem | None:
        """Find the PENDING/RUNNING row already claiming `dedupe_key`, if any."""
        return self._session.scalars(
            select(JobItem).where(
                JobItem.dedupe_key == dedupe_key,
                JobItem.status.in_(_INFLIGHT_STATUSES),
            )
        ).first()

    def enqueue(
        self,
        job_run_id: UUID,
        item_type: str,
        dedupe_key: str | None = None,
        *,
        max_attempts: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict | None:
        """Insert one job, returning the existing in-flight row for a live `dedupe_key`.

        The partial unique index (`dedupe_key IS NOT NULL`) closes the
        concurrent-insert race on Postgres, and a `None` dedupe_key never dedupes.
        Returns `None` when the winning row turns terminal before the loser re-reads
        it. `metadata` lands on `job_item.metadata_json`, defaulting to `{}`.
        """
        if dedupe_key is not None:
            existing = self._find_inflight(dedupe_key)
            if existing is not None:
                return _to_dict(existing)

        row = JobItem(
            job_run_id=job_run_id,
            item_type=item_type,
            dedupe_key=dedupe_key,
            status=JobItemStatus.PENDING,
            max_attempts=max_attempts,
            metadata_json=metadata or {},
        )
        self._session.add(row)

        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            if dedupe_key is None:
                raise
            existing = self._find_inflight(dedupe_key)
            return _to_dict(existing) if existing is not None else None

        return _to_dict(row)

    def enqueue_many(self, job_run_id: UUID, items: Sequence[dict[str, Any]], *, max_attempts: int) -> int:
        """Bulk-enqueue jobs, skipping dedupe_keys already in flight.

        On Postgres this is one `INSERT ... ON CONFLICT DO NOTHING` against
        the partial unique index on `dedupe_key`, so concurrent callers can't
        double-enqueue the same key even under load. On SQLite (unit tests
        only) the check is done in Python against the current in-flight set before
        inserting, which is idempotent only because tests run single-threaded. Returns
        the number of rows actually inserted, not the number of items passed in.
        """
        if not items:
            return 0

        rows = [
            {
                "job_run_id": job_run_id,
                "item_type": item["item_type"],
                "dedupe_key": item.get("dedupe_key"),
                "status": JobItemStatus.PENDING,
                "max_attempts": max_attempts,
            }
            for item in items
        ]

        if self._is_postgres():
            # The partial unique index provides concurrent idempotency.
            stmt = pg_insert(JobItem).values(rows)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["dedupe_key"],
                index_where=JobItem.status.in_(_INFLIGHT_STATUSES),
            )
            result = cast(CursorResult, self._session.execute(stmt))
            self._session.flush()
            return result.rowcount if result.rowcount is not None else 0

        # SQLite has no migration-only partial index here, so this fallback
        # is idempotent only for the single-threaded test environment.
        existing_keys = {
            r.dedupe_key
            for r in self._session.scalars(
                select(JobItem).where(JobItem.status.in_(_INFLIGHT_STATUSES), JobItem.dedupe_key.isnot(None))
            ).all()
        }
        to_insert: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows:
            key = r["dedupe_key"]
            if key is not None and (key in existing_keys or key in seen):
                continue
            if key is not None:
                seen.add(key)
            to_insert.append(r)

        if to_insert:
            self._session.execute(insert(JobItem), to_insert)
            self._session.flush()
        return len(to_insert)

    # ------------------------------------------------------------------
    # Claim / heartbeat / completion
    # ------------------------------------------------------------------

    def claim_batch(
        self,
        worker_id: str,
        limit: int,
        job_item_ids: Sequence[UUID] | None = None,
    ) -> list[dict]:
        """Claim up to `limit` eligible jobs for `worker_id`.

        With `job_item_ids=None` this claims the oldest eligible jobs; otherwise it
        restricts claims to the supplied ids. Postgres increments `attempt_count`
        atomically with the claim.
        """
        if self._is_postgres():
            result = self._session.execute(
                _CLAIM_BATCH_SQL,
                {
                    "worker_id": worker_id,
                    "ids_is_null": job_item_ids is None,
                    "ids": list(job_item_ids) if job_item_ids else [],
                    "limit": limit,
                },
            )
            claimed = [dict(row._mapping) for row in result]
            self._session.flush()
            return claimed

        # SQLite cannot provide SKIP LOCKED; this tests claim semantics,
        # not concurrent safety. PostgreSQL integration tests cover that.
        now = _utcnow()
        stmt = select(JobItem).where(
            JobItem.status == JobItemStatus.PENDING,
            JobItem.available_at <= now,
        )
        if job_item_ids is not None:
            stmt = stmt.where(JobItem.id.in_(job_item_ids))
        stmt = stmt.order_by(JobItem.available_at.asc(), JobItem.id.asc()).limit(limit)

        rows = self._session.scalars(stmt).all()
        for row in rows:
            row.status = JobItemStatus.RUNNING
            row.locked_by = worker_id
            row.locked_at = now
            row.heartbeat_at = now
            row.attempt_count += 1
            row.updated_at = now
        self._session.flush()
        return [_to_dict(row) for row in rows]

    def heartbeat(self, job_item_id: UUID, worker_id: str) -> bool:
        """Refresh the lock heartbeat; return False if ownership was lost."""
        now = _utcnow()
        result = cast(
            CursorResult,
            self._session.execute(
                update(JobItem)
                .where(JobItem.id == job_item_id, JobItem.locked_by == worker_id)
                .values(heartbeat_at=now, updated_at=now)
            ),
        )
        self._session.flush()
        return result.rowcount > 0

    def mark_succeeded(self, job_item_id: UUID, worker_id: str) -> dict | None:
        """Mark a claimed job SUCCEEDED, returning None if `worker_id` no longer holds its lock."""
        row = self._session.get(JobItem, job_item_id)
        if row is None or row.locked_by != worker_id:
            return None

        now = _utcnow()
        row.status = JobItemStatus.SUCCEEDED
        row.completed_at = now
        row.updated_at = now
        self._session.flush()
        return _to_dict(row)

    def mark_failed(
        self,
        job_item_id: UUID,
        worker_id: str,
        error: str,
        error_code: str | None,
        retry_in_seconds: int,
    ) -> dict | None:
        """Retry a failed job or move it to DEAD when attempts are exhausted."""
        row = self._session.get(JobItem, job_item_id)
        if row is None or row.locked_by != worker_id:
            return None

        now = _utcnow()
        row.last_error = error
        row.last_error_code = error_code
        row.updated_at = now

        if row.attempt_count >= row.max_attempts:
            row.status = JobItemStatus.DEAD
            row.completed_at = now
        else:
            row.status = JobItemStatus.PENDING
            row.available_at = now + timedelta(seconds=retry_in_seconds)
            row.locked_by = None

        self._session.flush()
        return _to_dict(row)

    def mark_dead(
        self,
        job_item_id: UUID,
        worker_id: str,
        error: str,
        error_code: str | None,
    ) -> dict | None:
        """Move a job directly to DEAD, regardless of retry count.

        For failures where retrying is pointless, such as a permanently invalid
        payload. Returns None when `worker_id` no longer holds the job's lock.
        """
        row = self._session.get(JobItem, job_item_id)
        if row is None or row.locked_by != worker_id:
            return None

        now = _utcnow()
        row.status = JobItemStatus.DEAD
        row.last_error = error
        row.last_error_code = error_code
        row.completed_at = now
        row.updated_at = now
        self._session.flush()
        return _to_dict(row)

    def release(self, job_item_id: UUID, worker_id: str) -> dict | None:
        """Return a claimed job to PENDING without consuming an attempt."""
        row = self._session.get(JobItem, job_item_id)
        if row is None or row.locked_by != worker_id:
            return None

        now = _utcnow()
        row.status = JobItemStatus.PENDING
        row.available_at = now
        row.locked_by = None
        row.updated_at = now
        self._session.flush()
        return _to_dict(row)

    def reclaim_stale(self, visibility_timeout_seconds: int) -> int:
        """Return abandoned RUNNING jobs to PENDING.

        Uses heartbeat time, or locked time when no heartbeat was recorded.
        The cutoff is computed in Python for SQLite/Postgres portability.
        """
        cutoff = _utcnow() - timedelta(seconds=visibility_timeout_seconds)
        now = _utcnow()
        stmt = (
            update(JobItem)
            .where(
                JobItem.status == JobItemStatus.RUNNING,
                (
                    (JobItem.heartbeat_at.isnot(None) & (JobItem.heartbeat_at < cutoff))
                    | (JobItem.heartbeat_at.is_(None) & (JobItem.locked_at < cutoff))
                ),
            )
            .values(status=JobItemStatus.PENDING, locked_by=None, updated_at=now)
        )
        result = cast(CursorResult, self._session.execute(stmt))
        self._session.flush()
        return result.rowcount if result.rowcount is not None else 0

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_item(self, job_item_id: UUID) -> dict | None:
        """Fetch one `job_item` row, `metadata_json` included, which `ClaimedJob` omits."""
        row = self._session.get(JobItem, job_item_id)
        return _to_dict(row) if row is not None else None

    def get_run_summary(self, job_run_id: UUID) -> dict:
        """Return per-status item counts for a run, alongside its requested and actual total item counts."""
        run = self._session.get(JobRun, job_run_id)
        requested_item_count = run.requested_item_count if run is not None else 0

        counts = dict.fromkeys(_ALL_STATUSES, 0)
        for status, count in self._session.execute(
            select(JobItem.status, func.count(JobItem.id))
            .where(JobItem.job_run_id == job_run_id)
            .group_by(JobItem.status)
        ).all():
            counts[status] = count

        return {
            "job_run_id": job_run_id,
            "requested_item_count": requested_item_count,
            "counts": counts,
            "total_items": sum(counts.values()),
        }

    def list_run_items(
        self,
        job_run_id: UUID,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Page through a run's items, oldest first, optionally filtered to one status."""
        stmt = select(JobItem).where(JobItem.job_run_id == job_run_id)
        if status is not None:
            stmt = stmt.where(JobItem.status == status)
        stmt = stmt.order_by(JobItem.created_at.asc(), JobItem.id.asc()).limit(limit).offset(offset)

        rows = self._session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Advisory lock
    # ------------------------------------------------------------------

    def try_advisory_lock(self, key: int) -> bool:
        """Attempt to take a session-scoped Postgres advisory lock on `key`; always succeeds on SQLite."""
        if self._is_postgres():
            return bool(
                self._session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            )

        # SQLite has no cross-connection advisory lock; tests are
        # single-threaded, so the fallback only exercises the happy path.
        return True

    def release_advisory_lock(self, key: int) -> None:
        """Release the advisory lock on `key` taken by `try_advisory_lock`; a no-op on SQLite."""
        if self._is_postgres():
            self._session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def truncate_all(self) -> None:
        """Delete every job_item, then every job_run (child before parent)."""
        self._session.execute(delete(JobItem))
        self._session.execute(delete(JobRun))
        self._session.flush()
