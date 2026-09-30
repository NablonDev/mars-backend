"""Seed timeline-based actual penalties, disputes, and V3 dispute summaries.

Seeds authentic, event-driven dispute scenarios for:
- TL-S01-PO (Amazon PREPAID - On-Time Proof via EDI 214 telematics -> FULL OVERTURN)
- TL-S08-PO (Costco PREPAID - Weather Disruption & Grace Period -> PARTIAL OVERTURN)
- TL-S10A-PO (Target PREPAID - Rescheduled Inbound Appointment Proof -> FULL OVERTURN)
- TL-S07-PO (Walmart COLLECT - FOB Origin Mars Staging Compliance -> FULL OVERTURN)
- TL-S04-PO (Walmart PREPAID - Signed BOL Dock Receipt Fill Rate -> PARTIAL OVERTURN)
"""

from __future__ import annotations

import datetime
import hashlib
from decimal import Decimal

from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import generate_uuid7
from app.db.session import Database
from app.models.common import PurchaseOrder, PurchaseOrderLine
from app.models.enums import DisputeReasonCode, DisputeStatus, DisputeVerdict, SummaryStatus
from app.models.penalties import ActualPenalty, PenaltyDispute, PenaltySummary
from app.models.process import Agent


def seed_timeline_disputes() -> None:
    settings = get_settings()
    db = Database(settings.database.url)

    with db.session() as session:
        # Retrieve active V3 agent for dispute summary
        agent = session.scalar(
            select(Agent).where(
                Agent.agent_code == "penalty_dispute_summary",
                Agent.prompt_version == "v3",
            )
        )
        agent_id = agent.id if agent else None

        scenarios = [
            {
                "po_number": "TL-S01-PO",
                "penalty_number": "ACT-TL-S01",
                "dispute_number": "DSP-TL-001",
                "violation_type": "OTIF_LATE",
                "claimed_amount": Decimal("500.00"),
                "computed_amount": Decimal("0.00"),
                "delta_amount": Decimal("500.00"),
                "verdict": DisputeVerdict.NO_PAY.value,
                "status": DisputeStatus.RESOLVED.value,
                "reason": DisputeReasonCode.NOT_LATE.value,
                "invoice_date": datetime.date(2026, 9, 28),
                "due_date": datetime.date(2026, 10, 15),
                "notes": "EDI 214 telematics and signed POD confirm dock delivery at Amazon DC on 2026-09-25 14:00 UTC within MABD window (deadline 2026-09-27). Penalty dismissed in full.",
                "resolved_by": "agent:dispute_auto_resolver",
                "summary": (
                    "### 1. Adjudication Verdict & Financial Impact\n\n"
                    "- **Adjudication Verdict**: **FULL OVERTURN (NO_PAY)**\n"
                    "- **Claimed Penalty**: $500.00 | **Contractual Liability**: $0.00 | **Recovery Amount**: **$500.00**\n"
                    "- **Recommendation**: Rebut in full. Amazon erroneously debited an OTIF late fee despite documented on-time delivery.\n\n"
                    "### 2. Contractual Grounds & Telematics Proof\n\n"
                    "- **Contractual Delivery Window**: 2026-09-23 to 2026-09-27 (MABD Window)\n"
                    "- **Actual Delivery Timestamp**: **2026-09-25 14:00 UTC**\n"
                    "- **Telematics Evidence**:\n"
                    "  - `EDI 214 Carrier Status`: Carrier confirmed on-dock arrival at Amazon BWI2 on 2026-09-25 13:42 UTC.\n"
                    "  - `Delivery Completed Event`: Unloading completed and electronic signature received at 14:00 UTC.\n"
                    "  - `POD Reference`: POD-994821 signed by Amazon Receiving Supervisor.\n"
                    "- **Governing Contract Terms**: Amazon Vendor Manual Section 4.1 stipulates delivery within the 4-day PO window is fully compliant with zero chargeback exposure.\n\n"
                    "### 3. Formal Retailer Dispute Letter Draft\n\n"
                    "```text\n"
                    "DISPUTE NOTICE — RE: CHARGEBACK DEDUCTION ON PO TL-S01-PO\n"
                    "To: Amazon Vendor Compliance & Accounts Payable\n"
                    "From: Mars Petcare US Logistics Compliance\n"
                    "Date: September 28, 2026\n"
                    "Claim Reference: ACT-TL-S01 / DSP-TL-001\n"
                    "PO Number: TL-S01-PO\n"
                    "Deduction Amount: $500.00 (OTIF Late Delivery)\n\n"
                    "Dear Amazon Vendor Compliance Team,\n\n"
                    "Mars Petcare respectfully submits this formal dispute regarding the $500.00 OTIF late penalty assessed against PO TL-S01-PO.\n\n"
                    "Under Section 4.1 of the Amazon Master Vendor Agreement, conforming delivery occurs within the contractual window of September 23 – September 27, 2026. Electronic telematics logs (EDI 214 transaction #994821) and carrier gate time-stamps confirm that trailer arrival occurred on September 25, 2026 at 13:42 UTC, two calendar days ahead of the delivery cutoff.\n\n"
                    "As physical delivery was executed fully on-time and verified by Amazon dock personnel, we request immediate cancellation of the deduction and credit back to Mars's account on the next remittance cycle.\n\n"
                    "Sincerely,\n"
                    "Mars Petcare Supply Chain Resolution Team\n"
                    "```"
                ),
            },
            {
                "po_number": "TL-S08-PO",
                "penalty_number": "ACT-TL-S08",
                "dispute_number": "DSP-TL-002",
                "violation_type": "OTIF_LATE",
                "claimed_amount": Decimal("1200.00"),
                "computed_amount": Decimal("600.00"),
                "delta_amount": Decimal("600.00"),
                "verdict": DisputeVerdict.PAY_PARTIAL.value,
                "status": DisputeStatus.ANALYZED.value,
                "reason": DisputeReasonCode.NOT_LATE.value,
                "invoice_date": datetime.date(2026, 9, 27),
                "due_date": datetime.date(2026, 10, 8),
                "notes": "Carrier telematics records verified weather disruption on I-80 corridor (Ticket #WY-9812). Under Section 6.2 Force Majeure & Grace Protocol, 2 days of transit delay are contractually excused. Reclaiming $600.00 difference.",
                "resolved_by": None,
                "summary": (
                    "### 1. Adjudication Verdict & Financial Impact\n\n"
                    "- **Adjudication Verdict**: **PARTIAL OVERTURN (PAY_PARTIAL)**\n"
                    "- **Claimed Penalty**: $1,200.00 | **Contractual Liability**: $600.00 | **Recovery Amount**: **$600.00**\n"
                    "- **Recommendation**: Contest $600.00 of the deduction under Costco Force Majeure & Emergency Transit exception rules.\n\n"
                    "### 2. Contractual Grounds & Telematics Proof\n\n"
                    "- **Contractual Delivery Baseline**: 2026-09-21 | **Actual Delivery**: 2026-09-24\n"
                    "- **Telematics Evidence**:\n"
                    "  - `Carrier Exception Log`: Severe snowstorm blizzard forced mandatory I-80 commercial vehicle closure by Wyoming Highway Patrol (WHP Bulletin #WY-9812).\n"
                    "  - `Fulfillment Event Trace`: `PLAN_CHANGED` event logged with reason code `WEATHER_DELAY` on 2026-09-21.\n"
                    "  - `EDI 214 Update`: Carrier dispatched immediately upon road reopening and delivered 2026-09-24.\n"
                    "- **Governing Contract Terms**: Costco Vendor Agreement Section 6.2 grants 48-hour penalty abatement for verified severe meteorological emergencies.\n\n"
                    "### 3. Formal Retailer Dispute Letter Draft\n\n"
                    "```text\n"
                    "DISPUTE NOTICE — RE: WEATHER DELAY ABATEMENT ON PO TL-S08-PO\n"
                    "To: Costco Wholesale Accounts Receivable & Vendor Compliance\n"
                    "From: Mars Petcare US Logistics Compliance\n"
                    "Date: September 27, 2026\n"
                    "Claim Reference: ACT-TL-S08 / DSP-TL-002\n"
                    "PO Number: TL-S08-PO\n"
                    "Assessed Fine: $1,200.00 | Contested Fine: $600.00\n\n"
                    "Dear Costco Vendor Accounting,\n\n"
                    "Mars Petcare requests a partial reversal of $600.00 regarding the OTIF deduction for PO TL-S08-PO.\n\n"
                    "Shipment transit from Mars's facility was delayed between September 21 and September 23 due to the complete closure of the Interstate 80 corridor across Wyoming under severe winter weather warnings. Independent state DOT road closure documentation and carrier GPS logs are attached.\n\n"
                    "Pursuant to Costco Vendor Agreement Section 6.2 (Acts of Nature and Force Majeure Highway Closures), transit delays resulting from government-mandated highway shutdowns qualify for 48 hours of non-punitive delivery extension. Adjusting for the 2-day excused period reduces the penalty from $1,200.00 to $600.00. We request a credit memo of $600.00 on the subsequent remittance advice.\n\n"
                    "Sincerely,\n"
                    "Mars Petcare Supply Chain Resolution Team\n"
                    "```"
                ),
            },
            {
                "po_number": "TL-S10A-PO",
                "penalty_number": "ACT-TL-S10A",
                "dispute_number": "DSP-TL-003",
                "violation_type": "OTIF_LATE",
                "claimed_amount": Decimal("750.00"),
                "computed_amount": Decimal("0.00"),
                "delta_amount": Decimal("750.00"),
                "verdict": DisputeVerdict.NO_PAY.value,
                "status": DisputeStatus.OPEN.value,
                "reason": DisputeReasonCode.NOT_LATE.value,
                "invoice_date": datetime.date(2026, 9, 28),
                "due_date": datetime.date(2026, 10, 3),
                "notes": "Target inbound scheduler shifted appointment from 08:00 to 14:00 (Target TMS Ref #TGT-APPT-8812). Carrier completed delivery at 13:45. Telematics proof attached.",
                "resolved_by": None,
                "summary": (
                    "### 1. Adjudication Verdict & Financial Impact\n\n"
                    "- **Adjudication Verdict**: **FULL OVERTURN (NO_PAY)**\n"
                    "- **Claimed Penalty**: $750.00 | **Contractual Liability**: $0.00 | **Recovery Amount**: **$750.00**\n"
                    "- **Recommendation**: Submit dispute immediately. Target assessed an appointment miss fine for a time slot Target unilaterally rescheduled.\n\n"
                    "### 2. Contractual Grounds & Telematics Proof\n\n"
                    "- **Original Appointment**: 2026-09-25 08:00\n"
                    "- **Rescheduled Target Appointment**: **2026-09-25 14:00** (Target TMS confirmation #TGT-APPT-8812)\n"
                    "- **Actual Carrier Check-in**: **2026-09-25 13:45 UTC** (15 minutes early)\n"
                    "- **Telematics Evidence**:\n"
                    "  - `Gate Arrival Event`: Geofence GPS check-in at Target DC 0588 at 13:45 UTC.\n"
                    "  - `Appointment Confirmation Record`: Target Transportation portal timestamp verifying slot re-assignment.\n"
                    "- **Governing Contract Terms**: Target Vendor Agreement Section 9.3 explicitly prohibits late penalty assessment when appointment revisions originate from Target DC staging capacity constraints.\n\n"
                    "### 3. Formal Retailer Dispute Letter Draft\n\n"
                    "```text\n"
                    "DISPUTE NOTICE — RE: UNILATERAL APPOINTMENT RESCHEDULE ON PO TL-S10A-PO\n"
                    "To: Target Corporation Vendor Reconciliation Group\n"
                    "From: Mars Petcare US Logistics Compliance\n"
                    "Date: September 28, 2026\n"
                    "Claim Reference: ACT-TL-S10A / DSP-TL-003\n"
                    "PO Number: TL-S10A-PO\n"
                    "Deduction Amount: $750.00 (Late Delivery / Missed Appointment)\n\n"
                    "Dear Target Vendor Compliance Team,\n\n"
                    "Mars Petcare requests immediate reversal of the $750.00 non-compliance penalty charged on PO TL-S10A-PO.\n\n"
                    "The penalty notice asserts that delivery missed the initial 08:00 dock appointment on September 25, 2026. However, Target TMS records show that Target Inbound Planning rescheduled this appointment to 14:00 due to internal cross-dock congestion (Confirmation #TGT-APPT-8812). Our carrier arrived at Target DC 0588 at 13:45, within the required 30-minute pre-appointment arrival window.\n\n"
                    "Under Target Vendor Agreement Section 9.3, penalties may not be levied for appointment changes initiated by Target. Please cancel this charge and confirm credit in the Target Vendor Portal.\n\n"
                    "Sincerely,\n"
                    "Mars Petcare Supply Chain Resolution Team\n"
                    "```"
                ),
            },
            {
                "po_number": "TL-S07-PO",
                "penalty_number": "ACT-TL-S07",
                "dispute_number": "DSP-TL-004",
                "violation_type": "OTIF_LATE",
                "claimed_amount": Decimal("900.00"),
                "computed_amount": Decimal("0.00"),
                "delta_amount": Decimal("900.00"),
                "verdict": DisputeVerdict.NO_PAY.value,
                "status": DisputeStatus.ANALYZED.value,
                "reason": DisputeReasonCode.NOT_LATE.value,
                "invoice_date": datetime.date(2026, 9, 28),
                "due_date": datetime.date(2026, 10, 5),
                "notes": "Order TL-S07-PO is FOB Origin (COLLECT). Mars WMS gate logs confirm order staged READY_FOR_PICKUP on 2026-09-22 08:00, within routing guide window. Delay was caused by Walmart designated carrier tender delay. Under Master Vendor Agreement Section 8.1, Vendor is exempt from transit delays on Collect terms.",
                "resolved_by": None,
                "summary": (
                    "### 1. Adjudication Verdict & Financial Impact\n\n"
                    "- **Adjudication Verdict**: **FULL OVERTURN (NO_PAY)**\n"
                    "- **Claimed Penalty**: $900.00 | **Contractual Liability**: $0.00 | **Recovery Amount**: **$900.00**\n"
                    "- **Recommendation**: Contest in full. Freight term is COLLECT; Mars satisfied all outbound staging obligations.\n\n"
                    "### 2. Contractual Grounds & Telematics Proof\n\n"
                    "- **Freight Term**: **COLLECT (FOB Origin)**\n"
                    "- **Contractual Milestone Obligation**: `READY_FOR_PICKUP` at Mars Facility dock\n"
                    "- **Milestone Timestamp**: Staged & confirmed on **2026-09-22 08:00 UTC**\n"
                    "- **Telematics Evidence**:\n"
                    "  - `WMS Staging Confirmation`: Order staged at Joplin dock door 14 on 2026-09-22.\n"
                    "  - `Routing Request Transmitted`: 2026-09-20 via EDI 753.\n"
                    "  - `Carrier Delay`: Walmart-contracted carrier did not arrive until 2026-09-24.\n"
                    "- **Governing Contract Terms**: Walmart Master Vendor Agreement Section 8.1 dictates that for Collect orders, Vendor liability terminates once goods are made available for pickup within the confirmed shipping window.\n\n"
                    "### 3. Formal Retailer Dispute Letter Draft\n\n"
                    "```text\n"
                    "DISPUTE NOTICE — RE: COLLECT FREIGHT TERM EXEMPTION ON PO TL-S07-PO\n"
                    "To: Walmart Accounts Payable & Inbound Compliance\n"
                    "From: Mars Petcare US Logistics Compliance\n"
                    "Date: September 28, 2026\n"
                    "Claim Reference: ACT-TL-S07 / DSP-TL-004\n"
                    "PO Number: TL-S07-PO\n"
                    "Deduction Amount: $900.00 (OTIF Late Transit)\n\n"
                    "Dear Walmart AP Compliance Group,\n\n"
                    "Mars Petcare hereby disputes the $900.00 OTIF deduction charged against PO TL-S07-PO.\n\n"
                    "This purchase order was issued under COLLECT freight terms (FOB Origin). Mars transmitted the EDI 753 routing request on September 20, 2026, and staged the freight ready for carrier pickup on September 22 at 08:00 UTC, compliant with the contractual pickup window. Walmart's designated truckload carrier arrived two days late on September 24.\n\n"
                    "Under Walmart Supplier Standards Section 8.1, suppliers are hold-harmless for carrier transit delays on Collect orders once goods are staged on schedule. We request immediate reversal and credit of the $900.00 deduction.\n\n"
                    "Sincerely,\n"
                    "Mars Petcare Supply Chain Resolution Team\n"
                    "```"
                ),
            },
            {
                "po_number": "TL-S04-PO",
                "penalty_number": "ACT-TL-S04",
                "dispute_number": "DSP-TL-005",
                "violation_type": "SHORT_SHIP",
                "claimed_amount": Decimal("1400.00"),
                "computed_amount": Decimal("400.00"),
                "delta_amount": Decimal("1000.00"),
                "verdict": DisputeVerdict.PAY_PARTIAL.value,
                "status": DisputeStatus.RESOLVED.value,
                "reason": DisputeReasonCode.QTY_CONFIRMED.value,
                "invoice_date": datetime.date(2026, 9, 26),
                "due_date": datetime.date(2026, 10, 10),
                "notes": "Walmart intake scan recorded partial receipt of 1,200 cases. Deduction should apply only to unfilled 200 units ($400.00). Walmart AP credited $1,000.00 on Remittance #WMT-REM-4491.",
                "resolved_by": "agent:dispute_auto_resolver",
                "summary": (
                    "### 1. Adjudication Verdict & Financial Impact\n\n"
                    "- **Adjudication Verdict**: **PARTIAL OVERTURN (PAY_PARTIAL)**\n"
                    "- **Claimed Penalty**: $1,400.00 | **Contractual Liability**: $400.00 | **Recovery Amount**: **$1,000.00**\n"
                    "- **Recommendation**: Reconcile deduction. Walmart charged a full cancellation penalty ($1,400.00), but signed delivery receipt confirms 1,200 of 1,400 cases were delivered.\n\n"
                    "### 2. Contractual Grounds & Telematics Proof\n\n"
                    "- **Ordered Quantity**: 1,400 cases | **Delivered Quantity**: **1,200 cases** | **Shortfall**: 200 cases\n"
                    "- **Contractual Penalty Formula**: $2.00 per unfulfilled unit = $400.00 actual exposure\n"
                    "- **Evidentiary Proof**:\n"
                    "  - `Signed BOL Dock Stamp`: BOL #WMT-BOL-7731 signed by Walmart receiving receiver.\n"
                    "  - `EDI 856 ASN Audit`: Advance Ship Notice sent with 1,200 confirmed shipped units.\n"
                    "- **Governing Contract Terms**: Walmart Supplier Standards Section 3.2 specifies fill rate chargebacks apply strictly to the net unfulfilled quantity, not the aggregate purchase order volume.\n\n"
                    "### 3. Formal Retailer Dispute Letter Draft\n\n"
                    "```text\n"
                    "DISPUTE NOTICE — RE: QUANTITY RECONCILIATION ON PO TL-S04-PO\n"
                    "To: Walmart Accounts Payable & Deduction Processing\n"
                    "From: Mars Petcare US Logistics Compliance\n"
                    "Date: September 26, 2026\n"
                    "Claim Reference: ACT-TL-S04 / DSP-TL-005\n"
                    "PO Number: TL-S04-PO\n"
                    "Original Deduction: $1,400.00 | Adjusted Liability: $400.00 | Refund Claim: $1,000.00\n\n"
                    "Dear Walmart Deduction Processing Group,\n\n"
                    "Mars Petcare submits this documentation to adjust the short ship deduction assessed against PO TL-S04-PO.\n\n"
                    "The deduction applied a full $1,400.00 fine across the entire order quantity. Attached BOL #WMT-BOL-7731 and EDI 856 transmission verify receipt of 1,200 cases, leaving a net variance of 200 cases. Under Walmart Agreement Section 3.2 ($2.00 per short unit), total contractual liability is $400.00.\n\n"
                    "We request an immediate credit memo adjustment of $1,000.00 to resolve this claim.\n\n"
                    "Sincerely,\n"
                    "Mars Petcare Supply Chain Resolution Team\n"
                    "```"
                ),
            },
        ]

        created_actuals = 0
        created_disputes = 0
        created_summaries = 0

        for sc in scenarios:
            po = session.scalar(
                select(PurchaseOrder).where(PurchaseOrder.purchase_order_number == sc["po_number"])
            )
            if not po:
                print(f"Skipping {sc['po_number']}: PO not found in database.")
                continue

            po_line = session.scalar(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)
            )

            # 1. Upsert Actual Penalty
            actual = session.scalar(
                select(ActualPenalty).where(ActualPenalty.actual_penalty_number == sc["penalty_number"])
            )
            if not actual:
                actual = ActualPenalty(
                    id=generate_uuid7(),
                    actual_penalty_number=sc["penalty_number"],
                    purchase_order_id=po.id,
                    purchase_order_line_id=po_line.id if po_line else None,
                    violation_type=sc["violation_type"],
                    actual_penalty_amount=sc["claimed_amount"],
                    invoice_or_deduction_date=sc["invoice_date"],
                    dispute_status=sc["status"],
                )
                session.add(actual)
                session.flush()
                created_actuals += 1
            else:
                actual.dispute_status = sc["status"]
                actual.actual_penalty_amount = sc["claimed_amount"]

            # 2. Upsert Penalty Dispute
            dispute = session.scalar(
                select(PenaltyDispute).where(PenaltyDispute.dispute_number == sc["dispute_number"])
            )
            if not dispute:
                dispute = PenaltyDispute(
                    id=generate_uuid7(),
                    dispute_number=sc["dispute_number"],
                    actual_penalty_id=actual.id,
                    purchase_order_id=po.id,
                    reason_code=sc["reason"],
                    claimed_amount=sc["claimed_amount"],
                    computed_amount=sc["computed_amount"],
                    delta_amount=sc["delta_amount"],
                    verdict=sc["verdict"],
                    dispute_status=sc["status"],
                    notes=sc["notes"],
                    response_due_date=sc["due_date"],
                    resolved_at=(
                        datetime.datetime.now(datetime.UTC)
                        if sc["status"] == DisputeStatus.RESOLVED.value
                        else None
                    ),
                    resolved_by=sc["resolved_by"],
                )
                session.add(dispute)
                session.flush()
                created_disputes += 1
            else:
                dispute.claimed_amount = sc["claimed_amount"]
                dispute.computed_amount = sc["computed_amount"]
                dispute.delta_amount = sc["delta_amount"]
                dispute.verdict = sc["verdict"]
                dispute.dispute_status = sc["status"]
                dispute.notes = sc["notes"]
                dispute.response_due_date = sc["due_date"]
                dispute.resolved_by = sc["resolved_by"]

            # 3. Upsert V3 Dispute Summary in penalties.penalty_summary
            ctx_hash = hashlib.sha256(sc["summary"].encode("utf-8")).hexdigest()
            summary_row = session.scalar(
                select(PenaltySummary).where(
                    PenaltySummary.purchase_order_id == po.id,
                    PenaltySummary.summary_type == "DISPUTE",
                )
            )
            if not summary_row:
                summary_row = PenaltySummary(
                    id=generate_uuid7(),
                    purchase_order_id=po.id,
                    summary_type="DISPUTE",
                    as_of_date=sc["invoice_date"],
                    agent_id=agent_id,
                    context_hash=ctx_hash,
                    content_fingerprint=ctx_hash,
                    model_name="azure-openai:gpt-4o",
                    status=SummaryStatus.READY.value,
                    summary=sc["summary"],
                )
                session.add(summary_row)
                created_summaries += 1
            else:
                summary_row.summary = sc["summary"]
                summary_row.status = SummaryStatus.READY.value
                summary_row.as_of_date = sc["invoice_date"]
                summary_row.context_hash = ctx_hash
                summary_row.content_fingerprint = ctx_hash
                summary_row.model_name = "azure-openai:gpt-4o"
                if agent_id:
                    summary_row.agent_id = agent_id

        session.commit()
        print(
            f"Successfully seeded {created_actuals} actual penalties, {created_disputes} disputes, and {created_summaries} V3 summaries."
        )


if __name__ == "__main__":
    seed_timeline_disputes()
