"""Tests for PenaltySummaryRepository (`penalties.penalty_summary`),
including the stranded-pending recovery-sweep queries that used to live on
`JobQueueRepository` as `find_stranded_pending_projection_summaries`/
`find_stranded_pending_mitigation_summaries` (see
app/repositories/penalties/summary.py's module docstring for why they
moved, and why `agent_id` replaces `prompt_version` in every lookup key).
Was covered by tests/unit/repositories/test_job_queue_repository.py against
`app.repositories.fine_projection.summary.FineProjectionSummaryRepository`.

No repository yet exists for the `penalty_job_item_context` extension
table this phase (see app/repositories/process/job_queue.py's module
docstring) -- tests construct that row directly against the ORM model,
matching how a future domain-layer enqueue call would populate it.
"""

from datetime import date
from uuid import uuid4

from app.models import JobItem, JobRun
from app.models.enums import JobItemStatus, JobRunType, JobTaskType, SummaryType
from app.models.penalties import PenaltyJobItemContext
from app.repositories.penalties.summary import PenaltySummaryRepository

_AGENT_ID = uuid4()


def _make_pending_summary(db_session, purchase_order_id, as_of_date: date) -> None:
    PenaltySummaryRepository(db_session).create_pending(
        purchase_order_id=purchase_order_id,
        summary_type=SummaryType.PROJECTION,
        as_of_date=as_of_date,
        agent_id=_AGENT_ID,
        context_hash="deadbeef",
    )
    db_session.commit()


def _make_covering_job_item(db_session, purchase_order_id, projection_date: date, *, item_type: str) -> None:
    run = JobRun(job_type="PENALTY_PROJECTION_BATCH", trigger_type=JobRunType.ON_DEMAND)
    db_session.add(run)
    db_session.flush()

    item = JobItem(job_run_id=run.id, item_type=item_type, status=JobItemStatus.PENDING)
    db_session.add(item)
    db_session.flush()

    db_session.add(
        PenaltyJobItemContext(
            job_item_id=item.id,
            purchase_order_id=purchase_order_id,
            projection_date=projection_date,
            task_type=item_type,
        )
    )
    db_session.flush()
    return item


def test_find_stranded_pending_finds_a_row_with_no_job_item(repos, db_session):
    purchase_order_id = uuid4()
    _make_pending_summary(db_session, purchase_order_id, date(2026, 8, 13))

    stranded = repos.penalty_summaries.find_stranded_pending(
        date(2026, 8, 10), date(2026, 8, 14), SummaryType.PROJECTION
    )

    assert stranded == [{"purchase_order_id": purchase_order_id, "as_of_date": date(2026, 8, 13)}]


def test_find_stranded_pending_excludes_a_row_with_a_job_item(repos, db_session):
    purchase_order_id = uuid4()
    _make_pending_summary(db_session, purchase_order_id, date(2026, 8, 13))
    _make_covering_job_item(
        db_session, purchase_order_id, date(2026, 8, 13), item_type=JobTaskType.PROJECTION_SUMMARY_REGEN
    )
    db_session.commit()

    stranded = repos.penalty_summaries.find_stranded_pending(
        date(2026, 8, 10), date(2026, 8, 14), SummaryType.PROJECTION
    )

    assert stranded == []


def test_find_stranded_pending_excludes_a_row_covered_by_an_order_run(repos, db_session):
    """An ORDER_RUN item produces the summary for the same (PO, date), so
    the ledger row is NOT stranded -- even though no
    PROJECTION_SUMMARY_REGEN context row exists. Matching only on
    PROJECTION_SUMMARY_REGEN would report this row as stranded and let the
    sweep enqueue a second item alongside the live ORDER_RUN."""
    purchase_order_id = uuid4()
    _make_pending_summary(db_session, purchase_order_id, date(2026, 8, 13))
    _make_covering_job_item(db_session, purchase_order_id, date(2026, 8, 13), item_type=JobTaskType.ORDER_RUN)
    db_session.commit()

    stranded = repos.penalty_summaries.find_stranded_pending(
        date(2026, 8, 10), date(2026, 8, 14), SummaryType.PROJECTION
    )

    assert stranded == []


