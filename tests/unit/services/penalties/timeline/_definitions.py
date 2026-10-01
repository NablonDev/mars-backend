"""Shared milestone-definition fixture for the timeline test suite.

Copies the 12 milestone rows from
`scripts/seed/seed_milestone_types.py::MILESTONE_TYPE_SEEDS` rather than
importing that script, keeping these tests free of any SQLAlchemy dependency.
"""

from app.services.penalties.timeline.types import MilestoneDefinition


def definitions() -> tuple[MilestoneDefinition, ...]:
    return (
        MilestoneDefinition(
            code="ORDER_RECEIVED",
            sequence_no=10,
            depends_on=(),
            default_duration_days=0,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Customer Service",
        ),
        MilestoneDefinition(
            code="ORDER_CONFIRMED",
            sequence_no=20,
            depends_on=("ORDER_RECEIVED",),
            default_duration_days=1,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Customer Service",
        ),
        MilestoneDefinition(
            code="MATERIAL_AVAILABLE",
            sequence_no=30,
            depends_on=("ORDER_CONFIRMED",),
            default_duration_days=0,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Supply Planning",
        ),
        MilestoneDefinition(
            code="TENDER_ACCEPTED",
            sequence_no=40,
            depends_on=("ORDER_CONFIRMED",),
            default_duration_days=1,
            freight_term_scope="PREPAID",
            is_measurement_point=False,
            owner_team="Transportation",
        ),
        MilestoneDefinition(
            code="DELIVERY_CREATED",
            sequence_no=50,
            depends_on=("MATERIAL_AVAILABLE",),
            default_duration_days=0,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Customer Service",
        ),
        MilestoneDefinition(
            code="PICKED",
            sequence_no=60,
            depends_on=("DELIVERY_CREATED",),
            default_duration_days=1,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Warehouse Ops",
        ),
        MilestoneDefinition(
            code="READY_FOR_PICKUP",
            sequence_no=65,
            depends_on=("PICKED",),
            default_duration_days=0,
            freight_term_scope="COLLECT",
            is_measurement_point=True,
            owner_team="Warehouse Ops",
        ),
        MilestoneDefinition(
            code="LOADED",
            sequence_no=70,
            depends_on=("PICKED", "TENDER_ACCEPTED", "READY_FOR_PICKUP"),
            default_duration_days=0,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Warehouse Ops",
        ),
        MilestoneDefinition(
            code="APPOINTMENT_CONFIRMED",
            sequence_no=75,
            depends_on=("TENDER_ACCEPTED",),
            default_duration_days=1,
            freight_term_scope="PREPAID",
            is_measurement_point=False,
            owner_team="Transportation",
        ),
        MilestoneDefinition(
            code="GOODS_ISSUED",
            sequence_no=80,
            depends_on=("LOADED",),
            default_duration_days=0,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Warehouse Ops",
        ),
        MilestoneDefinition(
            code="ASN_SENT",
            sequence_no=90,
            depends_on=("GOODS_ISSUED",),
            default_duration_days=0,
            freight_term_scope="ANY",
            is_measurement_point=False,
            owner_team="Customer Service",
        ),
        MilestoneDefinition(
            code="DELIVERED",
            sequence_no=100,
            depends_on=("GOODS_ISSUED", "APPOINTMENT_CONFIRMED"),
            default_duration_days=None,
            freight_term_scope="PREPAID",
            is_measurement_point=True,
            owner_team="Transportation",
        ),
    )
