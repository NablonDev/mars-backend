"""Dedicated standalone seed script for Retailer Agreements.

Seeds institutional-grade retailer vendor agreements (Target MVA, Kroger Compliance)
directly from self-contained Python fixtures without requiring filesystem I/O, pathlib,
or external data folders.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import generate_uuid7
from app.db.session import Database
from app.models.common import Retailer, RetailerAgreement
from app.utils.hashing import content_sha256
from scripts.seed.contract_fixtures import (
    COSTCO_CONTRACT_TEXT,
    KROGER_CONTRACT_TEXT,
    TARGET_CONTRACT_TEXT,
)

logger = logging.getLogger(__name__)


AGREEMENT_SPECS: list[dict[str, Any]] = [
    {
        "retailer_code": "RET-TARGET",
        "retailer_name": "Target Corporation",
        "priority_tier": "TIER_1",
        "stacking_mode": "SUM",
        "source_system": "SAP",
        "contract_code": "TGT-MVA-2024",
        "title": "Target Master Vendor Merchandise and Compliance Agreement",
        "markdown_text": TARGET_CONTRACT_TEXT,
        "effective_date": datetime.date(2024, 10, 15),
        "expiration_date": datetime.date(2027, 10, 14),
        "dispute_window_days": 30,
    },
    {
        "retailer_code": "RET-KROGER",
        "retailer_name": "The Kroger Co.",
        "priority_tier": "TIER_1",
        "stacking_mode": "SUM",
        "source_system": "SAP",
        "contract_code": "KROGER-MLVCA-2024",
        "title": "Kroger Master Logistics and Vendor Compliance Agreement",
        "markdown_text": KROGER_CONTRACT_TEXT,
        "effective_date": datetime.date(2024, 11, 1),
        "expiration_date": datetime.date(2027, 10, 31),
        "dispute_window_days": 45,
    },
    {
        "retailer_code": "RET-COSTCO",
        "retailer_name": "Costco Wholesale",
        "priority_tier": "TIER_1",
        "stacking_mode": "SUM",
        "source_system": "SAP",
        "contract_code": "COSTCO-MNA-2026",
        "title": "Costco Wholesale Master Vendor Agreement 2026",
        "markdown_text": COSTCO_CONTRACT_TEXT,
        "effective_date": datetime.date(2024, 11, 1),
        "expiration_date": datetime.date(2027, 10, 31),
        "dispute_window_days": 30,
    },
]


def seed_agreements() -> None:
    """Seed or update retailer agreements idempotently."""
    settings = get_settings()
    db = Database(settings.database.url)
    with db.session() as session:
        for spec in AGREEMENT_SPECS:
            # 1. Ensure Retailer exists
            retailer = session.scalars(
                select(Retailer).where(Retailer.retailer_code == spec["retailer_code"])
            ).first()
            if retailer is None:
                retailer = Retailer(
                    id=generate_uuid7(),
                    retailer_code=spec["retailer_code"],
                    retailer_name=spec["retailer_name"],
                    priority_tier=spec["priority_tier"],
                    stacking_mode=spec["stacking_mode"],
                    source_system=spec["source_system"],
                )
                session.add(retailer)
                session.flush()
                print(f"Added Retailer: {retailer.retailer_name} ({retailer.retailer_code}) -> {retailer.id}")
            else:
                print(
                    f"Existing Retailer: {retailer.retailer_name} ({retailer.retailer_code}) -> {retailer.id}"
                )

            # 2. Ensure Agreement exists or update text
            sha = content_sha256(spec["markdown_text"])
            agreement = session.scalars(
                select(RetailerAgreement).where(
                    (RetailerAgreement.contract_code == spec["contract_code"])
                    | (RetailerAgreement.retailer_id == retailer.id)
                )
            ).first()

            if agreement is None:
                agreement = RetailerAgreement(
                    id=generate_uuid7(),
                    retailer_id=retailer.id,
                    contract_code=spec["contract_code"],
                    title=spec["title"],
                    document_sha256=sha,
                    markdown_text=spec["markdown_text"],
                    effective_date=spec["effective_date"],
                    expiration_date=spec["expiration_date"],
                    dispute_window_days=spec["dispute_window_days"],
                )
                session.add(agreement)
                session.flush()
                print(f"Created Agreement: {agreement.title} [{agreement.contract_code}] -> {agreement.id}")
            else:
                # Update text and sha256 to ensure current fixture is stored
                agreement.markdown_text = spec["markdown_text"]
                agreement.document_sha256 = sha
                agreement.title = spec["title"]
                agreement.contract_code = spec["contract_code"]
                agreement.effective_date = spec["effective_date"]
                agreement.expiration_date = spec["expiration_date"]
                agreement.dispute_window_days = spec["dispute_window_days"]
                session.flush()
                print(f"Updated Agreement: {agreement.title} [{agreement.contract_code}] -> {agreement.id}")

        session.commit()
        print("Successfully seeded all retailer agreements.")


if __name__ == "__main__":
    seed_agreements()
