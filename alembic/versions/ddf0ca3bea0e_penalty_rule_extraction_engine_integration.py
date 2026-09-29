"""Expand penalty-rule schema for engine integration.

Revision ID: ddf0ca3bea0e
Revises: 1d92b65b8eb4
Create Date: 2026-09-16

This migration is schema-only: expand-only changes that leave every existing
row readable and writable exactly as it was, with no data backfill and no new
`NOT NULL` enforcement performed here. Backfilling existing rows and enforcing
`NOT NULL` where a value must be derived from existing data, creating the large
`purchase_order` index, and dropping `penalty_rule.retailer_id` are all left to
a follow-up contract migration once this one has been deployed.

`penalties.penalty_rule` gains nullable `engine_family`, `penalty_category`,
`metric_code`, `metric_denominator`, `retailer_agreement_id`,
`extracted_rule_id`, `measurement_window_type`/`_length`/`_unit`, and
`rounding_convention`; `is_engine_priceable` (`NOT NULL DEFAULT true`, safe as
a fast, metadata-only add on Postgres 11+ since the default is a non-volatile
constant); nullable `commitment_quantity`/`commitment_value`; nullable FKs to
`retailer_agreement` (`fk_penalty_rule_retailer_agreement_id`) and
`extracted_penalty_rule` (`fk_penalty_rule_extracted_rule_id`); and the
`ck_penalty_rule_violation_type`/`ck_penalty_rule_engine_family` CHECK
constraints. Those two constraints are new here -- `1d92b65b8eb4` did not
create them -- but they are pure schema (no table scan, no data dependency),
so they belong in this expand migration rather than the follow-up.

`penalty_category` and `retailer_agreement_id` are added nullable and stay
that way until the follow-up migration backfills every pre-existing row and
enforces `NOT NULL`: `penalty_category` to a governed sentinel category,
`retailer_agreement_id` via a placeholder `retailer_agreement` row per
distinct pre-existing `retailer_id` that needs one.

`penalties.penalty_projection` gains nullable `skip_reason` (set only for a
rule this run's engine could not price).

`penalties.penalty_rule_tier` gains nullable `tier_application` and
`tier_basis` (also left for the follow-up migration to backfill and enforce
`NOT NULL`), and widens `band_min`/`band_max` from `Numeric(6,4)` to
`Numeric(12,4)`, with `band_max` made nullable.

`penalties.actual_penalty` gains nullable `claim_facts` JSONB.

Deliberately left for the follow-up migration, since each needs either a data
backfill or non-transactional DDL that does not belong in a schema-only
expand migration:

- `NOT NULL` enforcement on `penalty_category` and `retailer_agreement_id`.
- `NOT NULL` enforcement on `penalty_rule_tier.tier_application`/`tier_basis`.
- `ix_purchase_order_retailer_order_date` on `purchase_order (retailer_id,
  order_date)` -- `purchase_order` is large/high-traffic, so it needs
  `CREATE INDEX CONCURRENTLY`, which cannot run inside this migration's
  transaction.
- Dropping `penalty_rule.retailer_id`, until `retailer_agreement_id` has been
  backfilled for every pre-existing row and the application reads/writes
  `retailer_agreement_id` exclusively.

PostgreSQL column order is not intentionally managed or reordered. New
columns are appended by PostgreSQL in the order in which they are added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ddf0ca3bea0e"
down_revision: str | None = "1d92b65b8eb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"

_VIOLATION_TYPE_CHECK = "ck_penalty_rule_violation_type"
_VIOLATION_TYPES = (
    "SHORT_SHIP",
    "FILL_RATE",
    "OTIF_LATE",
    "ASN_LATE",
    "DELIVERY_WINDOW_VIOLATION",
    "DELIVERY_ACCEPTANCE_COST_SHIFT",
    "QUALITY_DEFECT",
    "COVER_PURCHASE",
    "VOLUME_SHORTFALL",
    "OVERAGE_CHARGEBACK",
    "OVERAGE_NONPAYMENT",
    "STORAGE_DURATION",
    "LIABILITY_CAP",
    "FINANCIAL_ADJUSTMENT",
)

_ENGINE_FAMILY_CHECK = "ck_penalty_rule_engine_family"
_ENGINE_FAMILIES = (
    "SHORTAGE",
    "DELAY",
    "VOLUME_COMMITMENT",
    "QUALITY",
    "COVER_PURCHASE",
    "FINANCIAL",
    "NON_MONETARY",
    "LIABILITY_CAP",
    "OVERAGE_CHARGEBACK",
    "OVERAGE_NONPAYMENT",
    "STORAGE_DURATION_FEE",
    "UNPRICEABLE",
)


def _resolve_schema() -> str | None:
    """Use the penalties schema on PostgreSQL and no schema on SQLite tests."""
    return PENALTIES if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # penalty_rule
    # ------------------------------------------------------------------

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.add_column(sa.Column("engine_family", sa.String(length=30), nullable=True))

        # Nullable for now. The follow-up migration backfills existing rows and
        # enforces NOT NULL.
        batch_op.add_column(sa.Column("penalty_category", sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column("metric_code", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("metric_denominator", sa.String(length=30), nullable=True))

        # Must remain nullable until the follow-up migration has resolved every
        # pre-existing penalty_rule row to a retailer_agreement.
        batch_op.add_column(sa.Column("retailer_agreement_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("extracted_rule_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("measurement_window_type", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("measurement_window_length", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("measurement_window_unit", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("rounding_convention", sa.String(length=30), nullable=True))

        # Existing rows safely receive the constant default on PostgreSQL 11+
        # as a fast, metadata-only operation.
        batch_op.add_column(
            sa.Column("is_engine_priceable", sa.Boolean(), nullable=False, server_default=sa.text("true"))
        )
        batch_op.add_column(
            sa.Column("commitment_quantity", sa.Numeric(precision=14, scale=3), nullable=True)
        )
        batch_op.add_column(sa.Column("commitment_value", sa.Numeric(precision=14, scale=2), nullable=True))

        batch_op.create_foreign_key(
            "fk_penalty_rule_retailer_agreement_id",
            "retailer_agreement",
            ["retailer_agreement_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_penalty_rule_extracted_rule_id",
            "extracted_penalty_rule",
            ["extracted_rule_id"],
            ["id"],
            referent_schema=_resolve_schema(),
        )

        batch_op.create_check_constraint(
            _VIOLATION_TYPE_CHECK,
            "violation_type IN (" + ", ".join(f"'{v}'" for v in _VIOLATION_TYPES) + ")",
        )
        batch_op.create_check_constraint(
            _ENGINE_FAMILY_CHECK,
            "engine_family IS NULL OR engine_family IN ("
            + ", ".join(f"'{v}'" for v in _ENGINE_FAMILIES)
            + ")",
        )

    # ------------------------------------------------------------------
    # penalty_projection
    # ------------------------------------------------------------------

    with op.batch_alter_table("penalty_projection", schema=_resolve_schema()) as batch_op:
        batch_op.add_column(sa.Column("skip_reason", sa.String(length=30), nullable=True))

    # ------------------------------------------------------------------
    # penalty_rule_tier
    # ------------------------------------------------------------------

    with op.batch_alter_table("penalty_rule_tier", schema=_resolve_schema()) as batch_op:
        # Nullable for now. The follow-up migration backfills these and
        # subsequently enforces NOT NULL.
        batch_op.add_column(sa.Column("tier_application", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("tier_basis", sa.String(length=30), nullable=True))
        batch_op.alter_column(
            "band_min",
            existing_type=sa.Numeric(precision=6, scale=4),
            type_=sa.Numeric(precision=12, scale=4),
        )
        batch_op.alter_column(
            "band_max",
            existing_type=sa.Numeric(precision=6, scale=4),
            type_=sa.Numeric(precision=12, scale=4),
            nullable=True,
        )

    # ------------------------------------------------------------------
    # actual_penalty
    # ------------------------------------------------------------------

    with op.batch_alter_table("actual_penalty", schema=_resolve_schema()) as batch_op:
        batch_op.add_column(
            sa.Column(
                "claim_facts",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=True,
            )
        )

    # ------------------------------------------------------------------
    # purchase_order index
    #
    # Not created here: purchase_order is large/high-traffic. The follow-up
    # migration creates it CONCURRENTLY on PostgreSQL.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # retailer_id removal
    #
    # Deferred until retailer_agreement_id has been backfilled for every
    # pre-existing row and the application reads/writes it exclusively.
    # ------------------------------------------------------------------


def downgrade() -> None:
    # This migration is an EXPAND migration. Its downgrade removes only the
    # structures introduced here. Data created by a later backfill migration
    # must not be deleted here.

    with op.batch_alter_table("actual_penalty", schema=_resolve_schema()) as batch_op:
        batch_op.drop_column("claim_facts")

    with op.batch_alter_table("penalty_rule_tier", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column(
            "band_max",
            existing_type=sa.Numeric(precision=12, scale=4),
            type_=sa.Numeric(precision=6, scale=4),
            nullable=False,
        )
        batch_op.alter_column(
            "band_min",
            existing_type=sa.Numeric(precision=12, scale=4),
            type_=sa.Numeric(precision=6, scale=4),
        )
        batch_op.drop_column("tier_basis")
        batch_op.drop_column("tier_application")

    with op.batch_alter_table("penalty_projection", schema=_resolve_schema()) as batch_op:
        batch_op.drop_column("skip_reason")

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.drop_constraint(_ENGINE_FAMILY_CHECK, type_="check")
        batch_op.drop_constraint(_VIOLATION_TYPE_CHECK, type_="check")
        batch_op.drop_constraint(
            "fk_penalty_rule_extracted_rule_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_penalty_rule_retailer_agreement_id",
            type_="foreignkey",
        )
        batch_op.drop_column("commitment_value")
        batch_op.drop_column("commitment_quantity")
        batch_op.drop_column("is_engine_priceable")
        batch_op.drop_column("rounding_convention")
        batch_op.drop_column("measurement_window_unit")
        batch_op.drop_column("measurement_window_length")
        batch_op.drop_column("measurement_window_type")
        batch_op.drop_column("extracted_rule_id")
        batch_op.drop_column("retailer_agreement_id")
        batch_op.drop_column("metric_denominator")
        batch_op.drop_column("metric_code")
        batch_op.drop_column("penalty_category")
        batch_op.drop_column("engine_family")
