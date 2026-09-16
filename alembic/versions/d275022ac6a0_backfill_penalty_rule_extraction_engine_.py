"""Backfill and enforce penalty-rule engine integration schema.

Revision ID: d275022ac6a0
Revises: ddf0ca3bea0e
Create Date: 2026-09-16

This is the contract half of the expand/contract pair started by
`ddf0ca3bea0e`. It backfills every pre-existing row for the columns that
migration added nullable, verifies no `NULL` is left before tightening each
column, then enforces `NOT NULL`; creates the large `purchase_order` index
without blocking writes; and drops `penalty_rule.retailer_id` now that
`retailer_agreement_id` covers the same relationship.

`penalty_rule.penalty_category`: every pre-existing `NULL` row is set to
`'UNSPECIFIED_INTERNAL'`, one of the governed `PENALTY_CATEGORIES`
(`app.services.penalties.rule_extraction.vocabulary`) already used on
`extracted_penalty_rule.penalty_category` to mean "a clause is referenced but
its terms live nowhere in the contract". On `penalty_rule` specifically,
though, this migration's backfill is a pure migration-time sentinel, not a
value the application will ever intentionally write going forward:
`PenaltyRulePublisher._check_admission` (`app/services/penalties/
rule_extraction/publisher.py`) rejects any staged rule whose category maps to
`engine_family = UNPRICEABLE` -- which is exactly what `UNSPECIFIED_INTERNAL`
maps to -- before that rule is ever turned into a `PublishedRule`/
`penalty_rule` row, so the extraction pipeline can never write it here. The
only other write path, `POST /penalties/rules`
(`app/api/v1/penalties/rules.py`), takes `penalty_category` as a plain
required string with no column default and no server-side enum check, so an
operator could still type it in by hand, but nothing in application code
assigns it silently. Accordingly this migration adds no server-side default
for `penalty_category`: every row from here on must supply a real category
explicitly, and any row still reading `UNSPECIFIED_INTERNAL` after this
migration runs predates it and needs its real category set by hand.

`penalty_rule.retailer_agreement_id`: for every pre-existing `penalty_rule`
row with no `retailer_agreement_id`, a placeholder `retailer_agreement` row
is created per distinct `retailer_id` needing one (`contract_code =
'MIGRATION-BACKFILL-' || retailer_id`, `title = 'Backfilled placeholder
agreement'`, a deterministic `document_sha256` from Postgres's built-in
`sha256()`), and those rows are pointed at it. Postgres-only: it relies on
`sha256()` (built into Postgres 11+) and `gen_random_uuid()` (built into
Postgres 13+); the unit test suite's SQLite databases are always fresh (no
pre-existing `penalty_rule` rows), so there is nothing for it to do there.
Every placeholder row must be re-pointed at the correct real agreement by
hand afterward; this migration does not attempt that judgment call.

`penalty_rule_tier.tier_application`/`tier_basis`: pre-existing `NULL` rows
are backfilled to `'CLIFF'` and `'SHORTFALL_PCT'` respectively, matching what
every tiered rule priced by the engine today already assumes.

`ix_purchase_order_retailer_order_date` on `purchase_order (retailer_id,
order_date)` is created with `CREATE INDEX CONCURRENTLY` inside
`op.get_context().autocommit_block()`, since `purchase_order` is large/
high-traffic and `CONCURRENTLY` cannot run inside Alembic's normal migration
transaction. SQLite has no `CONCURRENTLY`, so the SQLite path (used only by
the unit test suite) creates the same index with a plain `CREATE INDEX`.

`penalty_rule.retailer_id` is dropped last, once every row reads
`retailer_agreement_id`: its FK constraint (`penalty_rule_retailer_id_fkey`,
Postgres's default name for the unnamed `ForeignKeyConstraint` `4b41f6bcb2f3`
created) is dropped first on Postgres, then the column itself.
`retailer_agreement` then gains `ix_retailer_agreement_retailer_id`, backing the
join `PenaltyRuleRepository` now uses in `retailer_agreement.retailer_id` in
place of the dropped `penalty_rule.retailer_id` filter -- a plain index, since
`retailer_agreement` is not large/high-traffic the way `purchase_order` is.

`downgrade()` is a best-effort inverse, not a perfect one, per this project's
migration guidance: it restores `retailer_id` (backfilled from
`retailer_agreement.retailer_id` via the same join used going the other
way), drops the `purchase_order` index, and reverts the four `NOT NULL`
constraints this migration added back to nullable. It does not undo the
backfill data itself -- the sentinel `penalty_category` values or the
placeholder `retailer_agreement` rows -- since reversing a schema change
should not delete substantive data other rows may already reference; that is
intentional.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d275022ac6a0"
down_revision: str | None = "ddf0ca3bea0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"

_BACKFILL_PENALTY_CATEGORY = "UNSPECIFIED_INTERNAL"
_BACKFILL_TIER_APPLICATION = "CLIFF"
_BACKFILL_TIER_BASIS = "SHORTFALL_PCT"

_PURCHASE_ORDER_INDEX = "ix_purchase_order_retailer_order_date"

# The FK constraint name Postgres assigned when `4b41f6bcb2f3` created `penalty_rule`
# with an unnamed `ForeignKeyConstraint(["retailer_id"], ["retailer.id"])`: Postgres's
# default naming for an unnamed table constraint is `<table>_<column>_fkey`.
_RETAILER_ID_FKEY = "penalty_rule_retailer_id_fkey"

# Label used for both the placeholder `retailer_agreement` row's `contract_code` and the
# input to its deterministic `document_sha256`.
_PLACEHOLDER_CONTRACT_CODE_SQL = "'MIGRATION-BACKFILL-' || pr.retailer_id::text"


def _resolve_schema() -> str | None:
    """Postgres has real schemas; `batch_alter_table` on SQLite needs `schema=None`,
    same reasoning as `ddf0ca3bea0e`'s `_resolve_schema`.
    """
    return PENALTIES if op.get_bind().dialect.name != "sqlite" else None


def _qualified(table: str) -> str:
    """`table`, schema-qualified for Postgres, unqualified for SQLite's translated tables."""
    schema = _resolve_schema()
    return f"{schema}.{table}" if schema else table


