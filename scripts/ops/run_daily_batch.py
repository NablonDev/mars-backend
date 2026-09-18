"""
Nightly batch runner: the concurrent, queue-backed replacement for
`scripts/ops/run_projection_cli.py --all-open --with-summary` on the
Azure Container Apps Job schedule. Supersedes that script's `--all-open`
batch path without modifying or removing it -- `run_projection_cli.py
--purchase-order-id ...` remains the ad-hoc/single-PO tool.

Was written against the pre-restructure `app.repositories.job_queue`/
`app.repositories.order` -- rewritten against the Phase 2/3
`common`/`process`/`penalties` repositories and
`app.workers.penalty_projection`/`penalty_mitigation` (the `fine_projection`/
`fine_mitigation` rename).

Sequence: acquire a Postgres advisory lock -> reclaim stale job_item rows
-> sweep and recover any stranded PENDING penalty-projection-summary ledger
rows (see app.workers.penalty_projection) -> sweep the mitigation-summary
side (see app.workers.penalty_mitigation) -> expire stale PO
delivery-change requests -> enqueue today's ORDER_RUN items -> drain the
queue -> release the lock -> print a summary.

Exit-code semantics: see docs/JOB-QUEUE-WALKTHROUGH.md §4.1 -- in short,
exit 0 whenever the run completed (even with DEAD items, or because a
previous run's lock was still held), non-zero only on genuine
infrastructure failure.
`--fail-on-dead` restores the old "exit non-zero if anything died"
behavior for CI/ad-hoc use.

Examples:
    python scripts/ops/run_daily_batch.py
    python scripts/ops/run_daily_batch.py --date 2026-08-13 --stacking-mode MAX
    python scripts/ops/run_daily_batch.py --concurrency 8
    python scripts/ops/run_daily_batch.py --enqueue-only
    python scripts/ops/run_daily_batch.py --drain-only
    python scripts/ops/run_daily_batch.py --dry-run
    python scripts/ops/run_daily_batch.py --fail-on-dead
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import LLMConfig, get_settings
from app.core.logging import configure_logging
from app.db.session import Database
from app.queue.factory import build_job_queue
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.workers.loop import process_jobs
from app.workers.penalty_mitigation import sweep_stranded_pending_mitigation_summaries
from app.workers.penalty_projection import (
    enqueue_daily_run,
    sweep_expired_po_delivery_change_requests,
    sweep_stranded_pending_projection_summaries,
)

logger = logging.getLogger(__name__)

# Arbitrary but stable key in pg_try_advisory_lock's global (per-database)
# integer namespace. Nothing else in this codebase takes an advisory lock
# as of writing (grepped) -- if that ever changes, every caller must agree
# on distinct keys, since the namespace has no further scoping.
_NIGHTLY_BATCH_LOCK_KEY = 837_401_559


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date",
        help="Projection date, YYYY-MM-DD (default: today in settings.summary.business_timezone)",
    )
    parser.add_argument(
        "--stacking-mode",
        choices=["SUM", "MAX"],
        help="Override the retailer's configured stacking policy for this run only",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help="Override job_queue.worker_concurrency for this run only",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--enqueue-only",
        action="store_true",
        help="Enqueue today's ORDER_RUN items and exit without draining the queue",
    )
    mode_group.add_argument(
        "--drain-only",
        action="store_true",
        help="Skip enqueueing; just drain whatever is already PENDING",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many OPEN purchase orders would be enqueued; take no lock, write nothing",
    )
    parser.add_argument(
        "--fail-on-dead",
        action="store_true",
        help=(
            "Exit non-zero if any item ended DEAD this run (old default behavior). "
            "Off by default -- a DEAD item should not fail/retry the whole "
            "Container Apps Job."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    projection_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None  # noqa: DTZ007

    settings = get_settings()
    if args.concurrency is not None:
        settings.job_queue.worker_concurrency = args.concurrency

    configure_logging(settings.app.log_level, settings.app.log_format)

    database = Database(
        settings.database.url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
    )

    if args.dry_run:
        with database.session() as session:
            purchase_order_count = len(
                PurchaseOrderRepository(session).list_purchase_orders(order_status="OPEN")
            )
        print(
            f"[dry-run] would enqueue {purchase_order_count} ORDER_RUN item(s); "
            "no lock taken, nothing written."
        )
        return 0

    job_dispatcher, job_source = build_job_queue(settings, database)

    # Dedicated session held open for the process's whole lifetime:
    # pg_advisory_lock/unlock are tied to the specific backend connection
    # that acquired the lock, not a logical "session" -- the normal
    # open-close-per-call idiom would return the connection to the pool
    # between acquire and release, letting a *different* connection get
    # handed out for the unlock (a silent no-op) or a concurrent lock
    # attempt (a false "not held").
    lock_session = database.new_session()
    lock_repo = JobQueueRepository(lock_session)
    try:
        acquired = lock_repo.try_advisory_lock(_NIGHTLY_BATCH_LOCK_KEY)
        lock_session.commit()

        if not acquired:
            logger.info(
                "Nightly batch lock (key=%s) already held -- a previous run is still in "
                "progress. Expected, not an error (Container Apps Jobs has no "
                "concurrencyPolicy: Forbid equivalent) -- exiting 0.",
                _NIGHTLY_BATCH_LOCK_KEY,
            )
            return 0

        reclaimed = job_source.reclaim_stale(settings.job_queue.visibility_timeout_seconds)
        if reclaimed:
            logger.info("Reclaimed %s stale job_item(s).", reclaimed)

        # Runs regardless of --enqueue-only/--drain-only: a recovered row
        # becomes an ordinary PENDING job_item, picked up by whichever
        # phase actually drains (see app.workers.penalty_projection).
        sweep_result = sweep_stranded_pending_projection_summaries(job_dispatcher, database, settings)
        if sweep_result.recovered_count:
            logger.info(
                "Recovered %s stranded PENDING penalty-projection-summary ledger row(s) (job_run_id=%s).",
                sweep_result.recovered_count,
                sweep_result.job_run_id,
            )
        print(f"Recovery sweep: recovered {sweep_result.recovered_count} stranded PENDING summary row(s).")

        # Same durability gap, mitigation-summary side (see
        # app.workers.penalty_mitigation) -- mitigation itself is on-demand only (no
        # nightly ORDER_RUN-equivalent for it), so this only ever finds
        # something if a mitigation-summary request crashed between its
        # ledger write and its job_item write.
        mitigation_sweep_result = sweep_stranded_pending_mitigation_summaries(
            job_dispatcher, database, settings
        )
        if mitigation_sweep_result.recovered_count:
            logger.info(
                "Recovered %s stranded PENDING penalty-mitigation-summary ledger row(s) (job_run_id=%s).",
                mitigation_sweep_result.recovered_count,
                mitigation_sweep_result.job_run_id,
            )
        print(
            f"Recovery sweep: recovered {mitigation_sweep_result.recovered_count} stranded "
            "PENDING mitigation-summary row(s)."
        )

        # Expire PENDING PO delivery-change requests past their SLA and
        # re-trigger projection for each affected purchase order (see
        # app.workers.penalty_projection.sweep_expired_po_delivery_change_requests).
        # No job_dispatcher involved -- the status flip and re-trigger happen
        # inline, no LLM call needed.
        po_delivery_change_sweep_result = sweep_expired_po_delivery_change_requests(database)
        if po_delivery_change_sweep_result.recovered_count:
            logger.info(
                "Expired %s stale PO delivery-change request(s).",
                po_delivery_change_sweep_result.recovered_count,
            )
        print(
            f"Recovery sweep: expired {po_delivery_change_sweep_result.recovered_count} stale "
            "PO delivery-change request(s)."
        )

        if not args.drain_only:
            result = enqueue_daily_run(
                job_dispatcher,
                database,
                settings,
                projection_date=projection_date,
                stacking_mode_override=args.stacking_mode,
            )
            print(
                f"Enqueued job_run_id={result.job_run_id}: {result.enqueued_count} new "
                f"ORDER_RUN item(s) of {result.purchase_order_count} OPEN purchase order(s)."
            )
            if result.no_open_orders_note:
                print(f"WARNING: {result.no_open_orders_note}")

        if args.enqueue_only:
            return 0

        llm_config = LLMConfig.from_settings(settings)
        llm = AzureOpenAIChatClient(
            llm_config,
            max_retries=llm_config.max_retries,
            timeout_seconds=llm_config.timeout_seconds,
        )
        summary = process_jobs(
            job_source,
            database,
            settings,
            llm,
            mode="drain",
            # Already reclaimed above, right after taking the lock.
            reclaim_stale_first=False,
        )

        print(
            f"Drain complete: succeeded={summary.succeeded} nacked={summary.nacked} "
            f"dead_lettered={summary.dead_lettered} dead_via_exhaustion={summary.dead_via_exhaustion} "
            f"dead_total={summary.dead_total} abandoned={summary.abandoned} "
            f"released={summary.released} rate_limit_hits={summary.rate_limit_hits}"
        )

        if summary.dead_total > 0:
            # DEAD is terminal -- a
            # successful run with failures, not a failed run: exit 0
            # unless the caller opted into --fail-on-dead.
            print(
                f"{summary.dead_total} item(s) ended DEAD this run (terminal -- will not be "
                f"retried by re-running the job). See job_item.last_error_code via "
                f"GET /job-runs/{{job_run_id}}/items?status=DEAD for detail."
            )
            if args.fail_on_dead:
                return 1

        return 0
    finally:
        lock_repo.release_advisory_lock(_NIGHTLY_BATCH_LOCK_KEY)
        lock_session.commit()
        lock_session.close()
        database.dispose()


if __name__ == "__main__":
    sys.exit(main())
