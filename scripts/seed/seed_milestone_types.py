"""Seed `milestone_type` with the fulfillment timeline's reference milestones.

Standalone runnable: `python -m scripts.seed.seed_milestone_types`. Writes
against the `MilestoneType` ORM model directly, using its own `Session` built
from `Settings().database.url`.

Idempotent: upserts keyed on `code`, so running this twice leaves exactly 12
rows, values refreshed from `MILESTONE_TYPE_SEEDS` rather than duplicated.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import Database
from app.models.common import MilestoneType


class _MilestoneTypeSeed:
    __slots__ = (
        "code",
        "default_duration_days",
        "depends_on",
        "freight_term_scope",
        "is_measurement_point",
        "name",
        "owner_team",
        "sap_source_reference",
        "sequence_no",
    )

    def __init__(
        self,
        *,
        code: str,
        name: str,
        sequence_no: int,
        depends_on: list[str],
        default_duration_days: float | None,
        freight_term_scope: str,
        is_measurement_point: bool,
        owner_team: str,
        sap_source_reference: str,
    ) -> None:
        self.code = code
        self.name = name
        self.sequence_no = sequence_no
        self.depends_on = depends_on
        self.default_duration_days = default_duration_days
        self.freight_term_scope = freight_term_scope
        self.is_measurement_point = is_measurement_point
        self.owner_team = owner_team
        self.sap_source_reference = sap_source_reference


MILESTONE_TYPE_SEEDS: list[_MilestoneTypeSeed] = [
    _MilestoneTypeSeed(
        code="ORDER_RECEIVED",
        name="Order Received",
        sequence_no=10,
        depends_on=[],
        default_duration_days=0,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Customer Service",
        sap_source_reference="EDI 850 / VBAK-ERDAT",
    ),
    _MilestoneTypeSeed(
        code="ORDER_CONFIRMED",
        name="Order Confirmed",
        sequence_no=20,
        depends_on=["ORDER_RECEIVED"],
        default_duration_days=1,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Customer Service",
        sap_source_reference="EDI 855",
    ),
    _MilestoneTypeSeed(
        code="MATERIAL_AVAILABLE",
        name="Material Available",
        sequence_no=30,
        depends_on=["ORDER_CONFIRMED"],
        default_duration_days=0,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Supply Planning",
        sap_source_reference="VBEP-MBDAT",
    ),
    _MilestoneTypeSeed(
        code="TENDER_ACCEPTED",
        name="Tender Accepted",
        sequence_no=40,
        depends_on=["ORDER_CONFIRMED"],
        default_duration_days=1,
        freight_term_scope="PREPAID",
        is_measurement_point=False,
        owner_team="Transportation",
        sap_source_reference="VBEP-TDDAT / EDI 990",
    ),
    _MilestoneTypeSeed(
        code="DELIVERY_CREATED",
        name="Delivery Created",
        sequence_no=50,
        depends_on=["MATERIAL_AVAILABLE"],
        default_duration_days=0,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Customer Service",
        sap_source_reference="LIKP-ERDAT",
    ),
    _MilestoneTypeSeed(
        code="PICKED",
        name="Picked",
        sequence_no=60,
        depends_on=["DELIVERY_CREATED"],
        default_duration_days=1,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Warehouse Ops",
        sap_source_reference="LIKP-KODAT",
    ),
    _MilestoneTypeSeed(
        code="READY_FOR_PICKUP",
        name="Ready For Pickup",
        sequence_no=65,
        depends_on=["PICKED"],
        default_duration_days=0,
        freight_term_scope="COLLECT",
        is_measurement_point=True,
        owner_team="Warehouse Ops",
        sap_source_reference="LIKP-LDDAT",
    ),
    _MilestoneTypeSeed(
        code="LOADED",
        name="Loaded",
        sequence_no=70,
        depends_on=["PICKED", "TENDER_ACCEPTED", "READY_FOR_PICKUP"],
        default_duration_days=0,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Warehouse Ops",
        sap_source_reference="LIKP-LDDAT",
    ),
    _MilestoneTypeSeed(
        code="APPOINTMENT_CONFIRMED",
        name="Appointment Confirmed",
        sequence_no=75,
        depends_on=["TENDER_ACCEPTED"],
        default_duration_days=1,
        freight_term_scope="PREPAID",
        is_measurement_point=False,
        owner_team="Transportation",
        sap_source_reference="Retailer appointment portal",
    ),
    _MilestoneTypeSeed(
        code="GOODS_ISSUED",
        name="Goods Issued",
        sequence_no=80,
        depends_on=["LOADED"],
        default_duration_days=0,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Warehouse Ops",
        sap_source_reference="LIKP-WADAT_IST / mvt 601",
    ),
    _MilestoneTypeSeed(
        code="ASN_SENT",
        name="ASN Sent",
        sequence_no=90,
        depends_on=["GOODS_ISSUED"],
        default_duration_days=0,
        freight_term_scope="ANY",
        is_measurement_point=False,
        owner_team="Customer Service",
        sap_source_reference="EDI 856",
    ),
    _MilestoneTypeSeed(
        code="DELIVERED",
        name="Delivered",
        sequence_no=100,
        depends_on=["GOODS_ISSUED", "APPOINTMENT_CONFIRMED"],
        default_duration_days=None,
        freight_term_scope="PREPAID",
        is_measurement_point=True,
        owner_team="Transportation",
        sap_source_reference="LIKP-LFDAT / EDI 214 D1",
    ),
]


def seed_milestone_types(session: Session) -> None:
    existing_by_code = {row.code: row for row in session.scalars(select(MilestoneType))}

    for seed in MILESTONE_TYPE_SEEDS:
        existing = existing_by_code.get(seed.code)
        if existing is not None:
            existing.name = seed.name
            existing.sequence_no = seed.sequence_no
            existing.depends_on = seed.depends_on
            existing.default_duration_days = seed.default_duration_days
            existing.freight_term_scope = seed.freight_term_scope
            existing.is_measurement_point = seed.is_measurement_point
            existing.owner_team = seed.owner_team
            existing.sap_source_reference = seed.sap_source_reference
            continue
        session.add(
            MilestoneType(
                code=seed.code,
                name=seed.name,
                sequence_no=seed.sequence_no,
                depends_on=seed.depends_on,
                default_duration_days=seed.default_duration_days,
                freight_term_scope=seed.freight_term_scope,
                is_measurement_point=seed.is_measurement_point,
                owner_team=seed.owner_team,
                sap_source_reference=seed.sap_source_reference,
            )
        )
    session.commit()


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # see app/core/config/__init__.py::get_settings
    database = Database(settings.database.url)
    with database.session() as session:
        seed_milestone_types(session)
    print(f"Seeded {len(MILESTONE_TYPE_SEEDS)} milestone_type rows.")


if __name__ == "__main__":
    main()
