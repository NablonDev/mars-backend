"""Seeds rich, authentic database fixtures into mars_e2e_verify for end-to-end frontend verification.

Populates:
1. Multi-retailer historical actual penalties (Jan-Sep 2026 for Walmart, Amazon, Costco)
2. Penalty disputes (resolved, analyzed, open with claimed amounts, verdicts, grounds)
3. Penalty interception logs (mitigations executed in current month avoiding fines)
4. Extracted penalty rules and attributes for retailer agreements
5. CMIR records across 6 months with healthy records and records needing attention
6. Workflow threads and pending human actions for HITL actions / error queue
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import generate_uuid7
from app.db.session import Database
from app.models.cmir import CmirRecord
from app.models.common import (
    Material,
    Plant,
    PurchaseOrder,
    PurchaseOrderLine,
    Retailer,
    RetailerAgreement,
    Sku,
)
from app.models.enums import (
    DisputeReasonCode,
    DisputeStatus,
    DisputeVerdict,
    WorkflowThreadSubjectType,
)
from app.models.penalties import (
    ActualPenalty,
    ExtractedPenaltyRule,
    ExtractedPenaltyRuleAttribute,
    MitigationOption,
    PenaltyDispute,
    PenaltyInterceptionLog,
)
from app.models.process import Agent, AgentRun, HumanAction, WorkflowThread, WorkflowThreadSubject


def seed_showcase_data() -> None:
    settings = get_settings()
    db = Database(settings.database.url)

    with db.session() as session:
        print("Connected to database:", settings.database.url)

        # -------------------------------------------------------------
        # 1. Ensure Retailers: Walmart, Amazon, Costco
        # -------------------------------------------------------------
        walmart = session.scalars(select(Retailer).where(Retailer.retailer_code == "RET-WMT")).first()
        amazon = session.scalars(select(Retailer).where(Retailer.retailer_code == "RET-AMZ")).first()
        costco = session.scalars(select(Retailer).where(Retailer.retailer_code == "RET-COSTCO")).first()
        assert walmart is not None, "Walmart retailer required"
        assert amazon is not None, "Amazon retailer required"

        if costco is None:
            costco = Retailer(
                id=generate_uuid7(),
                retailer_code="RET-COSTCO",
                retailer_name="Costco Wholesale",
                priority_tier="TIER_1",
                stacking_mode="INDEPENDENT",
                source_system="SAP",
            )
            session.add(costco)
            session.flush()
            print("Added Costco retailer:", costco.id)

        # Ensure Costco agreement
        costco_agreement = session.scalars(
            select(RetailerAgreement).where(RetailerAgreement.retailer_id == costco.id)
        ).first()
        if costco_agreement is None:
            costco_agreement = RetailerAgreement(
                id=generate_uuid7(),
                retailer_id=costco.id,
                contract_code="COSTCO-MNA-2026",
                title="Costco Wholesale Master Vendor Agreement 2026",
                document_sha256="c057c0f1e0000000000000000000000000000000000000000000000000000001",
                markdown_text="# Costco Master Vendor Agreement\n\nSection 4.1 On-Time Delivery...",
                effective_date=datetime.date(2026, 1, 1),
                expiration_date=datetime.date(2026, 12, 31),
                dispute_window_days=30,
            )
            session.add(costco_agreement)
            session.flush()
            print("Added Costco agreement:", costco_agreement.id)

        # Find Walmart and Amazon agreements
        walmart_agreement = session.scalars(
            select(RetailerAgreement).where(RetailerAgreement.retailer_id == walmart.id)
        ).first()
        amazon_agreement = session.scalars(
            select(RetailerAgreement).where(RetailerAgreement.retailer_id == amazon.id)
        ).first()

        # Ensure Purchase Orders for Costco
        costco_po = session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.retailer_id == costco.id)
        ).first()
        if costco_po is None:
            costco_po = PurchaseOrder(
                id=generate_uuid7(),
                purchase_order_number="CST-990142",
                retailer_id=costco.id,
                retailer_po_number="PO-CST-88124",
                order_date=datetime.date(2026, 2, 10),
                requested_delivery_date=datetime.date(2026, 2, 18),
                required_ship_date=datetime.date(2026, 2, 15),
                order_status="DELIVERED",
                source_system="SAP",
            )
            session.add(costco_po)
            session.flush()
            print("Added Costco PO:", costco_po.id)

        # Ensure Purchase Order Line for Costco
        costco_pol = session.scalars(
            select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == costco_po.id)
        ).first()
        if costco_pol is None:
            sku = session.scalars(select(Sku)).first()
            mat = session.scalars(select(Material)).first()
            plant = session.scalars(select(Plant)).first()
            costco_pol = PurchaseOrderLine(
                id=generate_uuid7(),
                purchase_order_id=costco_po.id,
                line_number="10",
                retailer_po_line_number="1",
                sku_id=sku.id if sku else None,
                material_id=mat.id if mat else None,
                plant_id=plant.id if plant else None,
                ordered_quantity=Decimal("1200.000"),
                unit_price=Decimal("45.50"),
                uom="CS",
                requested_delivery_date=datetime.date(2026, 2, 18),
                required_ship_date=datetime.date(2026, 2, 15),
                line_status="DELIVERED",
            )
            session.add(costco_pol)
            session.flush()
            print("Added Costco PO Line:", costco_pol.id)

        # Gather POs by retailer
        walmart_pos = list(
            session.scalars(select(PurchaseOrder).where(PurchaseOrder.retailer_id == walmart.id)).all()
        )
        amazon_pos = list(
            session.scalars(select(PurchaseOrder).where(PurchaseOrder.retailer_id == amazon.id)).all()
        )

        # -------------------------------------------------------------
        # 2. Historical Actual Penalties across Jan - Sep 2026
        # -------------------------------------------------------------
        existing_penalties = session.scalars(select(ActualPenalty)).all()
        # If fewer than 15 actual penalties exist, seed the monthly history
        if len(existing_penalties) < 15:
            print(
                f"Seeding historical actual penalties across 2026 (current count: {len(existing_penalties)})..."
            )
            monthly_fines_plan = [
                # (month_num, day, retailer, po, violation_type, amount, dispute_status)
                (1, 15, walmart, walmart_pos[0], "OTIF_LATE", Decimal("12500.00"), "PAID"),
                (1, 20, amazon, amazon_pos[0], "SHORT_SHIP", Decimal("6400.00"), "PAID"),
                (1, 25, costco, costco_po, "ASN_LATE", Decimal("4100.00"), "PAID"),
                (2, 12, walmart, walmart_pos[0], "SHORT_SHIP", Decimal("18200.00"), "PAID"),
                (2, 18, amazon, amazon_pos[0], "OTIF_LATE", Decimal("8900.00"), "PAID"),
                (2, 22, costco, costco_po, "PALLET_NON_COMPLIANCE", Decimal("5200.00"), "PAID"),
                (
                    3,
                    10,
                    walmart,
                    walmart_pos[1] if len(walmart_pos) > 1 else walmart_pos[0],
                    "OTIF_LATE",
                    Decimal("14000.00"),
                    "PAID",
                ),
                (
                    3,
                    15,
                    amazon,
                    amazon_pos[1] if len(amazon_pos) > 1 else amazon_pos[0],
                    "ASN_LATE",
                    Decimal("7500.00"),
                    "PAID",
                ),
                (3, 28, costco, costco_po, "SHORT_SHIP", Decimal("3800.00"), "PAID"),
                (4, 8, walmart, walmart_pos[0], "SHORT_SHIP", Decimal("16800.00"), "PAID"),
                (4, 19, amazon, amazon_pos[0], "OTIF_LATE", Decimal("9200.00"), "PAID"),
                (4, 24, costco, costco_po, "OTIF_LATE", Decimal("6000.00"), "PAID"),
                (
                    5,
                    14,
                    walmart,
                    walmart_pos[1] if len(walmart_pos) > 1 else walmart_pos[0],
                    "OTIF_LATE",
                    Decimal("11500.00"),
                    "PAID",
                ),
                (
                    5,
                    21,
                    amazon,
                    amazon_pos[1] if len(amazon_pos) > 1 else amazon_pos[0],
                    "SHORT_SHIP",
                    Decimal("5800.00"),
                    "PAID",
                ),
                (5, 27, costco, costco_po, "BARCODE_UNREADABLE", Decimal("4500.00"), "PAID"),
                (6, 11, walmart, walmart_pos[0], "ASN_LATE", Decimal("13400.00"), "PAID"),
                (6, 18, amazon, amazon_pos[0], "OTIF_LATE", Decimal("8100.00"), "PAID"),
                (6, 25, costco, costco_po, "SHORT_SHIP", Decimal("5000.00"), "PAID"),
                (7, 9, walmart, walmart_pos[0], "OTIF_LATE", Decimal("9800.00"), "PAID"),
                (7, 16, amazon, amazon_pos[0], "SHORT_SHIP", Decimal("6200.00"), "PAID"),
                (7, 23, costco, costco_po, "OTIF_LATE", Decimal("3500.00"), "PAID"),
                (
                    8,
                    12,
                    walmart,
                    walmart_pos[1] if len(walmart_pos) > 1 else walmart_pos[0],
                    "SHORT_SHIP",
                    Decimal("8500.00"),
                    "DISPUTED",
                ),
                (
                    8,
                    19,
                    amazon,
                    amazon_pos[1] if len(amazon_pos) > 1 else amazon_pos[0],
                    "ASN_LATE",
                    Decimal("5400.00"),
                    "PAID",
                ),
                (8, 26, costco, costco_po, "SHORT_SHIP", Decimal("2900.00"), "PAID"),
                (9, 5, walmart, walmart_pos[0], "OTIF_LATE", Decimal("6200.00"), "DISPUTED"),
                (9, 10, amazon, amazon_pos[0], "SHORT_SHIP", Decimal("4100.00"), "DISPUTED"),
                (9, 15, costco, costco_po, "OTIF_LATE", Decimal("2100.00"), "PAID"),
            ]
            for idx, (m_num, day, ret, po, violation, amt, disp_stat) in enumerate(monthly_fines_plan, 1):
                po_line = session.scalars(
                    select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)
                ).first()
                actual_pen = ActualPenalty(
                    id=generate_uuid7(),
                    actual_penalty_number=f"ACT-2026-{m_num:02d}{idx:02d}",
                    purchase_order_id=po.id,
                    purchase_order_line_id=po_line.id if po_line else None,
                    violation_type=violation,
                    actual_penalty_amount=amt,
                    invoice_or_deduction_date=datetime.date(2026, m_num, day),
                    dispute_status=disp_stat,
                )
                session.add(actual_pen)
            session.flush()
            print("Seeded monthly actual penalties.")

        # -------------------------------------------------------------
        # 3. Penalty Disputes (for Disputed vs Paid Tab)
        # -------------------------------------------------------------
        existing_disputes = session.scalars(select(PenaltyDispute)).all()
        if len(existing_disputes) == 0:
            print("Seeding penalty disputes...")
            # Pick some actual penalties
            penalties_list = list(session.scalars(select(ActualPenalty)).all())
            dispute_specs = [
                {
                    "num": "DSP-2026-001",
                    "pen": penalties_list[0],
                    "po": walmart_pos[0],
                    "claimed": Decimal("18400.00"),
                    "computed": Decimal("0.00"),
                    "verdict": DisputeVerdict.NO_PAY.value,
                    "status": DisputeStatus.RESOLVED.value,
                    "reason": DisputeReasonCode.NOT_LATE.value,
                    "notes": "Carrier EDI 214 confirmed on-dock appointment delivery at 09:15 within MABD window. Walmart AP cancelled fine in full.",
                    "due_date": datetime.date(2026, 9, 30),
                },
                {
                    "num": "DSP-2026-002",
                    "pen": penalties_list[1],
                    "po": amazon_pos[0],
                    "claimed": Decimal("14200.00"),
                    "computed": Decimal("4200.00"),
                    "verdict": DisputeVerdict.PAY_PARTIAL.value,
                    "status": DisputeStatus.ANALYZED.value,
                    "reason": DisputeReasonCode.QTY_CONFIRMED.value,
                    "notes": "POD signed by Amazon sorting supervisor verifies 980 cases received vs 900 scanned at conveyer dock. Reclaiming $10,000 difference.",
                    "due_date": datetime.date(2026, 9, 23),  # 5 days left
                },
                {
                    "num": "DSP-2026-003",
                    "pen": penalties_list[2],
                    "po": costco_po,
                    "claimed": Decimal("8500.00"),
                    "computed": None,
                    "verdict": None,
                    "status": DisputeStatus.OPEN.value,
                    "reason": DisputeReasonCode.RULE_MISAPPLIED.value,
                    "notes": "Delivery appointment rescheduled by Costco dispatch (Ticket #CST-88192). Grace window applies under Section 4.2.",
                    "due_date": datetime.date(2026, 9, 30),  # 12 days left
                },
                {
                    "num": "DSP-2026-004",
                    "pen": penalties_list[3],
                    "po": walmart_pos[0],
                    "claimed": Decimal("24000.00"),
                    "computed": None,
                    "verdict": None,
                    "status": DisputeStatus.OPEN.value,
                    "reason": DisputeReasonCode.NOT_LATE.value,
                    "notes": "Blizzard forced I-80 road closure verified by Wyoming DOT road report #WY-9812. Force majeure clause Section 14 invoked.",
                    "due_date": datetime.date(2026, 9, 20),  # 2 days left (high urgency!)
                },
                {
                    "num": "DSP-2026-005",
                    "pen": penalties_list[4],
                    "po": amazon_pos[0],
                    "claimed": Decimal("7800.00"),
                    "computed": Decimal("0.00"),
                    "verdict": DisputeVerdict.NO_PAY.value,
                    "status": DisputeStatus.RESOLVED.value,
                    "reason": DisputeReasonCode.AMOUNT_INCORRECT.value,
                    "notes": "Rate assessed at 5% tier rather than contractually agreed 2.5% tier. Amazon Vendor Central approved refund.",
                    "due_date": datetime.date(2026, 8, 15),
                },
            ]
            for spec in dispute_specs:
                disp = PenaltyDispute(
                    id=generate_uuid7(),
                    dispute_number=spec["num"],
                    actual_penalty_id=spec["pen"].id,
                    purchase_order_id=spec["po"].id,
                    reason_code=spec["reason"],
                    claimed_amount=spec["claimed"],
                    computed_amount=spec["computed"],
                    delta_amount=(spec["claimed"] - spec["computed"])
                    if spec["computed"] is not None
                    else None,
                    verdict=spec["verdict"],
                    dispute_status=spec["status"],
                    notes=spec["notes"],
                    response_due_date=spec["due_date"],
                    resolved_at=datetime.datetime.now(datetime.UTC) if spec["status"] == "RESOLVED" else None,
                    resolved_by="agent:dispute_auto_resolver" if spec["status"] == "RESOLVED" else None,
                )
                session.add(disp)
            session.flush()
            print("Seeded penalty disputes.")

        # -------------------------------------------------------------
        # 4. Agent Protection Interception Logs (Current Month)
        # -------------------------------------------------------------
        existing_interceptions = session.scalars(select(PenaltyInterceptionLog)).all()
        if len(existing_interceptions) == 0:
            print("Seeding penalty interception logs...")
            # Fetch mitigation options created during simulation
            mit_options = list(session.scalars(select(MitigationOption)).all())
            today = datetime.datetime.now(datetime.UTC)
            logs_specs = [
                (
                    walmart_pos[0],
                    mit_options[0].id if mit_options else generate_uuid7(),
                    "OTIF_LATE",
                    Decimal("1250.00"),
                    "Early carrier slot rebooking averted missed appointment delivery penalty.",
                    today - datetime.timedelta(days=14),
                ),
                (
                    amazon_pos[0],
                    mit_options[1].id if len(mit_options) > 1 else generate_uuid7(),
                    "SHORT_SHIP",
                    Decimal("850.00"),
                    "Split shipment dispatch routed from Plant 1002; full allocation preserved.",
                    today - datetime.timedelta(days=10),
                ),
                (
                    walmart_pos[0],
                    mit_options[2].id if len(mit_options) > 2 else generate_uuid7(),
                    "OTIF_LATE",
                    Decimal("2100.00"),
                    "Carrier expedited re-routing after mechanical failure on route I-95.",
                    today - datetime.timedelta(days=6),
                ),
                (
                    amazon_pos[0],
                    mit_options[3].id if len(mit_options) > 3 else generate_uuid7(),
                    "ASN_LATE",
                    Decimal("640.00"),
                    "Automated EDI 856 resend executed within 30-minute advance window.",
                    today - datetime.timedelta(days=3),
                ),
                (
                    walmart_pos[0],
                    mit_options[4].id if len(mit_options) > 4 else generate_uuid7(),
                    "SHORT_SHIP",
                    Decimal("1800.00"),
                    "Dynamic inventory reallocation from warehouse buffer covered line shortfall.",
                    today - datetime.timedelta(days=1),
                ),
                (
                    amazon_pos[0],
                    mit_options[5].id if len(mit_options) > 5 else generate_uuid7(),
                    "OTIF_LATE",
                    Decimal("950.00"),
                    "Agreed 4-hour receiving slot shift directly with Amazon freight operations.",
                    today,
                ),
            ]
            for po, opt_id, viol, amt, msg, created in logs_specs:
                log_entry = PenaltyInterceptionLog(
                    id=generate_uuid7(),
                    purchase_order_id=po.id,
                    mitigation_option_id=opt_id,
                    violation_type=viol,
                    amount_avoided=amt,
                    message=msg,
                    created_at=created,
                )
                session.add(log_entry)
            session.flush()
            print("Seeded penalty interception logs.")

        # -------------------------------------------------------------
        # 5. Extracted Penalty Rules & Attributes
        # -------------------------------------------------------------
        existing_extracted = session.scalars(select(ExtractedPenaltyRule)).all()
        if len(existing_extracted) == 0:
            print("Seeding extracted penalty rules and attributes...")
            rule_extractor_agent = session.scalars(
                select(Agent).where(Agent.agent_code == "penalty_rule_extractor")
            ).first()
            agent_run = AgentRun(
                id=generate_uuid7(),
                agent_id=rule_extractor_agent.id if rule_extractor_agent else None,
                status="SUCCEEDED",
                run_type="ON_DEMAND",
                started_at=datetime.datetime.now(datetime.UTC),
                completed_at=datetime.datetime.now(datetime.UTC),
                metadata_json={},
            )
            session.add(agent_run)
            session.flush()

            rule_specs = [
                {
                    "agreement": walmart_agreement,
                    "section": "Section 7.1",
                    "clause": "Supplier shall deliver 100% of purchase order quantities within the confirmed MABD window. Deliveries arriving after the specified delivery window shall incur a 3% deduction of the total order line value.",
                    "category": "OTIF",
                    "calc": "PERCENTAGE",
                    "status": "APPROVED",
                    "confidence": Decimal("0.96"),
                    "notes": "Standard Walmart OTIF delay penalty confirmed and aligned with Master Agreement.",
                    "attrs": [
                        ("OTIF_THRESHOLD", ">=", Decimal("98.0"), "%", "Clause 7.1 line 1"),
                        ("PENALTY_RATE", "=", Decimal("3.0"), "%", "Clause 7.1 line 2"),
                    ],
                },
                {
                    "agreement": walmart_agreement,
                    "section": "Section 7.3",
                    "clause": "Electronic Advance Shipping Notices (EDI 856) must be successfully transmitted and acknowledged no later than 30 minutes prior to delivery arrival. Missing or late ASN transmissions incur a flat fee of $250.00 per trailer.",
                    "category": "ASN",
                    "calc": "FLAT",
                    "status": "PENDING_REVIEW",
                    "confidence": Decimal("0.91"),
                    "notes": "Newly extracted requirement in 2026 terms; pending supply chain ops signoff.",
                    "attrs": [
                        ("ASN_LEAD_TIME", ">=", Decimal("30.0"), "minutes", "Clause 7.3 line 1"),
                        ("FLAT_PENALTY", "=", Decimal("250.0"), "USD", "Clause 7.3 line 2"),
                    ],
                },
                {
                    "agreement": walmart_agreement,
                    "section": "Section 8.2",
                    "clause": "All palletized shipments must comply with GMA Grade A standards and display legible SSCC-18 barcodes on two adjacent sides. Non-compliant pallets incur a $50.00 handling and relabeling surcharge per pallet.",
                    "category": "PALLET",
                    "calc": "FLAT",
                    "status": "PENDING_REVIEW",
                    "confidence": Decimal("0.88"),
                    "notes": "Verification needed on whether Mars packaging facilities meet updated wrap specs.",
                    "attrs": [
                        ("PALLET_SURCHARGE", "=", Decimal("50.0"), "USD", "Clause 8.2 line 2"),
                    ],
                },
                {
                    "agreement": amazon_agreement,
                    "section": "Section 4.2",
                    "clause": "Vendor Central purchase orders delivered past Carrier Requested Delivery Date (CRDD) are subject to a 2.5% invoice deduction assessed on delayed item total.",
                    "category": "OTIF",
                    "calc": "PERCENTAGE",
                    "status": "APPROVED",
                    "confidence": Decimal("0.95"),
                    "notes": "Approved Amazon late delivery rule.",
                    "attrs": [
                        ("LATE_DEDUCTION", "=", Decimal("2.5"), "%", "Clause 4.2 line 1"),
                    ],
                },
                {
                    "agreement": amazon_agreement,
                    "section": "Section 5.1",
                    "clause": "Carton barcodes with scan verification grades below ISO/IEC Grade C will be assessed a defect chargeback of $0.35 per carton.",
                    "category": "BARCODE",
                    "calc": "FLAT",
                    "status": "PENDING_REVIEW",
                    "confidence": Decimal("0.92"),
                    "notes": "Quality assurance audit required.",
                    "attrs": [
                        ("BARCODE_DEFECT_FEE", "=", Decimal("0.35"), "USD", "Clause 5.1 line 1"),
                    ],
                },
            ]
            for spec in rule_specs:
                rule_id = generate_uuid7()
                # 32 char fingerprint
                fp = uuid.uuid4().hex[:32]
                ex_rule = ExtractedPenaltyRule(
                    id=rule_id,
                    retailer_agreement_id=spec["agreement"].id,
                    agent_run_id=agent_run.id,
                    section=spec["section"],
                    clause_text=spec["clause"],
                    clause_fingerprint=fp,
                    penalty_category=spec["category"],
                    calc_type=spec["calc"],
                    po_shortage_flag=True,
                    po_delay_flag=True,
                    pricing_readiness="READY",
                    status=spec["status"],
                    confidence=spec["confidence"],
                    review_notes=spec["notes"],
                    extra={},
                )
                session.add(ex_rule)
                session.flush()

                for branch_no, (metric, op, val, unit, src) in enumerate(spec["attrs"], 1):
                    attr = ExtractedPenaltyRuleAttribute(
                        id=generate_uuid7(),
                        extracted_rule_id=rule_id,
                        branch_no=branch_no,
                        attribute_role="RULE_CONDITION",
                        metric_code=metric,
                        operator=op,
                        value=val,
                        value_unit=unit,
                        value_status="EXTRACTED",
                        source_text=src,
                        confidence=spec["confidence"],
                        extra={},
                    )
                    session.add(attr)
            session.flush()
            print("Seeded extracted penalty rules.")

        # -------------------------------------------------------------
        # 6. CMIR Records (for Table Health & Mappings)
        # -------------------------------------------------------------
        existing_cmir = session.scalars(select(CmirRecord)).all()
        if len(existing_cmir) == 0:
            print("Seeding CMIR records...")
            today = datetime.datetime.now(datetime.UTC)
            cmir_specs = [
                # Healthy records (created Apr - Sep 2026, valid within 180 days, all fields)
                (
                    "EDI",
                    "Walmart Inc",
                    "Pedigree Adult Complete 20lb",
                    "PED-ADULT-20",
                    "Pedigree",
                    "Plant 1001",
                    "GRD-1001",
                    "WMT-PED-20",
                    today - datetime.timedelta(days=150),
                ),
                (
                    "EDI",
                    "Walmart Inc",
                    "Whiskas Poultry Selections 12pk",
                    "WHISK-POUL-12",
                    "Whiskas",
                    "Plant 1002",
                    "GRD-1002",
                    "WMT-WHK-12",
                    today - datetime.timedelta(days=120),
                ),
                (
                    "EDI",
                    "Amazon.com Services",
                    "Sheba Cuts in Gravy Salmon 24pk",
                    "SHEB-SALM-24",
                    "Sheba",
                    "Plant 1001",
                    "GRD-2001",
                    "AMZ-SHB-24",
                    today - datetime.timedelta(days=90),
                ),
                (
                    "EDI",
                    "Amazon.com Services",
                    "Iams Minichunks Small Breed 15lb",
                    "IAMS-SML-15",
                    "Iams",
                    "Plant 1003",
                    "GRD-2002",
                    "AMZ-IAM-15",
                    today - datetime.timedelta(days=60),
                ),
                (
                    "PORTAL",
                    "Target Corporation",
                    "Temptations Classic Cat Treats 16oz",
                    "TEMP-TREAT-16",
                    "Temptations",
                    "Plant 1002",
                    "GRD-3001",
                    "TGT-TMP-16",
                    today - datetime.timedelta(days=45),
                ),
                (
                    "PORTAL",
                    "Target Corporation",
                    "Greenies Dental Treats Large 36ct",
                    "GRN-DENT-36",
                    "Greenies",
                    "Plant 1001",
                    "GRD-3002",
                    "TGT-GRN-36",
                    today - datetime.timedelta(days=30),
                ),
                (
                    "EMAIL",
                    "Costco Wholesale",
                    "Nutro Ultra Adult Dry Dog Food 30lb",
                    "NUT-ULTRA-30",
                    "Nutro",
                    "Plant 1003",
                    "GRD-4001",
                    "CST-NUT-30",
                    today - datetime.timedelta(days=20),
                ),
                (
                    "EMAIL",
                    "Costco Wholesale",
                    "Pedigree DentaStix Large Dog 40ct",
                    "PED-STIX-40",
                    "Pedigree",
                    "Plant 1001",
                    "GRD-4002",
                    "CST-PED-40",
                    today - datetime.timedelta(days=10),
                ),
                (
                    "EDI",
                    "Chewy.com",
                    "Cesar Classic Loaf in Sauce 24pk",
                    "CSR-LOAF-24",
                    "Cesar",
                    "Plant 1002",
                    "GRD-5001",
                    "CHW-CSR-24",
                    today - datetime.timedelta(days=5),
                ),
                (
                    "EDI",
                    "Kroger Company",
                    "Sheba Perfect Portions Turkey 12pk",
                    "SHEB-TURK-12",
                    "Sheba",
                    "Plant 1001",
                    "GRD-6001",
                    "KRG-SHB-12",
                    today - datetime.timedelta(days=2),
                ),
                # Unhealthy: missing_required_field (blank target_customer_material_ref)
                (
                    "EMAIL",
                    "Walmart Inc",
                    "Pedigree Chopped Ground Dinner 13.2oz",
                    "PED-CHOP-13",
                    "Pedigree",
                    "Plant 1001",
                    "GRD-1005",
                    "",
                    today - datetime.timedelta(days=25),
                ),
                # Unhealthy: missing_required_field (blank brand)
                (
                    "PORTAL",
                    "Amazon.com Services",
                    "Crave Grain Free High Protein Beef 4lb",
                    "CRV-BEEF-4",
                    "",
                    "Plant 1002",
                    "GRD-2005",
                    "AMZ-CRV-04",
                    today - datetime.timedelta(days=35),
                ),
                # Unhealthy: stale_validation (> 180 days, e.g. 240 days ago)
                (
                    "EDI",
                    "Target Corporation",
                    "Iams Proactive Health Mature Dog 15lb",
                    "IAMS-MAT-15",
                    "Iams",
                    "Plant 1003",
                    "GRD-3008",
                    "TGT-IAM-M15",
                    today - datetime.timedelta(days=240),
                ),
                # Unhealthy: stale_validation (> 180 days, e.g. 210 days ago)
                (
                    "EDI",
                    "Costco Wholesale",
                    "Greenies Teenie Dog Dental Treats 96ct",
                    "GRN-TEEN-96",
                    "Greenies",
                    "Plant 1001",
                    "GRD-4009",
                    "CST-GRN-T96",
                    today - datetime.timedelta(days=210),
                ),
                # Unhealthy: duplicate_row (same customer + material as record #1)
                (
                    "EMAIL",
                    "Walmart Inc",
                    "Pedigree Adult Complete 20lb",
                    "PED-ADULT-20",
                    "Pedigree",
                    "Plant 1001",
                    "GRD-1001-B",
                    "WMT-PED-20-ALT",
                    today - datetime.timedelta(days=15),
                ),
            ]

            for sender_type, cust, mat, existing_ref, brand, site, grd, target_ref, v_from in cmir_specs:
                rec = CmirRecord(
                    id=generate_uuid7(),
                    sender_type=sender_type,
                    customer_identity=cust,
                    customer_identity_key=cust.strip().lower(),
                    material_identity=mat,
                    intent_phrase="Add or update SKU customer material reference",
                    existing_cmir_ref=existing_ref,
                    brand=brand,
                    site=site,
                    target_grd_code=grd,
                    target_customer_material_ref=target_ref,
                    target_customer_material_ref_key=target_ref.strip().lower() if target_ref else "",
                    effective_date=v_from.date(),
                    reason="Catalog synchronization",
                    is_current=True,
                    valid_from=v_from,
                    created_at=v_from,
                    updated_at=v_from,
                )
                session.add(rec)
            session.flush()
            print("Seeded CMIR records.")

        # -------------------------------------------------------------
        # 7. Workflow Threads & Human Actions (HITL Queue / Actions Needed)
        # -------------------------------------------------------------
        existing_threads = session.scalars(select(WorkflowThread)).all()
        if len(existing_threads) == 0:
            print("Seeding workflow threads and human actions...")
            thread_specs: list[dict[str, Any]] = [
                {
                    "stage": "AWAITING_MISSING_FIELDS",
                    "status": "ACTIVE",
                    "node": "human_review",
                    "meta": {
                        "batch_id": "batch-cmir-001",
                        "sender": "vendor-ops@walmart.com",
                        "subject": "URGENT: New Pedigree 20lb SKU Onboarding for Week 39",
                        "customer_identity": "Walmart Inc",
                        "po_number": "WMT-PO-882109",
                        "po_line_number": 1,
                        "retailer_name": "Walmart",
                    },
                    "action": {
                        "interrupt_type": "missing_mandatory_fields",
                        "action_type": "decision",
                        "status": "PENDING",
                        "req": {"missing_fields": ["material_identity", "target_grd_code"]},
                    },
                },
                {
                    "stage": "AWAITING_APPROVAL",
                    "status": "ACTIVE",
                    "node": "human_approval",
                    "meta": {
                        "batch_id": "batch-cmir-002",
                        "sender": "edi-catalog@amazon.com",
                        "subject": "CMIR Target Ref Update Request: Whiskas Pouch 12pk",
                        "customer_identity": "Amazon.com Services",
                        "po_number": "AMZ-PO-449102",
                        "po_line_number": 2,
                        "retailer_name": "Amazon",
                    },
                    "action": {
                        "interrupt_type": "approval_required",
                        "action_type": "decision",
                        "status": "PENDING",
                        "req": {
                            "diff": {
                                "target_customer_material_ref": {"from": "AMZ-W-001", "to": "AMZ-W-002"},
                                "target_grd_code": {"from": "GRD-2001", "to": "GRD-2002"},
                            }
                        },
                    },
                },
                {
                    "stage": "AWAITING_MANUAL_CMIR_ENTRY",
                    "status": "ACTIVE",
                    "node": "cmir_lookup",
                    "meta": {
                        "batch_id": "batch-po-001",
                        "po_number": "PO-991204",
                        "po_line_number": 1,
                        "retailer_material_code": "WM-PET-5501",
                        "retailer_name": "Walmart",
                        "order_quantity": 2400,
                        "customer_identity": "Walmart Inc",
                        "material_identity": "Pedigree Dog Treats 16oz",
                    },
                    "action": {
                        "interrupt_type": "manual_cmir_entry_required",
                        "action_type": "manual_entry",
                        "status": "PENDING",
                        "req": {"reason": "Unrecognized customer material code WM-PET-5501"},
                    },
                },
                {
                    "stage": "AWAITING_QTY_MISMATCH_DECISION",
                    "status": "ACTIVE",
                    "node": "inventory_check",
                    "meta": {
                        "batch_id": "batch-po-002",
                        "po_number": "PO-991310",
                        "po_line_number": 3,
                        "retailer_material_code": "AMZ-CAT-110",
                        "retailer_name": "Amazon",
                        "order_quantity": 1200,
                        "customer_identity": "Amazon.com Services",
                        "material_identity": "Whiskas Dry Cat Food 10lb",
                    },
                    "action": {
                        "interrupt_type": "qty_mismatch",
                        "action_type": "decision",
                        "status": "PENDING",
                        "req": {
                            "available_quantity": 1080,
                            "order_quantity": 1200,
                            "shortfall": 120,
                            "candidate_sap_code": "1000301",
                            "plant": "Plant 1002",
                        },
                    },
                },
                {
                    "stage": "COMPLETED_APPROVED",
                    "status": "COMPLETED",
                    "node": "finalize",
                    "meta": {
                        "batch_id": "batch-cmir-003",
                        "sender": "orders@target.com",
                        "subject": "Target Spring Catalog CMIR Registration",
                        "customer_identity": "Target Corporation",
                        "po_number": "TGT-PO-551299",
                        "po_line_number": 1,
                        "retailer_name": "Target",
                    },
                    "action": {
                        "interrupt_type": "approval_required",
                        "action_type": "decision",
                        "status": "COMPLETED",
                        "req": {"decision": "APPROVE"},
                    },
                },
            ]

            for spec in thread_specs:
                th_id = generate_uuid7()
                th = WorkflowThread(
                    id=th_id,
                    status=spec["status"],
                    stage=spec["stage"],
                    current_node=spec["node"],
                    completed_at=datetime.datetime.now(datetime.UTC)
                    if spec["status"] == "COMPLETED"
                    else None,
                    error=None,
                    metadata_json=spec["meta"],
                )
                session.add(th)
                session.flush()

                # Subject link
                subj = WorkflowThreadSubject(
                    workflow_thread_id=th_id,
                    subject_type=WorkflowThreadSubjectType.EMAIL_EVENT.value
                    if "sender" in spec["meta"]
                    else WorkflowThreadSubjectType.PURCHASE_ORDER_LINE.value,
                    subject_id=generate_uuid7(),
                )
                session.add(subj)

                # Human action
                act_spec = spec["action"]
                act = HumanAction(
                    id=generate_uuid7(),
                    workflow_thread_id=th_id,
                    action_type=act_spec["action_type"],
                    interrupt_type=act_spec["interrupt_type"],
                    request_payload=act_spec["req"],
                    status=act_spec["status"],
                    decision="APPROVE" if act_spec["status"] == "COMPLETED" else None,
                    actor="dashboard-user@mars.com" if act_spec["status"] == "COMPLETED" else None,
                    requested_at=datetime.datetime.now(datetime.UTC),
                    responded_at=datetime.datetime.now(datetime.UTC)
                    if act_spec["status"] == "COMPLETED"
                    else None,
                )
                session.add(act)

            session.flush()
            print("Seeded workflow threads and human actions.")

        session.commit()
        print("\nAll showcase fixtures successfully seeded into mars_e2e_verify!")


if __name__ == "__main__":
    seed_showcase_data()
