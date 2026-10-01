"""Fulfillment timeline, risk projection, mitigation, QA lot, and timeline alert schema.

Revision ID: 6e9199719a2d
Revises: 763b4eb09a7d
Create Date: 2026-09-25

Adds the fulfillment timeline data model used by the penalty projection engine:
milestone reference data, fulfillment plans/lines/milestones, and the
append-only fulfillment event trace.

Also adds fulfillment risk projections, mitigation options, QA lot supply input,
supporting nullable fields on purchase_order, material_master, and
fulfillment_plan_line, and ops-facing timeline alerts that track the lifecycle
of fulfillment-plan/risk-type alerts across engine runs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6e9199719a2d"
down_revision: str | None = "763b4eb09a7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # Fulfillment timeline schema
    # -------------------------------------------------------------------------

    op.create_table(
        "milestone_type",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column(
            "depends_on",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("default_duration_days", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("freight_term_scope", sa.String(length=20), nullable=False),
        sa.Column("is_measurement_point", sa.Boolean(), nullable=False),
        sa.Column("owner_team", sa.String(length=100), nullable=True),
        sa.Column("sap_source_reference", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sequence_no"),
    )
    op.create_index(op.f("ix_milestone_type_code"), "milestone_type", ["code"], unique=True)

    op.create_table(
        "fulfillment_plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_number", sa.String(length=50), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=True),
        sa.Column("ship_from_warehouse_id", sa.Uuid(), nullable=True),
        sa.Column("carrier_id", sa.Uuid(), nullable=True),
        sa.Column("freight_term", sa.String(length=20), nullable=False),
        sa.Column("planned_transit_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["carrier_id"], ["carrier.id"]),
        sa.ForeignKeyConstraint(["delivery_id"], ["delivery.id"]),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.ForeignKeyConstraint(["ship_from_warehouse_id"], ["warehouse.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_fulfillment_plan_plan_number"), "fulfillment_plan", ["plan_number"], unique=True)
    op.create_index(
        op.f("ix_fulfillment_plan_purchase_order_id"), "fulfillment_plan", ["purchase_order_id"], unique=False
    )

    op.create_table(
        "fulfillment_plan_line",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fulfillment_plan_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=False),
        sa.Column("planned_quantity", sa.Numeric(precision=18, scale=3), nullable=False),
        sa.Column("confirmed_quantity", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["fulfillment_plan_id"], ["fulfillment_plan.id"]),
        sa.ForeignKeyConstraint(["purchase_order_line_id"], ["purchase_order_line.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fulfillment_plan_id", "purchase_order_line_id", name="uq_fulfillment_plan_line_plan_po_line"
        ),
    )

    op.create_table(
        "fulfillment_milestone",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fulfillment_plan_id", sa.Uuid(), nullable=False),
        sa.Column("milestone_type_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_date", sa.Date(), nullable=True),
        sa.Column("planned_date", sa.Date(), nullable=True),
        sa.Column("actual_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source_document_type", sa.String(length=50), nullable=True),
        sa.Column("source_document_number", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["fulfillment_plan_id"], ["fulfillment_plan.id"]),
        sa.ForeignKeyConstraint(["milestone_type_id"], ["milestone_type.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fulfillment_plan_id", "milestone_type_id", name="uq_fulfillment_milestone_plan_type"
        ),
    )

    op.create_table(
        "fulfillment_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=30), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("fulfillment_plan_id", sa.Uuid(), nullable=True),
        sa.Column("milestone_type_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("field_name", sa.String(length=100), nullable=True),
        sa.Column("old_value", sa.String(length=255), nullable=True),
        sa.Column("new_value", sa.String(length=255), nullable=True),
        sa.Column("reason_code", sa.String(length=50), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("source_reference", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["fulfillment_plan_id"], ["fulfillment_plan.id"]),
        sa.ForeignKeyConstraint(["milestone_type_id"], ["milestone_type.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fulfillment_event_subject", "fulfillment_event", ["subject_type", "subject_id"], unique=False
    )
    op.create_index(
        "ix_fulfillment_event_plan_event_at",
        "fulfillment_event",
        ["fulfillment_plan_id", "event_at"],
        unique=False,
    )

    with op.batch_alter_table("purchase_order", schema=None) as batch_op:
        batch_op.add_column(sa.Column("window_start", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("window_end", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("cancel_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("freight_term", sa.String(length=20), nullable=True))

    with op.batch_alter_table("material_master", schema=None) as batch_op:
        batch_op.add_column(sa.Column("standard_cost", sa.Numeric(precision=12, scale=4), nullable=True))

    # -------------------------------------------------------------------------
    # Fulfillment risk / mitigation / QA schema
    # -------------------------------------------------------------------------

    op.create_table(
        "quality_lot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lot_number", sa.String(length=50), nullable=False),
        sa.Column("production_order_id", sa.Uuid(), nullable=True),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=3), nullable=False),
        sa.Column("inspection_start_date", sa.Date(), nullable=False),
        sa.Column("planned_release_date", sa.Date(), nullable=False),
        sa.Column("actual_release_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["material_id"], ["material.id"]),
        sa.ForeignKeyConstraint(["plant_id"], ["plant.id"]),
        sa.ForeignKeyConstraint(["production_order_id"], ["production_order.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_quality_lot_lot_number"), "quality_lot", ["lot_number"], unique=True)

    op.create_table(
        "fulfillment_risk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fulfillment_plan_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("projection_date", sa.Date(), nullable=False),
        sa.Column("risk_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("measured_milestone_code", sa.String(length=50), nullable=False),
        sa.Column("projected_measured_date", sa.Date(), nullable=True),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("days_off", sa.Integer(), nullable=True),
        sa.Column("shortfall_quantity", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("driver_milestone_code", sa.String(length=50), nullable=True),
        sa.Column("driver_reason_code", sa.String(length=50), nullable=True),
        sa.Column("driver_event_id", sa.Uuid(), nullable=True),
        sa.Column("projected_penalty_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency_code", sa.CHAR(length=3), nullable=False),
        sa.Column(
            "priced_rule_ids",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "projected_milestones",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "calculation_detail",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["driver_event_id"], ["fulfillment_event.id"]),
        sa.ForeignKeyConstraint(["fulfillment_plan_id"], ["fulfillment_plan.id"]),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fulfillment_plan_id", "projection_date", "risk_type", name="uq_fulfillment_risk_plan_date_type"
        ),
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_fulfillment_risk_purchase_order_id"),
        "fulfillment_risk",
        ["purchase_order_id"],
        unique=False,
        schema=PENALTIES,
    )

    op.create_table(
        "fulfillment_mitigation_option",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fulfillment_plan_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("projection_date", sa.Date(), nullable=False),
        sa.Column("action_code", sa.String(length=50), nullable=False),
        sa.Column("owner_team", sa.String(length=100), nullable=True),
        sa.Column("feasible", sa.Boolean(), nullable=False),
        sa.Column("infeasible_reason", sa.String(length=100), nullable=True),
        sa.Column("act_by_date", sa.Date(), nullable=True),
        sa.Column("penalty_before", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("penalty_after", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("action_cost", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("net_saving", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("rank_no", sa.Integer(), nullable=True),
        sa.Column(
            "addresses_risk_types",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["fulfillment_plan_id"], ["fulfillment_plan.id"]),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fulfillment_plan_id",
            "projection_date",
            "action_code",
            name="uq_fulfillment_mitigation_option_plan_date_action",
        ),
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_fulfillment_mitigation_option_purchase_order_id"),
        "fulfillment_mitigation_option",
        ["purchase_order_id"],
        unique=False,
        schema=PENALTIES,
    )

    with op.batch_alter_table("fulfillment_plan_line", schema=None) as batch_op:
        batch_op.add_column(sa.Column("shipped_quantity", sa.Numeric(precision=18, scale=3), nullable=True))

    with op.batch_alter_table("material_master", schema=None) as batch_op:
        batch_op.add_column(sa.Column("qa_release_days", sa.Integer(), nullable=True))

    # -------------------------------------------------------------------------
    # Ops lifecycle timeline alerts
    # -------------------------------------------------------------------------

    op.create_table(
        "timeline_alert",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fulfillment_plan_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("risk_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_tracking", sa.Boolean(), nullable=False),
        sa.Column("change_type", sa.String(length=10), nullable=False),
        sa.Column("first_seen_date", sa.Date(), nullable=False),
        sa.Column("last_seen_date", sa.Date(), nullable=False),
        sa.Column("prev_penalty_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("last_penalty_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("prev_days_off", sa.Integer(), nullable=True),
        sa.Column("last_days_off", sa.Integer(), nullable=True),
        sa.Column("prev_shortfall_quantity", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("last_shortfall_quantity", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("assigned_to", sa.String(length=100), nullable=True),
        sa.Column("chosen_action_code", sa.String(length=50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=100), nullable=True),
        sa.Column("action_taken_by", sa.String(length=100), nullable=True),
        sa.Column("closed_by", sa.String(length=100), nullable=True),
        sa.Column("closed_reason", sa.String(length=30), nullable=True),
        sa.Column("actual_penalty_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["fulfillment_plan_id"], ["fulfillment_plan.id"]),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_timeline_alert_fulfillment_plan_id"),
        "timeline_alert",
        ["fulfillment_plan_id"],
        unique=False,
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_timeline_alert_purchase_order_id"),
        "timeline_alert",
        ["purchase_order_id"],
        unique=False,
        schema=PENALTIES,
    )
    op.create_index(
        "ix_timeline_alert_plan_type_tracking",
        "timeline_alert",
        ["fulfillment_plan_id", "risk_type", "is_tracking"],
        unique=False,
        schema=PENALTIES,
    )


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # Ops lifecycle timeline alerts
    # -------------------------------------------------------------------------

    op.drop_index("ix_timeline_alert_plan_type_tracking", table_name="timeline_alert", schema=PENALTIES)
    op.drop_index(op.f("ix_timeline_alert_purchase_order_id"), table_name="timeline_alert", schema=PENALTIES)
    op.drop_index(
        op.f("ix_timeline_alert_fulfillment_plan_id"), table_name="timeline_alert", schema=PENALTIES
    )
    op.drop_table("timeline_alert", schema=PENALTIES)

    # -------------------------------------------------------------------------
    # Fulfillment risk / mitigation / QA schema
    # -------------------------------------------------------------------------

    with op.batch_alter_table("material_master", schema=None) as batch_op:
        batch_op.drop_column("qa_release_days")

    with op.batch_alter_table("fulfillment_plan_line", schema=None) as batch_op:
        batch_op.drop_column("shipped_quantity")

    op.drop_index(
        op.f("ix_fulfillment_mitigation_option_purchase_order_id"),
        table_name="fulfillment_mitigation_option",
        schema=PENALTIES,
    )
    op.drop_table("fulfillment_mitigation_option", schema=PENALTIES)

    op.drop_index(
        op.f("ix_fulfillment_risk_purchase_order_id"), table_name="fulfillment_risk", schema=PENALTIES
    )
    op.drop_table("fulfillment_risk", schema=PENALTIES)

    op.drop_index(op.f("ix_quality_lot_lot_number"), table_name="quality_lot")
    op.drop_table("quality_lot")

    # -------------------------------------------------------------------------
    # Fulfillment timeline schema
    # -------------------------------------------------------------------------

    with op.batch_alter_table("material_master", schema=None) as batch_op:
        batch_op.drop_column("standard_cost")

    with op.batch_alter_table("purchase_order", schema=None) as batch_op:
        batch_op.drop_column("freight_term")
        batch_op.drop_column("cancel_date")
        batch_op.drop_column("window_end")
        batch_op.drop_column("window_start")

    op.drop_index("ix_fulfillment_event_plan_event_at", table_name="fulfillment_event")
    op.drop_index("ix_fulfillment_event_subject", table_name="fulfillment_event")
    op.drop_table("fulfillment_event")
    op.drop_table("fulfillment_milestone")
    op.drop_table("fulfillment_plan_line")
    op.drop_index(op.f("ix_fulfillment_plan_purchase_order_id"), table_name="fulfillment_plan")
    op.drop_index(op.f("ix_fulfillment_plan_plan_number"), table_name="fulfillment_plan")
    op.drop_table("fulfillment_plan")
    op.drop_index(op.f("ix_milestone_type_code"), table_name="milestone_type")
    op.drop_table("milestone_type")
