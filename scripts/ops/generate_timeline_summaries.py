"""Operational script to batch-generate and persist 4-step LLM timeline reasoning for all fulfillment plans."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import select

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import LLMConfig, get_settings
from app.db.session import Database
from app.models.common import FulfillmentPlan
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.services.penalties.timeline.summary_service import TimelineSummaryService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("generate_timeline_summaries")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate and cache 4-step LLM reasoning for timeline fulfillment plans."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force regeneration even if a cached ready summary exists.",
    )
    parser.add_argument(
        "--plan-id",
        type=str,
        default=None,
        help="Generate for a specific fulfillment plan ID only.",
    )
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.database.url)
    llm_config = LLMConfig.from_settings(settings)
    llm = AzureOpenAIChatClient(llm_config)

    with db.session() as session:
        timeline_repo = FulfillmentTimelineRepository(session)
        risk_repo = FulfillmentRiskRepository(session)
        alert_repo = TimelineAlertRepository(session)
        po_repo = PurchaseOrderRepository(session)
        master_repo = MasterDataRepository(session)
        summary_repo = PenaltySummaryRepository(session)
        agent_repo = AgentRegistryRepository(session)

        service = TimelineSummaryService(
            session=session,
            timeline=timeline_repo,
            risks=risk_repo,
            alerts=alert_repo,
            purchase_orders=po_repo,
            master_data=master_repo,
            summaries=summary_repo,
            agent_registry=agent_repo,
            llm=llm,
        )

        if args.plan_id:
            plans = [timeline_repo.get_plan(UUID(args.plan_id))]
            if not plans[0]:
                logger.error("Plan %s not found", args.plan_id)
                sys.exit(1)
            plan_ids = [(UUID(args.plan_id), plans[0]["plan_number"])]
        else:
            rows = session.scalars(select(FulfillmentPlan).order_by(FulfillmentPlan.plan_number)).all()
            plan_ids = [(r.id, r.plan_number) for r in rows]

        logger.info("Found %d fulfillment plan(s) to process", len(plan_ids))

        success_count = 0
        error_count = 0

        for idx, (p_id, p_num) in enumerate(plan_ids, 1):
            logger.info("[%d/%d] Processing plan %s (%s)...", idx, len(plan_ids), p_num, p_id)
            try:
                result = service.generate_and_persist(p_id, force_regenerate=args.force)
                logger.info(
                    "✓ Plan %s succeeded (%d reasoning steps, model=%s)",
                    p_num,
                    len(result.reasoning_steps),
                    result.model_name,
                )
                success_count += 1
            except Exception:
                logger.exception("✗ Failed generating reasoning for plan %s", p_num)
                error_count += 1

        logger.info(
            "Batch summary generation completed: %d succeeded, %d failed out of %d total.",
            success_count,
            error_count,
            len(plan_ids),
        )


if __name__ == "__main__":
    main()
