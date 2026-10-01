"""Seed engine-true disputes against the timeline scenarios.

Every verdict here is produced by the real `DisputeResolutionService`: the actual penalty is
recorded, the dispute is opened, and `analyze()` recomputes the charge from the PO's own rule and
the fulfillment-timeline facts (milestone actuals, shipped quantity). Nothing is hand-written, so
the numbers a demo shows are the numbers the engine would produce on live data.

Each scenario needs delivery facts the replay simulator already created, so run
`python -m scripts.seed.seed_timeline_scenarios` first. Re-running is safe: it first clears the
disputes, dispute summaries and actual penalties it owns on the `TL-` purchase orders, and
nothing else (the stored projection and mitigation narratives stay).

Scenarios (retailer's claim versus what the rule recomputes):
- TL-S01-PO  Amazon, delivered inside the window, charged the flat $500 OTIF fee  -> NO_PAY, RESOLVED
- TL-S08-PO  Costco, 2 days late (1 day grace), charged twice the contract amount  -> PAY_PARTIAL, ANALYZED
- TL-S10A-PO Target, delivered inside the window, charged a 3% OTIF fee            -> left OPEN (analyze live)
- TL-S12-PO  Walmart, delivered inside the window, charged a 3% OTIF fee ($165)      -> NO_PAY, ANALYZED

The dispute narrative is a deterministic template filled from the engine's own analysis, not an
LLM output, so it never states a number or a document the database does not hold.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import Database
from app.models import Agent, PenaltySummary, Retailer
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.services.penalties.dispute.service import DisputeResolutionService
from app.services.penalties.projection.service import ProjectionService
from app.utils.clock import business_today

_SEED_PREFIX = "TL-"
_RESOLVED_BY = "ops.demo"


@dataclass(frozen=True)
class DisputeScenario:
    po_number: str
    penalty_number: str
    violation_type: str
    claimed_amount: float
    reason_code: str
    finish: str  # "OPEN", "ANALYZED" or "RESOLVED": how far the seed takes the dispute
    retailer_note: str


SCENARIOS: tuple[DisputeScenario, ...] = (
    DisputeScenario(
        "TL-S01-PO",
        "ACT-TL-S01",
        "OTIF_LATE",
        500.00,
        "NOT_LATE",
        "RESOLVED",
        "Retailer debited the flat OTIF late fee although the shipment arrived inside the window.",
    ),
    DisputeScenario(
        "TL-S08-PO",
        "ACT-TL-S08",
        "OTIF_LATE",
        78.00,
        "AMOUNT_INCORRECT",
        "ANALYZED",
        "Retailer charged the contract amount twice (once per late day); the clause is a single charge.",
    ),
    DisputeScenario(
        "TL-S10A-PO",
        "ACT-TL-S10A",
        "OTIF_LATE",
        50.40,
        "NOT_LATE",
        "OPEN",
        "Retailer assessed an OTIF late fee on a shipment that arrived inside the window.",
    ),
    DisputeScenario(
        "TL-S12-PO",
        "ACT-TL-S12",
        "OTIF_LATE",
        165.00,
        "NOT_LATE",
        "ANALYZED",
        "Retailer assessed an OTIF late fee on a shipment that arrived inside the window.",
    ),
)


def seed_timeline_disputes(as_of: date | None = None) -> None:
    """Replace the seeded timeline disputes with engine-computed ones."""
    invoice_date = (as_of or business_today()) - timedelta(days=2)
    database = Database(get_settings().database.url)
    with database.session() as session:
        disputes = PenaltyDisputeRepository(session)
        disputes.delete_seed_disputes(_SEED_PREFIX)

        purchase_orders = PurchaseOrderRepository(session)
        actual_penalties = ActualPenaltyRepository(session)
        service = _build_service(session, purchase_orders, actual_penalties, disputes)

        for scenario in SCENARIOS:
            purchase_order = purchase_orders.get_by_number(scenario.po_number)
            if purchase_order is None:
                print(f"Skipping {scenario.po_number}: run seed_timeline_scenarios first.")
                continue
            lines = purchase_orders.list_lines(purchase_order["id"])
            actual = actual_penalties.add_actual_penalty(
                actual_penalty_number=scenario.penalty_number,
                purchase_order_id=purchase_order["id"],
                violation_type=scenario.violation_type,
                actual_penalty_amount=scenario.claimed_amount,
                invoice_or_deduction_date=invoice_date,
                purchase_order_line_id=lines[0]["id"] if lines else None,  # the fines list shows this SKU
            )
            dispute = service.open_dispute(
                actual["id"], scenario.reason_code, scenario.claimed_amount, notes=scenario.retailer_note
            )
            print(
                f"{scenario.po_number}: opened {dispute['dispute_number']} (claimed {scenario.claimed_amount:.2f})"
            )
            if scenario.finish == "OPEN":
                continue

            analyzed = service.analyze(dispute["id"])
            print(
                f"  analyzed: computed {analyzed['computed_amount']:.2f}, "
                f"verdict {analyzed['verdict']}, delta {analyzed['delta_amount']:.2f}"
            )
            if scenario.finish == "RESOLVED":
                service.resolve(dispute["id"], resolved_by=_RESOLVED_BY)
                analyzed = service.get(dispute["id"])
            _upsert_summary(session, purchase_order, analyzed, scenario, invoice_date)
        session.commit()


def _build_service(
    session: Session,
    purchase_orders: PurchaseOrderRepository,
    actual_penalties: ActualPenaltyRepository,
    disputes: PenaltyDisputeRepository,
) -> DisputeResolutionService:
    fulfillment = FulfillmentRepository(session)
    rules = PenaltyRuleRepository(session)
    projection_service = ProjectionService(
        purchase_orders=purchase_orders,
        fulfillment=fulfillment,
        rules=rules,
        master_data=MasterDataRepository(session),
        projections=PenaltyProjectionRepository(session),
    )
    return DisputeResolutionService(
        purchase_orders=purchase_orders,
        disputes=disputes,
        actual_penalties=actual_penalties,
        rules=rules,
        projection_service=projection_service,
        fulfillment=fulfillment,
        fulfillment_timeline=FulfillmentTimelineRepository(session),
        retailer_agreements=RetailerAgreementRepository(session),
    )


def _upsert_summary(
    session: Session, purchase_order: dict, dispute: dict, scenario: DisputeScenario, invoice_date: date
) -> None:
    retailer_name = session.scalar(
        select(Retailer.retailer_name).where(Retailer.id == purchase_order["retailer_id"])
    )
    agent_id = session.scalar(
        select(Agent.id).where(Agent.agent_code == "penalty_dispute_summary", Agent.is_active.is_(True))
    )
    if agent_id is None:
        print("  no active penalty_dispute_summary agent; run scripts.seed.seed_agents. Summary skipped.")
        return
    text = _render_summary(scenario, dispute, retailer_name or "the retailer")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    session.add(
        PenaltySummary(
            purchase_order_id=purchase_order["id"],
            summary_type="DISPUTE",
            as_of_date=invoice_date,
            agent_id=agent_id,
            context_hash=digest,
            content_fingerprint=digest,
            model_name="deterministic-template",
            status="READY",
            summary=text,
        )
    )
    session.flush()


def _render_summary(scenario: DisputeScenario, dispute: dict, retailer_name: str) -> str:
    """A plain narrative built only from the engine's analysis of this dispute."""
    breakdown = dispute["analysis_breakdown"] or {}
    claimed = dispute["claimed_amount"]
    computed = dispute["computed_amount"]
    recovery = max(0.0, claimed - computed)
    verdict = dispute["override_verdict"] or dispute["verdict"]
    recommendation = {
        "NO_PAY": "Contest in full.",
        "PAY_PARTIAL": "Contest the overcharge and accept the contractual amount.",
        "PAY_FULL": "Accept the charge; it matches the contract.",
    }.get(verdict, "Review manually.")

    required = breakdown.get("required_delivery_date")
    actual = breakdown.get("actual_delivery_date")
    deadline = breakdown.get("deadline")
    is_late = breakdown.get("facts", {}).get("is_late")
    if is_late and actual and deadline:
        days_late = (date.fromisoformat(actual) - date.fromisoformat(deadline)).days
        timing = f"delivered {actual}, {days_late} day(s) after the grace-adjusted deadline {deadline}"
    else:
        timing = f"delivered {actual}, on or before the deadline {deadline}"

    events = breakdown.get("telematics_events") or []
    event_lines = (
        "\n".join(
            f"  - `{e['event_type']}` at {e['event_at']} (source {e['source']}, ref {e['source_reference'] or 'n/a'})"
            for e in events
        )
        or "  - No carrier or EDI events are on record for this plan."
    )

    return (
        "### 1. Adjudication Verdict & Financial Impact\n\n"
        f"- **Verdict**: **{verdict}**\n"
        f"- **Claimed**: ${claimed:,.2f} | **Contractual liability**: ${computed:,.2f} | "
        f"**Recoverable**: ${recovery:,.2f}\n"
        f"- **Recommendation**: {recommendation} {scenario.retailer_note}\n\n"
        "### 2. Contractual Grounds & Evidence\n\n"
        f"- **Rule applied**: `{breakdown.get('rule_code')}` ({breakdown.get('calc_type')}, "
        f"grace {breakdown.get('grace_period_days')} day(s), cap {breakdown.get('cap_amount') or 'none'})\n"
        f"- **Required delivery date**: {required} | **Outcome**: {timing}\n"
        "- **Recorded events**:\n"
        f"{event_lines}\n\n"
        "### 3. Dispute Letter Draft\n\n"
        "```text\n"
        f"DISPUTE NOTICE - PO {scenario.po_number}, claim {scenario.penalty_number}\n"
        f"To: {retailer_name} Vendor Compliance\n"
        "From: Mars Petcare US Logistics Compliance\n\n"
        f"Mars Petcare disputes the ${claimed:,.2f} {scenario.violation_type} deduction on PO {scenario.po_number}. "
        f"Recomputing under rule {breakdown.get('rule_code')}, the shipment was {timing}. "
        f"The contractual amount is ${computed:,.2f}; we request a credit of ${recovery:,.2f}.\n\n"
        "Mars Petcare Supply Chain Resolution Team\n"
        "```\n"
    )


if __name__ == "__main__":
    seed_timeline_disputes()
