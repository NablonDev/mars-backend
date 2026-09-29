"""Penalty wiring-gap schema: interception log, dispute response window, actual-penalty
line link, retailer-agreement dispute window.

Revision ID: e834e943318e
Revises: d275022ac6a0
Create Date: 2026-09-17

Four small, additive changes bundled into one chained revision (following the
11ce88f609e0 precedent for bundling more than one change per revision when
they land together, all sourced from mars_common's models as of this revision):

- `penalty_interception_log`: new table, one row per mitigation applied that
  averted or reduced a fine. Backs the previously 100%-hardcoded
  agent-protection analytics panel in mars-bff.
- `penalty_dispute.response_due_date`: nullable date, set once at
  open_dispute() time from either the retailer's currently-effective
  agreement's `dispute_window_days` or DisputeSettings.default_window_days.
- `actual_penalty.purchase_order_line_id`: nullable FK to purchase_order_line,
  so mars-bff's actual-penalties feed can join the correct line instead of the
  hardcoded `line_number = '10'` it currently guesses at. Unbackfilled:
  pre-existing rows keep no line reference.
- `retailer_agreement.dispute_window_days`: nullable integer, contractual
  dispute response window in days, varies per agreement.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e834e943318e"
down_revision: str | None = "d275022ac6a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"


def upgrade() -> None:
    op.create_table(
        "penalty_interception_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("mitigation_option_id", sa.Uuid(), nullable=False),
        sa.Column("violation_type", sa.String(length=50), nullable=False),
        sa.Column("amount_avoided", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.ForeignKeyConstraint(["mitigation_option_id"], [f"{PENALTIES}.mitigation_option.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )

    with op.batch_alter_table("penalty_dispute", schema=_resolve_schema()) as batch_op:
        batch_op.add_column(sa.Column("response_due_date", sa.Date(), nullable=True))

    with op.batch_alter_table("actual_penalty", schema=_resolve_schema()) as batch_op:
        batch_op.add_column(sa.Column("purchase_order_line_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_actual_penalty_purchase_order_line_id_purchase_order_line",
            "purchase_order_line",
            ["purchase_order_line_id"],
            ["id"],
        )

    with op.batch_alter_table("retailer_agreement", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dispute_window_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("retailer_agreement", schema=None) as batch_op:
        batch_op.drop_column("dispute_window_days")

    with op.batch_alter_table("actual_penalty", schema=_resolve_schema()) as batch_op:
        batch_op.drop_constraint(
            "fk_actual_penalty_purchase_order_line_id_purchase_order_line", type_="foreignkey"
        )
        batch_op.drop_column("purchase_order_line_id")

    with op.batch_alter_table("penalty_dispute", schema=_resolve_schema()) as batch_op:
        batch_op.drop_column("response_due_date")

    op.drop_table("penalty_interception_log", schema=PENALTIES)


def _resolve_schema() -> str | None:
    """Postgres has real schemas; SQLite's schema_translate_map collapses
    "penalties" to no schema, and batch_alter_table needs schema=None there
    (same reasoning as 11ce88f609e0's identical helper).
    """
    dialect = op.get_bind().dialect.name
    return PENALTIES if dialect != "sqlite" else None
