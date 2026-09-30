"""Exit-code semantics for scripts/ops/run_daily_batch.py against Azure
Container Apps Jobs -- see that module's own docstring for the full
rationale. Everything below the CLI entry point is mocked (`Database`,
the job-queue backend, the LLM client, `process_jobs`,
`enqueue_daily_run`, the recovery sweep) so this file tests only
`main()`'s control flow / exit-code decisions -- each underlying
component already has its own dedicated test module (worker loop:
test_worker_loop.py; repository: test_job_queue_repository.py; recovery
sweep: test_worker_penalty_projection.py).

Was written against the pre-restructure `app.repositories.job_queue`/
`app.workers.fine_projection` -- rewritten against
`app.repositories.process.job_queue`/`app.workers.penalty_projection`
(the `fine`/`fines` -> `penalty`/`penalties` rename).
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

import scripts.ops.run_daily_batch as batch_script
from app.queue.types import SweepResult
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.timeline.service import TimelineRunSummary
from app.workers.loop import WorkerLoopSummary
from app.workers.penalty_projection import EnqueueResult


def _invoke_main() -> int:
    """Mirrors what `sys.exit(main())` actually does at the OS level: an
    uncaught exception propagating out of `main()` is what makes a plain
    `python script.py` invocation exit non-zero -- not a caught-and-
    returned code. Reproducing that here (rather than asserting
    `pytest.raises`) is what lets every test in this file assert on a
    single exit-code integer regardless of which path produced it."""
    try:
        return batch_script.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException:  # noqa: BLE001 -- deliberately mirrors Python's own default
        # excepthook behavior for an uncaught exception (exit code 1), not a bug to fix
        return 1


class _FakeDispatcher:
    def dispatch(self, job_item_id, *, delay_seconds: int = 0) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeSource(_FakeDispatcher):
    def reclaim_stale(self, visibility_timeout_seconds: int) -> int:
        return 0


def _fake_enqueue_daily_run(*args, **kwargs) -> EnqueueResult:
    return EnqueueResult(job_run_id=uuid4(), purchase_order_count=0, enqueued_count=0)


def _fake_sweep(*args, **kwargs) -> SweepResult:
    return SweepResult(recovered_count=0)


def _fake_run_daily_timeline(*args, **kwargs) -> TimelineRunSummary:
    return TimelineRunSummary()


@pytest.fixture(autouse=True)
def _mock_common(monkeypatch, database):
    """Plumbing every test in this file needs: a real in-memory SQLite
    `Database` (the shared `database` fixture, see conftest.py) instead of
    an attempted Postgres connection, a fake job-queue backend, an
    `AzureOpenAIChatClient` that main() can construct but which
    `process_jobs` (mocked per-test) never actually calls, and no-op
    defaults for the enqueue/sweep phases that individual tests override
    where the test cares about them."""
    monkeypatch.setattr(batch_script, "Database", lambda *a, **k: database)
    monkeypatch.setattr(
        batch_script, "build_job_queue", lambda settings, db: (_FakeDispatcher(), _FakeSource())
    )
    monkeypatch.setattr(batch_script, "AzureOpenAIChatClient", lambda *a, **k: object())
    monkeypatch.setattr(batch_script, "sweep_stranded_pending_projection_summaries", _fake_sweep)
    monkeypatch.setattr(batch_script, "sweep_stranded_pending_mitigation_summaries", _fake_sweep)
    monkeypatch.setattr(
        batch_script, "sweep_expired_po_delivery_change_requests", lambda *a, **k: _fake_sweep()
    )
    monkeypatch.setattr(batch_script, "enqueue_daily_run", _fake_enqueue_daily_run)
    monkeypatch.setattr(batch_script, "run_daily_timeline", _fake_run_daily_timeline)
    monkeypatch.setattr(batch_script, "process_jobs", lambda *a, **k: WorkerLoopSummary())
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py"])


def test_some_dead_items_exits_zero_by_default(monkeypatch, capsys):
    summary = WorkerLoopSummary(succeeded=2, dead_lettered=1)
    monkeypatch.setattr(batch_script, "process_jobs", lambda *a, **k: summary)

    assert _invoke_main() == 0

    out = capsys.readouterr().out
    assert "dead_total=1" in out
    assert "1 item(s) ended DEAD" in out


def test_fail_on_dead_flag_makes_dead_items_exit_nonzero(monkeypatch):
    summary = WorkerLoopSummary(succeeded=2, dead_lettered=1)
    monkeypatch.setattr(batch_script, "process_jobs", lambda *a, **k: summary)
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--fail-on-dead"])

    assert _invoke_main() == 1


def test_fail_on_dead_flag_is_a_noop_with_no_dead_items(monkeypatch):
    summary = WorkerLoopSummary(succeeded=3)
    monkeypatch.setattr(batch_script, "process_jobs", lambda *a, **k: summary)
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--fail-on-dead"])

    assert _invoke_main() == 0


def test_dead_via_exhaustion_also_counts_toward_dead_total(monkeypatch):
    """dead_total covers both an explicit kill and a nack that exhausted
    max_attempts (see WorkerLoopSummary.dead_total's own docstring)."""
    summary = WorkerLoopSummary(succeeded=1, nacked=1, dead_via_exhaustion=1)
    monkeypatch.setattr(batch_script, "process_jobs", lambda *a, **k: summary)
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--fail-on-dead"])

    assert _invoke_main() == 1


def test_lock_already_held_exits_zero_without_enqueueing_or_draining(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(JobQueueRepository, "try_advisory_lock", lambda self, key: False)
    monkeypatch.setattr(
        batch_script,
        "enqueue_daily_run",
        lambda *a, **k: calls.append("enqueue") or _fake_enqueue_daily_run(),
    )
    monkeypatch.setattr(
        batch_script, "process_jobs", lambda *a, **k: calls.append("drain") or WorkerLoopSummary()
    )
    monkeypatch.setattr(
        batch_script,
        "sweep_stranded_pending_projection_summaries",
        lambda *a, **k: calls.append("sweep") or _fake_sweep(),
    )

    assert _invoke_main() == 0
    assert calls == []


def test_db_connection_failure_exits_nonzero(monkeypatch):
    def _boom(self, key):
        raise OperationalError("could not connect to server", None, BaseException("connection refused"))

    monkeypatch.setattr(JobQueueRepository, "try_advisory_lock", _boom)

    assert _invoke_main() != 0


def test_dry_run_exits_zero_and_never_takes_the_lock(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        JobQueueRepository, "try_advisory_lock", lambda self, key: calls.append("lock") or True
    )
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--dry-run"])

    assert _invoke_main() == 0
    assert calls == []


def test_sweep_runs_before_enqueue(monkeypatch):
    order: list[str] = []

    def _sweep(*args, **kwargs) -> SweepResult:
        order.append("sweep")
        return SweepResult(recovered_count=0)

    def _enqueue(*args, **kwargs) -> EnqueueResult:
        order.append("enqueue")
        return _fake_enqueue_daily_run()

    monkeypatch.setattr(batch_script, "sweep_stranded_pending_projection_summaries", _sweep)
    monkeypatch.setattr(batch_script, "enqueue_daily_run", _enqueue)
    monkeypatch.setattr(batch_script, "process_jobs", lambda *a, **k: WorkerLoopSummary())

    assert _invoke_main() == 0
    assert order == ["sweep", "enqueue"]


def test_timeline_step_runs_after_drain_by_default(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        batch_script, "process_jobs", lambda *a, **k: calls.append("drain") or WorkerLoopSummary()
    )
    monkeypatch.setattr(
        batch_script,
        "run_daily_timeline",
        lambda *a, **k: calls.append("timeline") or _fake_run_daily_timeline(),
    )
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--date", "2026-08-13"])

    assert _invoke_main() == 0
    assert calls == ["drain", "timeline"]


def test_timeline_step_uses_the_same_resolved_date_as_the_legacy_enqueue(monkeypatch):
    seen: dict[str, date] = {}

    def _spy(db, projection_date):
        seen["date"] = projection_date
        return _fake_run_daily_timeline()

    monkeypatch.setattr(batch_script, "run_daily_timeline", _spy)
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--date", "2026-08-13"])

    assert _invoke_main() == 0
    assert seen["date"] == date(2026, 8, 13)


def test_both_engines_receive_the_identical_resolved_date_when_no_date_flag_is_given(monkeypatch):
    """Neither engine resolves its own "today" -- `main()` resolves it once via
    `business_today()` and passes that same value to both `enqueue_daily_run` and
    `run_daily_timeline`."""
    seen: dict[str, date] = {}

    def _enqueue_spy(job_dispatcher, database, settings, *, projection_date, stacking_mode_override):
        seen["enqueue"] = projection_date
        return _fake_enqueue_daily_run()

    def _timeline_spy(db, projection_date):
        seen["timeline"] = projection_date
        return _fake_run_daily_timeline()

    monkeypatch.setattr(batch_script, "enqueue_daily_run", _enqueue_spy)
    monkeypatch.setattr(batch_script, "run_daily_timeline", _timeline_spy)
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py"])

    assert _invoke_main() == 0

    assert seen["enqueue"] is not None
    assert seen["enqueue"] == seen["timeline"]


def test_timeline_step_is_skipped_when_enqueue_only(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        batch_script,
        "run_daily_timeline",
        lambda *a, **k: calls.append("timeline") or _fake_run_daily_timeline(),
    )
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--enqueue-only"])

    assert _invoke_main() == 0
    assert calls == []


def test_timeline_step_is_skipped_when_drain_only(monkeypatch):
    """`--drain-only` is a partial legacy operation (controller ruling) -- it must not
    trigger the timeline step either, same as `--enqueue-only`."""
    calls: list[str] = []
    monkeypatch.setattr(
        batch_script, "process_jobs", lambda *a, **k: calls.append("drain") or WorkerLoopSummary()
    )
    monkeypatch.setattr(
        batch_script,
        "run_daily_timeline",
        lambda *a, **k: calls.append("timeline") or _fake_run_daily_timeline(),
    )
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--drain-only"])

    assert _invoke_main() == 0
    assert calls == ["drain"]


def test_skip_timeline_flag_skips_the_timeline_step(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        batch_script,
        "run_daily_timeline",
        lambda *a, **k: calls.append("timeline") or _fake_run_daily_timeline(),
    )
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--skip-timeline"])

    assert _invoke_main() == 0
    assert calls == []


def test_timeline_only_flag_skips_every_legacy_step_but_keeps_the_lock(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        JobQueueRepository, "try_advisory_lock", lambda self, key: calls.append("lock") or True
    )
    monkeypatch.setattr(
        batch_script,
        "sweep_stranded_pending_projection_summaries",
        lambda *a, **k: calls.append("sweep") or _fake_sweep(),
    )
    monkeypatch.setattr(
        batch_script,
        "enqueue_daily_run",
        lambda *a, **k: calls.append("enqueue") or _fake_enqueue_daily_run(),
    )
    monkeypatch.setattr(
        batch_script, "process_jobs", lambda *a, **k: calls.append("drain") or WorkerLoopSummary()
    )
    monkeypatch.setattr(
        batch_script,
        "run_daily_timeline",
        lambda *a, **k: calls.append("timeline") or _fake_run_daily_timeline(),
    )
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--timeline-only"])

    assert _invoke_main() == 0
    assert calls == ["lock", "timeline"]


def test_timeline_only_and_skip_timeline_together_is_a_usage_error(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--timeline-only", "--skip-timeline"])

    assert _invoke_main() == 2


def test_dry_run_mentions_the_timeline_step_and_does_not_run_it(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(
        batch_script,
        "run_daily_timeline",
        lambda *a, **k: calls.append("timeline") or _fake_run_daily_timeline(),
    )
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--dry-run"])

    assert _invoke_main() == 0
    assert calls == []
    assert "timeline" in capsys.readouterr().out.lower()


def test_timeline_failure_is_logged_and_exits_zero_by_default(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(batch_script, "run_daily_timeline", _boom)

    assert _invoke_main() == 0
    assert "timeline" in capsys.readouterr().out.lower()


def test_fail_on_dead_flag_makes_a_timeline_failure_exit_nonzero(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(batch_script, "run_daily_timeline", _boom)
    monkeypatch.setattr("sys.argv", ["run_daily_batch.py", "--fail-on-dead"])

    assert _invoke_main() == 1


def test_timeline_summary_is_included_in_the_printed_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        batch_script,
        "run_daily_timeline",
        lambda *a, **k: TimelineRunSummary(
            plans_evaluated=3,
            plans_skipped=1,
            status_counts={"ON_TRACK": 2, "SLIPPING": 1},
            total_projected_penalty=125.5,
        ),
    )

    assert _invoke_main() == 0

    out = capsys.readouterr().out
    assert "plans_evaluated=3" in out
    assert "plans_skipped=1" in out
    assert "125.5" in out
