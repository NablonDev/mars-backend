"""
No-server CLI: runs the penalty-projection service directly against the
configured database (DATABASE_URL / .env), without needing `uvicorn`
running. Kept for ad-hoc/local use and cron/Airflow-style scheduling
where standing up an HTTP server just to run a batch job is unnecessary
-- the API (`POST /api/v1/penalties/projections`,
or `POST /api/v1/job-runs` for the full nightly batch) is the equivalent
for anything that should go through HTTP.

Was written against the pre-restructure `app.repositories.order`/
`app.services.fine_projection.*` -- rewritten against the Phase 2/3
`common`/`penalties` repositories and services (the `fine`/`fines` ->
`penalty`/`penalties` rename).

Examples:
    python scripts/ops/run_projection_cli.py --purchase-order-id 6f7a2c9e-...
    python scripts/ops/run_projection_cli.py --purchase-order-id 6f7a2c9e-... --date 2026-08-05
    python scripts/ops/run_projection_cli.py --all-open
    python scripts/ops/run_projection_cli.py --all-open --date 2026-08-05 --stacking-mode MAX
    python scripts/ops/run_projection_cli.py --all-open --with-summary

--with-summary additionally runs the penalty-projection-summary generation
for each purchase order right after its projection succeeds -- the same
sequential guarantee as
`POST /penalties/projections/summary`,
for this no-HTTP-server path. Runs inline (no BackgroundTasks needed in a
one-shot CLI process) and needs Azure OpenAI configured (AZURE_OPENAI_API_KEY/
AZURE_OPENAI_ENDPOINT/AZURE_OPENAI_DEPLOYMENT_NAME).
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import LLMConfig, get_settings
from app.core.exceptions import AppError
from app.db.session import Database
from app.models.enums import SummaryStatus
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purchase-order-id", help="Run a single purchase order by id (UUID)")
    parser.add_argument(
        "--all-open", action="store_true", help="Run every purchase order with order_status = OPEN"
    )
    parser.add_argument("--date", help="Projection date, YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--stacking-mode",
        choices=["SUM", "MAX"],
        help="Override the retailer's configured stacking policy for this run only",
    )
    parser.add_argument(
        "--with-summary",
        action="store_true",
        help=(
            "Also generate the penalty projection summary for each purchase order after a "
            "successful projection"
        ),
    )
    args = parser.parse_args()

    if not args.purchase_order_id and not args.all_open:
        parser.error("Pass either --purchase-order-id <uuid> or --all-open")

    projection_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None  # noqa: DTZ007

    settings = get_settings()
    database = Database(settings.database.url)
    with database.session() as session:
        purchase_orders = PurchaseOrderRepository(session)
        fulfillment = FulfillmentRepository(session)
        rules = PenaltyRuleRepository(session)
        master_data = MasterDataRepository(session)
        projections = PenaltyProjectionRepository(session)
        projection_service = ProjectionService(
            purchase_orders=purchase_orders,
            fulfillment=fulfillment,
            rules=rules,
            master_data=master_data,
            projections=projections,
        )
        projection_summary_service = None
        if args.with_summary:
            projection_summary_service = ProjectionSummaryService(
                purchase_orders=purchase_orders,
                summaries=PenaltySummaryRepository(session),
                agent_registry=AgentRegistryRepository(session),
                job_queue=JobQueueRepository(session),
                job_context=PenaltyJobItemContextRepository(session),
                llm=AzureOpenAIChatClient(
                    LLMConfig.from_settings(settings),
                    max_retries=settings.llm.max_attempts,
                    timeout_seconds=settings.llm.timeout_seconds,
                ),
                rules=rules,
                master_data=master_data,
                projections=projections,
                actual_penalties=ActualPenaltyRepository(session),
                projection_service=projection_service,
            )

        if args.purchase_order_id:
            purchase_order_ids = [UUID(args.purchase_order_id)]
        else:
            purchase_order_ids = [
                po["id"] for po in purchase_orders.list_purchase_orders(order_status="OPEN")
            ]
            print(f"Running {len(purchase_order_ids)} open purchase order(s):")

        for purchase_order_id in purchase_order_ids:
            try:
                result = projection_service.run_for_purchase_order(
                    purchase_order_id, projection_date, args.stacking_mode
                )
            # `AppError` covers NotFoundError(code="PO_NOT_FOUND")/
            # BusinessRuleError(code="NO_ACTIVE_RULES"); `ValueError` still
            # covers the engine's own rule-data validation, so one bad rule
            # skips one purchase order instead of aborting a whole
            # --all-open batch.
            except (AppError, ValueError) as exc:
                print(f"  [skip] {purchase_order_id}: {exc}")
                continue

            # `result.violations` are the pure-engine `ViolationProjection`
            # dataclasses (app/services/penalties/projection/types.py), which
            # already carry `penalty_amount` alongside `expected_penalty_amount`
            # -- state probability against the raw amount as the primary
            # framing, with the blended figure kept as a secondary,
            # clearly-labeled risk-adjusted estimate (see docs/API.md's
            # response-shape note; never print a bare blended $ as fact).
            parts = ", ".join(
                f"{v.violation_type} {v.probability * 100:.0f}% probability of a "
                f"${v.penalty_amount:,.2f} penalty (risk-adjusted estimate: ${v.expected_penalty_amount:,.2f})"
                for v in result.violations
            )
            print(
                f"  {purchase_order_id} [{result.projection_date}]  "
                f"days_to_delivery={result.days_to_delivery:<3} "
                f"stacking={result.stacking_mode:<3} "
                f"total (risk-adjusted estimate)=${result.total_expected_penalty_amount:,.2f}   {parts}"
            )

            if projection_summary_service is None:
                continue

            try:
                job = projection_summary_service.get_or_schedule(
                    purchase_order_id, as_of_date=result.projection_date
                )
                if job.status == SummaryStatus.PENDING:
                    try:
                        projection_summary_service.run_generation(job.purchase_order_id, job.as_of_date)
                    except Exception:
                        # run_generation re-raises after persisting a
                        # FAILED ledger row (see SummaryServiceBase.
                        # run_generation) so a queue worker can classify
                        # retry-vs-dead. This one-shot CLI has always
                        # printed the resulting status line below
                        # regardless of success or failure -- get_status
                        # reads that same ledger row -- so swallow here
                        # rather than falling into the `except AppError`
                        # below, which prints a different "[summary
                        # skipped]" message reserved for get_or_schedule
                        # failing outright.
                        logger.exception(
                            "Penalty projection summary generation failed: "
                            "purchase_order_id=%s as_of_date=%s",
                            job.purchase_order_id,
                            job.as_of_date,
                        )
                    job = projection_summary_service.get_status(
                        purchase_order_id, as_of_date=result.projection_date
                    )
            except AppError as exc:
                print(f"    [summary skipped] {purchase_order_id}: {exc}")
                continue

            print(f"    summary [{job.status}]")


if __name__ == "__main__":
    main()
