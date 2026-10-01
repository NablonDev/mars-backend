"""Seed cleanup of the dispute repository must not touch the stored projection narratives."""

from datetime import date
from uuid import uuid4

from app.models.enums import SummaryType
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.summary import PenaltySummaryRepository

_AGENT_ID = uuid4()


def _seed_po_with_summaries(repos, db_session, number: str):
    retailer = repos.master_data.add_retailer(f"RET-{number}", "Cleanup Retailer", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=number,
        retailer_id=retailer["id"],
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        order_status="OPEN",
    )
    summaries = PenaltySummaryRepository(db_session)
    for summary_type in (SummaryType.PROJECTION, SummaryType.DISPUTE):
        summaries.create_pending(
            purchase_order_id=purchase_order["id"],
            summary_type=summary_type,
            as_of_date=date(2026, 6, 1),
            agent_id=_AGENT_ID,
            context_hash="hash",
        )
    db_session.commit()
    return purchase_order["id"]


def _summary_types(db_session, po_id) -> set[str]:
    repo = PenaltySummaryRepository(db_session)
    return {
        summary_type
        for summary_type in (SummaryType.PROJECTION, SummaryType.DISPUTE)
        if repo.get_by_key(po_id, summary_type, date(2026, 6, 1)) is not None
    }


def test_delete_seed_disputes_keeps_the_stored_projection_narrative(repos, db_session):
    po_id = _seed_po_with_summaries(repos, db_session, "TL-CLEAN-1")

    PenaltyDisputeRepository(db_session).delete_seed_disputes("TL-")

    assert _summary_types(db_session, po_id) == {SummaryType.PROJECTION}


def test_delete_seed_data_clears_every_summary_before_the_po_delete(repos, db_session):
    po_id = _seed_po_with_summaries(repos, db_session, "TL-CLEAN-2")

    PenaltyDisputeRepository(db_session).delete_seed_data("TL-")

    assert _summary_types(db_session, po_id) == set()


def test_delete_seed_disputes_leaves_pos_outside_the_prefix_alone(repos, db_session):
    po_id = _seed_po_with_summaries(repos, db_session, "REAL-CLEAN-1")

    PenaltyDisputeRepository(db_session).delete_seed_disputes("TL-")

    assert _summary_types(db_session, po_id) == {SummaryType.PROJECTION, SummaryType.DISPUTE}