def test_find_stranded_pending_excludes_a_row_outside_the_date_window(repos, db_session):
    purchase_order_id = uuid4()
    _make_pending_summary(db_session, purchase_order_id, date(2026, 8, 1))

    stranded = repos.penalty_summaries.find_stranded_pending(
        date(2026, 8, 10), date(2026, 8, 14), SummaryType.PROJECTION
    )

    assert stranded == []


def test_find_stranded_pending_excludes_ready_and_failed_rows(repos, db_session):
    ready_po = uuid4()
    failed_po = uuid4()

    repos.penalty_summaries.create_pending(
        purchase_order_id=ready_po,
        summary_type=SummaryType.PROJECTION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        context_hash="hash1",
    )
    repos.penalty_summaries.mark_ready(
        purchase_order_id=ready_po,
        summary_type=SummaryType.PROJECTION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        model_name="gpt-test",
        summary="All clear.",
    )
    repos.penalty_summaries.create_pending(
        purchase_order_id=failed_po,
        summary_type=SummaryType.PROJECTION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        context_hash="hash2",
    )
    repos.penalty_summaries.mark_failed(
        purchase_order_id=failed_po,
        summary_type=SummaryType.PROJECTION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        error_message="boom",
    )
    db_session.commit()

    stranded = repos.penalty_summaries.find_stranded_pending(
        date(2026, 8, 10), date(2026, 8, 14), SummaryType.PROJECTION
    )

    assert stranded == []


def test_find_stranded_pending_is_scoped_to_summary_type(repos, db_session):
    """MITIGATION rows are never reported as PROJECTION stranded rows, and
    vice versa -- the merge's `summary_type` discriminator must not leak
    coverage across the two."""
    purchase_order_id = uuid4()
    repos.penalty_summaries.create_pending(
        purchase_order_id=purchase_order_id,
        summary_type=SummaryType.MITIGATION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        context_hash="hash",
    )
    db_session.commit()

    projection_stranded = repos.penalty_summaries.find_stranded_pending(
        date(2026, 8, 10), date(2026, 8, 14), SummaryType.PROJECTION
    )
    mitigation_stranded = repos.penalty_summaries.find_stranded_pending(
        date(2026, 8, 10), date(2026, 8, 14), SummaryType.MITIGATION
    )

    assert projection_stranded == []
    assert mitigation_stranded == [{"purchase_order_id": purchase_order_id, "as_of_date": date(2026, 8, 13)}]


def test_create_pending_resets_a_failed_row_and_get_cached_only_returns_ready(repos, db_session):
    purchase_order_id = uuid4()
    repos.penalty_summaries.create_pending(
        purchase_order_id=purchase_order_id,
        summary_type=SummaryType.MITIGATION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        context_hash="hash1",
    )
    repos.penalty_summaries.mark_failed(
        purchase_order_id=purchase_order_id,
        summary_type=SummaryType.MITIGATION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        error_message="boom",
    )
    assert (
        repos.penalty_summaries.get_cached(purchase_order_id, SummaryType.MITIGATION, date(2026, 8, 13))
        is None
    )

    reset = repos.penalty_summaries.create_pending(
        purchase_order_id=purchase_order_id,
        summary_type=SummaryType.MITIGATION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        context_hash="hash2",
    )
    assert reset["status"] == "PENDING"
    assert reset["error_message"] is None


def test_projection_and_mitigation_summaries_for_the_same_po_and_date_do_not_collide(repos):
    """The merged table's unique constraint is (purchase_order_id,
    summary_type, as_of_date) -- two different summary_types for the same
    PO/date are two distinct rows, not a conflict."""
    purchase_order_id = uuid4()
    repos.penalty_summaries.create_pending(
        purchase_order_id=purchase_order_id,
        summary_type=SummaryType.PROJECTION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        context_hash="p-hash",
    )
    repos.penalty_summaries.create_pending(
        purchase_order_id=purchase_order_id,
        summary_type=SummaryType.MITIGATION,
        as_of_date=date(2026, 8, 13),
        agent_id=_AGENT_ID,
        context_hash="m-hash",
    )

    projection = repos.penalty_summaries.get_by_key(
        purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 13)
    )
    mitigation = repos.penalty_summaries.get_by_key(
        purchase_order_id, SummaryType.MITIGATION, date(2026, 8, 13)
    )

    assert projection["context_hash"] == "p-hash"
    assert mitigation["context_hash"] == "m-hash"
