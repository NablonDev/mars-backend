"""Penalties schema.

Revision ID: 4b41f6bcb2f3
Revises: 374aa902b053
Create Date: 2026-08-29

Fourth of five revisions (see 0824321a02a4's docstring for the overall
squash rationale). Creates every `penalties`-schema table -- the full
`fine`/`fines` -> `penalty`/`penalties` domain rename, plus the merge of
`agent`+`prompt_version` into `process.agent` (collapsing
`projection_summary`'s/`mitigation_summary`'s composite FK into a
single-column `agent_id`), and the merge of `projection_summary`+
`mitigation_summary` into one `penalty_summary` table with a
`summary_type` discriminator.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b41f6bcb2f3"
down_revision: str | None = "374aa902b053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"
PROCESS = "process"


def upgrade() -> None:
    op.create_table(
        "penalty_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_code", sa.String(length=20), nullable=False),
        sa.Column("retailer_id", sa.Uuid(), nullable=False),
        sa.Column("violation_type", sa.String(length=30), nullable=False),
        sa.Column("threshold_pct", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("calc_type", sa.String(length=20), nullable=False),
        sa.Column("rate", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("cap_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("grace_period_days", sa.Integer(), nullable=False),
        sa.Column("effective_start_date", sa.Date(), nullable=False),
        sa.Column("effective_end_date", sa.Date(), nullable=True),
        sa.Column("source_doc_reference", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["retailer_id"], ["retailer.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_penalty_rule_rule_code"), "penalty_rule", ["rule_code"], unique=True, schema=PENALTIES
    )

    op.create_table(
        "penalty_rule_tier",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("tier_code", sa.String(length=50), nullable=False),
        sa.Column("band_min", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("band_max", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("rate", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], [f"{PENALTIES}.penalty_rule.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "tier_code", name="uq_penalty_rule_tier_rule_code"),
        schema=PENALTIES,
    )

    op.create_table(
        "penalty_job_run_context",
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("projection_date", sa.Date(), nullable=False),
        sa.Column("stacking_mode_override", sa.String(length=30), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_run_id"], [f"{PROCESS}.job_run.id"]),
        sa.PrimaryKeyConstraint("job_run_id"),
        schema=PENALTIES,
    )

    op.create_table(
        "penalty_job_item_context",
        sa.Column("job_item_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("projection_date", sa.Date(), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("stacking_mode_override", sa.String(length=30), nullable=True),
        sa.Column("force_regenerate_summary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_item_id"], [f"{PROCESS}.job_item.id"]),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("job_item_id"),
        schema=PENALTIES,
    )

    op.create_table(
        "mitigation_input",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("shortage_cause", sa.String(length=50), nullable=False),
        sa.Column("shortage_cause_confirmed", sa.Boolean(), nullable=False),
        sa.Column("capacity_boost_cost_per_unit", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("capacity_boost_max_units_per_day", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("capacity_boost_data_confirmed", sa.Boolean(), nullable=False),
        sa.Column("express_carrier_cost", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("express_carrier_transit_days", sa.Integer(), nullable=True),
        sa.Column("express_carrier_data_confirmed", sa.Boolean(), nullable=False),
        sa.Column("split_shipment_handling_cost", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_mitigation_input_purchase_order_id"),
        "mitigation_input",
        ["purchase_order_id"],
        unique=True,
        schema=PENALTIES,
    )

    op.create_table(
        "mitigation_option",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("projection_date", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=True),
        sa.Column("projected_penalty_after", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("action_cost", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("net_saving", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("risk_level", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=30), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_order_id", "projection_date", "action", name="uq_mitigation_option_po_date_action"
        ),
        schema=PENALTIES,
    )

    op.create_table(
        "penalty_summary",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("summary_type", sa.String(length=30), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("source_as_of_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "summary_type IN ('PROJECTION', 'MITIGATION')", name="ck_penalty_summary_summary_type"
        ),
        sa.ForeignKeyConstraint(["agent_id"], [f"{PROCESS}.agent.id"]),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_order_id", "summary_type", "as_of_date", name="uq_penalty_summary_po_type_date"
        ),
        schema=PENALTIES,
    )

    op.create_table(
        "penalty_projection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("projection_date", sa.Date(), nullable=False),
        sa.Column("violation_type", sa.String(length=50), nullable=False),
        sa.Column("failure_probability", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("penalty_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("expected_penalty_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("days_to_delivery", sa.Integer(), nullable=False),
        sa.Column("projection_status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.ForeignKeyConstraint(["rule_id"], [f"{PENALTIES}.penalty_rule.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_order_id", "rule_id", "projection_date", name="uq_penalty_projection_po_rule_date"
        ),
        schema=PENALTIES,
    )

    op.create_table(
        "actual_penalty",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actual_penalty_number", sa.String(length=50), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("violation_type", sa.String(length=50), nullable=False),
        sa.Column("actual_penalty_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("invoice_or_deduction_date", sa.Date(), nullable=False),
        sa.Column("dispute_status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_actual_penalty_actual_penalty_number"),
        "actual_penalty",
        ["actual_penalty_number"],
        unique=True,
        schema=PENALTIES,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_actual_penalty_actual_penalty_number"), table_name="actual_penalty", schema=PENALTIES
    )
    op.drop_table("actual_penalty", schema=PENALTIES)
    op.drop_table("penalty_projection", schema=PENALTIES)
    op.drop_table("penalty_summary", schema=PENALTIES)
    op.drop_table("mitigation_option", schema=PENALTIES)
    op.drop_index(
        op.f("ix_mitigation_input_purchase_order_id"), table_name="mitigation_input", schema=PENALTIES
    )
    op.drop_table("mitigation_input", schema=PENALTIES)
    op.drop_table("penalty_job_item_context", schema=PENALTIES)
    op.drop_table("penalty_job_run_context", schema=PENALTIES)
    op.drop_table("penalty_rule_tier", schema=PENALTIES)
    op.drop_index(op.f("ix_penalty_rule_rule_code"), table_name="penalty_rule", schema=PENALTIES)
    op.drop_table("penalty_rule", schema=PENALTIES)
