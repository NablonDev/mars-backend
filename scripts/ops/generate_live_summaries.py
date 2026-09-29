"""Generate live LLM summaries across all showcase purchase orders via the real service & worker pipeline."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import LLMConfig, get_settings
from app.core.exceptions import BusinessRuleError, ValidationError
from app.db.session import Database
from app.models.common import PurchaseOrder
from app.models.penalties import PenaltyDispute
from app.queue.factory import build_job_queue
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.mitigation import MitigationOptionRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.dispute.summary_service import DisputeSummaryService
from app.services.penalties.mitigation.summary_service import MitigationSummaryService
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService
from app.workers.loop import process_jobs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("generate_live_summaries")


def main() -> None:
    settings = get_settings()
    db = Database(settings.database.url)
    llm_config = LLMConfig.from_settings(settings)
    llm = AzureOpenAIChatClient(llm_config)

    target_po_numbers = ["WMT-100234", "AMZ-778501", "AMZ-780112", "WMT-100511", "CST-990142"]

    with db.session() as session:
        po_repo = PurchaseOrderRepository(session)
        sum_repo = PenaltySummaryRepository(session)
        agent_repo = AgentRegistryRepository(session)
        jq_repo = JobQueueRepository(session)
        ctx_repo = PenaltyJobItemContextRepository(session)
        proj_repo = PenaltyProjectionRepository(session)
        act_repo = ActualPenaltyRepository(session)
        rule_repo = PenaltyRuleRepository(session)
        master_repo = MasterDataRepository(session)
        ful_repo = FulfillmentRepository(session)
        mit_repo = MitigationOptionRepository(session)
        disp_repo = PenaltyDisputeRepository(session)

        proj_service = ProjectionService(
            purchase_orders=po_repo,
            fulfillment=ful_repo,
            rules=rule_repo,
            master_data=master_repo,
            projections=proj_repo,
        )
        proj_summary_service = ProjectionSummaryService(
            purchase_orders=po_repo,
            summaries=sum_repo,
            agent_registry=agent_repo,
            job_queue=jq_repo,
            job_context=ctx_repo,
            llm=llm,
            rules=rule_repo,
            master_data=master_repo,
            projections=proj_repo,
            actual_penalties=act_repo,
            projection_service=proj_service,
        )
        mit_summary_service = MitigationSummaryService(
            purchase_orders=po_repo,
            summaries=sum_repo,
            agent_registry=agent_repo,
            job_queue=jq_repo,
            job_context=ctx_repo,
            llm=llm,
            master_data=master_repo,
            mitigation_options=mit_repo,
            actual_penalties=act_repo,
            projection_service=proj_service,
        )
        disp_summary_service = DisputeSummaryService(
            purchase_orders=po_repo,
            summaries=sum_repo,
            agent_registry=agent_repo,
            job_queue=jq_repo,
            job_context=ctx_repo,
            llm=llm,
            disputes=disp_repo,
            rules=rule_repo,
            master_data=master_repo,
        )

        for po_num in target_po_numbers:
            po = session.scalars(
                select(PurchaseOrder).where(PurchaseOrder.purchase_order_number == po_num)
            ).first()
            if not po:
                logger.warning(f"PO {po_num} not found")
                continue

            po_projs = proj_repo.list_projections(purchase_order_id=po.id)
            if not po_projs:
                logger.warning(f"PO {po_num} has no projections on record, skipping summary")
                continue

            valid_dates = sorted({p["projection_date"] for p in po_projs})
            if not valid_dates:
                logger.warning(f"PO {po_num} has no projections on record, skipping")
                continue

            for target_date in valid_dates:
                existing_proj = sum_repo.get_by_key(po.id, "PROJECTION", target_date)
                if not existing_proj or existing_proj.get("status") != "READY":
                    logger.info(f"Scheduling PROJECTION summary for {po_num} as of {target_date}...")
                    proj_summary_service.get_or_schedule(po.id, as_of_date=target_date, force_regenerate=True)

                existing_mit = sum_repo.get_by_key(po.id, "MITIGATION", target_date)
                if not existing_mit or existing_mit.get("status") != "READY":
                    logger.info(f"Scheduling MITIGATION summary for {po_num} as of {target_date}...")
                    try:
                        mit_summary_service.get_or_schedule(
                            po.id, as_of_date=target_date, force_regenerate=True
                        )
                    except (ValidationError, BusinessRuleError) as e:
                        logger.warning(
                            f"Could not schedule mitigation summary for {po_num} as of {target_date}: {e}"
                        )

        # Schedule dispute summaries for analyzed/resolved disputes
        disputes = session.scalars(
            select(PenaltyDispute).where(PenaltyDispute.dispute_status.in_(["ANALYZED", "RESOLVED"]))
        ).all()
        for d in disputes:
            logger.info(f"Scheduling DISPUTE summary for dispute {d.id}...")
            try:
                disp_summary_service.get_or_schedule_for_dispute(d.id, force_regenerate=True)
            except (ValidationError, BusinessRuleError) as e:
                logger.warning(f"Could not schedule dispute summary for {d.id}: {e}")

        session.commit()

    logger.info("Starting background worker drain...")
    _, job_source = build_job_queue(settings, database=db)
    summary = process_jobs(job_source, db, settings, llm, mode="drain", reclaim_stale_first=True)
    logger.info(f"Drain completed: succeeded={summary.succeeded} dead_total={summary.dead_total}")


if __name__ == "__main__":
    main()