def _verify_no_nulls(table: str, column: str) -> None:
    """Raise if `table.column` still has a NULL row, instead of silently enforcing NOT NULL
    over data that would violate it.
    """
    remaining = (
        op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")).scalar_one()
    )
    if remaining:
        raise RuntimeError(
            f"{table}.{column} still has {remaining} NULL row(s) after backfill; "
            "refusing to enforce NOT NULL."
        )


def _backfill_penalty_category() -> None:
    """Every pre-existing NULL `penalty_category` becomes the governed sentinel category.
    See this migration's docstring for why that sentinel is never written by the
    application itself.
    """
    table = _qualified("penalty_rule")
    op.execute(
        f"UPDATE {table} SET penalty_category = '{_BACKFILL_PENALTY_CATEGORY}' WHERE penalty_category IS NULL"
    )
    _verify_no_nulls(table, "penalty_category")


def _backfill_retailer_agreement_id() -> None:
    """Point every pre-existing `penalty_rule` row lacking `retailer_agreement_id` at a
    new, clearly-labeled placeholder `retailer_agreement` row (one per distinct
    `retailer_id` among such rows), so `retailer_agreement_id` can go `NOT NULL` with no
    row left behind. A fresh database with no such rows leaves both statements' `WHERE`
    predicates unsatisfied, so neither inserts nor updates anything.

    Postgres-only: relies on `sha256()` (built into Postgres 11+, no extension) and
    `gen_random_uuid()` (built into Postgres 13+, no extension). Never called on SQLite,
    whose databases in this codebase are always freshly created (no pre-existing
    `penalty_rule` rows), so there would be nothing for it to do there anyway.
    """
    op.execute(
        f"""
        INSERT INTO retailer_agreement (
            id, retailer_id, contract_code, title, document_sha256, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            pr.retailer_id,
            {_PLACEHOLDER_CONTRACT_CODE_SQL},
            'Backfilled placeholder agreement',
            encode(sha256(({_PLACEHOLDER_CONTRACT_CODE_SQL})::bytea), 'hex'),
            now(),
            now()
        FROM (
            SELECT DISTINCT retailer_id
            FROM {PENALTIES}.penalty_rule
            WHERE retailer_agreement_id IS NULL
        ) pr
        """
    )
    op.execute(
        f"""
        UPDATE {PENALTIES}.penalty_rule AS pr
        SET retailer_agreement_id = ra.id
        FROM retailer_agreement AS ra
        WHERE pr.retailer_agreement_id IS NULL
          AND ra.retailer_id = pr.retailer_id
          AND ra.contract_code = {_PLACEHOLDER_CONTRACT_CODE_SQL}
        """
    )
    _verify_no_nulls(_qualified("penalty_rule"), "retailer_agreement_id")


