"""Tests for app/workers/po_validation.py -- the recovery path for a
PO_VALIDATION job item left PENDING/RUNNING after ingest_po_lines' own
inline claim+settle didn't happen (see that module's docstring). A fake
`PoValidationService` double stands in -- the graph itself is out of scope
here, same as tests/unit/services/test_po_validation_service.py's FakeGraph.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.core.exceptions import ValidationError
from app.workers.po_validation import replay_stranded_job_run_items


class _FakePoValidationService:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.replayed: list[UUID] = []
        self._raises = raises

    def replay_line(self, purchase_order_line_id: UUID) -> None:
        if self._raises is not None:
            raise self._raises
        self.replayed.append(purchase_order_line_id)


def _seed_pending_po_validation_item(repos, purchase_order_line_id: UUID | None) -> tuple[UUID, UUID]:
    run = repos.job_queue.create_run(job_type="PO_VALIDATION_BATCH", trigger_type="ON_DEMAND")
    item = repos.job_queue.enqueue(
        run["id"],
        item_type="PO_VALIDATION",
        dedupe_key=str(purchase_order_line_id or uuid4()),
        max_attempts=5,
    )
    if purchase_order_line_id is not None:
        repos.cmir_job_item_context.create(
            job_item_id=item["id"], purchase_order_line_id=purchase_order_line_id
        )
    return run["id"], item["id"]


def test_replay_stranded_job_run_items_replays_pending_item(repos):
    po_line_id = uuid4()
    job_run_id, _ = _seed_pending_po_validation_item(repos, po_line_id)
    service = _FakePoValidationService()

    recovered = replay_stranded_job_run_items(
        service,
        repos.job_queue,
        repos.cmir_job_item_context,
        job_run_id,
        worker_id="test-worker",
        visibility_timeout_seconds=300,
    )

    assert recovered == 1
    assert service.replayed == [po_line_id]
    items = repos.job_queue.list_run_items(job_run_id)
    assert items[0]["status"] == "SUCCEEDED"


def test_replay_stranded_job_run_items_marks_dead_on_replay_failure(repos):
    po_line_id = uuid4()
    job_run_id, _ = _seed_pending_po_validation_item(repos, po_line_id)
    service = _FakePoValidationService(
        raises=ValidationError(code="VALIDATION_ERROR", message="no raw_payload")
    )

    recovered = replay_stranded_job_run_items(
        service,
        repos.job_queue,
        repos.cmir_job_item_context,
        job_run_id,
        worker_id="test-worker",
        visibility_timeout_seconds=300,
    )

    assert recovered == 0
    items = repos.job_queue.list_run_items(job_run_id)
    assert items[0]["status"] == "DEAD"


def test_replay_stranded_job_run_items_marks_dead_when_context_missing(repos):
    job_run_id, _ = _seed_pending_po_validation_item(repos, None)
    service = _FakePoValidationService()

    recovered = replay_stranded_job_run_items(
        service,
        repos.job_queue,
        repos.cmir_job_item_context,
        job_run_id,
        worker_id="test-worker",
        visibility_timeout_seconds=300,
    )

    assert recovered == 0
    items = repos.job_queue.list_run_items(job_run_id)
    assert items[0]["status"] == "DEAD"
    assert items[0]["last_error_code"] == "MISSING_CONTEXT"


def test_replay_stranded_job_run_items_returns_zero_when_nothing_pending(repos):
    run = repos.job_queue.create_run(job_type="PO_VALIDATION_BATCH", trigger_type="ON_DEMAND")
    service = _FakePoValidationService()

    recovered = replay_stranded_job_run_items(
        service,
        repos.job_queue,
        repos.cmir_job_item_context,
        run["id"],
        worker_id="test-worker",
        visibility_timeout_seconds=300,
    )

    assert recovered == 0