def _backfill_tier_defaults() -> None:
    """Every pre-existing NULL `tier_application`/`tier_basis` becomes the governed default
    already assumed for every tiered rule the engine prices today.
    """
    table = _qualified("penalty_rule_tier")
    op.execute(
        f"UPDATE {table} SET tier_application = '{_BACKFILL_TIER_APPLICATION}' WHERE tier_application IS NULL"
    )
    _verify_no_nulls(table, "tier_application")
    op.execute(f"UPDATE {table} SET tier_basis = '{_BACKFILL_TIER_BASIS}' WHERE tier_basis IS NULL")
    _verify_no_nulls(table, "tier_basis")


def _create_purchase_order_index() -> None:
    """CONCURRENTLY on Postgres, since purchase_order is large/high-traffic and a plain
    CREATE INDEX would hold a write lock for the duration of the build. CONCURRENTLY
    cannot run inside a transaction, so it runs in an autocommit block, Alembic's
    supported mechanism for non-transactional DDL. SQLite (unit tests only) has no
    CONCURRENTLY, so it gets a plain index instead.
    """
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY {_PURCHASE_ORDER_INDEX} "
                "ON purchase_order (retailer_id, order_date)"
            )
    else:
        op.create_index(_PURCHASE_ORDER_INDEX, "purchase_order", ["retailer_id", "order_date"])


def upgrade() -> None:
    _backfill_penalty_category()
    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column("penalty_category", existing_type=sa.String(length=60), nullable=False)

    if op.get_bind().dialect.name == "postgresql":
        _backfill_retailer_agreement_id()
    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column("retailer_agreement_id", existing_type=sa.Uuid(), nullable=False)

    _backfill_tier_defaults()
    with op.batch_alter_table("penalty_rule_tier", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column("tier_application", existing_type=sa.String(length=20), nullable=False)
        batch_op.alter_column("tier_basis", existing_type=sa.String(length=30), nullable=False)

    _create_purchase_order_index()

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        if op.get_bind().dialect.name == "postgresql":
            batch_op.drop_constraint(_RETAILER_ID_FKEY, type_="foreignkey")
        batch_op.drop_column("retailer_id")

    # Backs the join PenaltyRuleRepository now uses in place of filtering
    # penalty_rule.retailer_id directly (retailer_agreement_id -> retailer_agreement,
    # filtered on retailer_agreement.retailer_id). retailer_agreement is not
    # large/high-traffic, so a plain CREATE INDEX (not CONCURRENTLY) is fine here.
    op.create_index("ix_retailer_agreement_retailer_id", "retailer_agreement", ["retailer_id"])


def downgrade() -> None:
    op.drop_index("ix_retailer_agreement_retailer_id", table_name="retailer_agreement")

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.add_column(sa.Column("retailer_id", sa.Uuid(), nullable=True))

    penalty_rule_table = _qualified("penalty_rule")
    op.execute(
        f"""
        UPDATE {penalty_rule_table}
        SET retailer_id = (
            SELECT retailer_agreement.retailer_id FROM retailer_agreement
            WHERE retailer_agreement.id = {penalty_rule_table}.retailer_agreement_id
        )
        """
    )

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column("retailer_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key(_RETAILER_ID_FKEY, "retailer", ["retailer_id"], ["id"])

    # Placeholder `retailer_agreement` rows created by `_backfill_retailer_agreement_id`
    # during `upgrade()`, and any sentinel `penalty_category`/tier-default values written
    # by the other backfills, are deliberately left in place: downgrading a schema change
    # should not delete substantive data other rows may already reference or rely on.

    op.drop_index(_PURCHASE_ORDER_INDEX, table_name="purchase_order")

    with op.batch_alter_table("penalty_rule_tier", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column("tier_basis", existing_type=sa.String(length=30), nullable=True)
        batch_op.alter_column("tier_application", existing_type=sa.String(length=20), nullable=True)

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column("retailer_agreement_id", existing_type=sa.Uuid(), nullable=True)
        batch_op.alter_column("penalty_category", existing_type=sa.String(length=60), nullable=True)
