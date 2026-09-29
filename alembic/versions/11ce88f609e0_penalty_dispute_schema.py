"""Penalty dispute schema.

Revision ID: 11ce88f609e0
Revises: a5b39c6e2181
Create Date: 2026-09-03

Sixth revision, first genuinely incremental one (the five before it --
0824321a02a4/ff53dabe6e4c/374aa902b053/4b41f6bcb2f3/a5b39c6e2181 -- are the
pre-release "initial" squash, edited in place rather than chained, per
0824321a02a4's docstring: no production data to preserve at that point).
That convention stopped being safe once a real local database already had
those five applied -- editing an already-applied revision's file changes
nothing for `alembic upgrade head` (alembic tracks revision *ids* applied,
never content), so the `penalty_dispute` table silently never got created
against any such database no matter how many times `4b41f6bcb2f3` was
edited in place. This revision undoes that edit-in-place attempt and
instead adds the dispute-resolution feature as a normal, chained revision:

- `penalty_dispute` (see `app.models.penalties.dispute.PenaltyDispute`),
  FK'd to the already-existing `penalties.actual_penalty` and
  `penalties.penalty_rule` from the initial squash -- no reordering of
  those needed, this revision runs strictly after both already exist.
- `penalty_summary.summary_type`'s CHECK constraint gains `'DISPUTE'` as a
  third valid value (see `app.models.penalties.summary.PenaltySummary`'s
  module docstring). `uq_penalty_summary_po_type_date` is untouched --
  DISPUTE rows are keyed by it exactly like PROJECTION/MITIGATION; no
  `dispute_id` column exists anywhere on `penalty_summary` (a nullable one
  was tried and reverted -- see that same docstring for why, and for the
  known limitation this keeps).
- `penalty_job_item_context` gets no new column: a `DISPUTE_SUMMARY_REGEN`
  item's dispute id lives in `process.job_item.metadata_json` instead (see
  `app.models.penalties.job_context.PenaltyJobItemContext`'s docstring).
- `penalty_rule.rule_code` widens from `varchar(20)` to `varchar(50)`.
  It was `varchar(20)` since the initial squash (`4b41f6bcb2f3`) -- too
  narrow for some dispute-scenario rule codes tried during this feature's
  seed data (e.g. `RULE-DSP-B-OTIF-GRACE`, 21 chars), which failed against
  real Postgres with `value too long for type character varying(20)`
  (SQLite's TEXT affinity doesn't enforce column length at all, so this
  passed every SQLite-backed test in this repo silently -- a real,
  structural blind spot in this test suite for this whole class of bug;
  see `app.models.penalties.rule.PenaltyRule`'s module docstring). The seed
  data was renamed to fit within 20 chars regardless (`RULE-DSPB-OTIFGRACE`,
  19 chars) -- this widening isn't load-bearing for the current dispute
  seed data, it's headroom against the same class of failure recurring for
  any future rule code. Bundled into this revision rather than split into
  its own, per the same reasoning above: `4b41f6bcb2f3` is part of the
  pre-release squash and must not be edited in place, so the widen has to
  live in a normal chained revision -- this one already is one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "11ce88f609e0"
down_revision: str | None = "a5b39c6e2181"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"

_OLD_SUMMARY_TYPE_CHECK = "ck_penalty_summary_summary_type"


def _resolve_schema() -> str | None:
    """Postgres has real schemas, so batch operations below get
    `schema=PENALTIES` explicitly. SQLite's schema_translate_map already
    collapses "penalties" to no schema for every other operation in this
    file, but `batch_alter_table` builds its temp-table name by literal
    string concatenation rather than through the compiled-SQL translation
    layer, so passing `schema=PENALTIES` to it on SQLite produces an
    invalid `penalties._alembic_tmp_...` name -- it must get `schema=None`
    there instead.
    """
    dialect = op.get_bind().dialect.name
    return PENALTIES if dialect != "sqlite" else None


def _alter_summary_type_check(new_condition: str) -> None:
    """Drop and recreate `penalty_summary`'s `summary_type` CHECK constraint
    under its existing name. SQLite has no ALTER-constraint support at all
    (this test suite runs every migration against SQLite via the
    schema_translate_map -- see tests/unit/db/test_migration_parity.py's
    module docstring), so this needs `batch_alter_table`'s copy-and-move
    strategy there. Postgres runs it as a plain ALTER (no table recreation
    needed).
    """
    with op.batch_alter_table("penalty_summary", schema=_resolve_schema()) as batch_op:
        batch_op.drop_constraint(_OLD_SUMMARY_TYPE_CHECK, type_="check")
        batch_op.create_check_constraint(_OLD_SUMMARY_TYPE_CHECK, new_condition)


def upgrade() -> None:
    op.create_table(
        "penalty_dispute",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispute_number", sa.String(length=50), nullable=False),
        sa.Column("actual_penalty_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.String(length=30), nullable=False),
        sa.Column("claimed_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("computed_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("delta_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("verdict", sa.String(length=30), nullable=True),
        sa.Column("dispute_status", sa.String(length=30), nullable=False),
        sa.Column(
            "analysis_breakdown",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=200), nullable=True),
        sa.Column("override_verdict", sa.String(length=30), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "dispute_status IN ('OPEN', 'ANALYZED', 'RESOLVED', 'OVERRIDDEN')",
            name="ck_penalty_dispute_dispute_status",
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('NO_PAY', 'PAY_PARTIAL', 'PAY_FULL')",
            name="ck_penalty_dispute_verdict",
        ),
        sa.CheckConstraint(
            "override_verdict IS NULL OR override_verdict IN ('NO_PAY', 'PAY_PARTIAL', 'PAY_FULL')",
            name="ck_penalty_dispute_override_verdict",
        ),
        sa.CheckConstraint(
            "reason_code IN ('AMOUNT_INCORRECT', 'NOT_LATE', 'QTY_CONFIRMED', 'RULE_MISAPPLIED', 'OTHER')",
            name="ck_penalty_dispute_reason_code",
        ),
        sa.ForeignKeyConstraint(["actual_penalty_id"], [f"{PENALTIES}.actual_penalty.id"]),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.ForeignKeyConstraint(["rule_id"], [f"{PENALTIES}.penalty_rule.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )
    op.create_index(
        op.f("ix_penalty_dispute_dispute_number"),
        "penalty_dispute",
        ["dispute_number"],
        unique=True,
        schema=PENALTIES,
    )

    _alter_summary_type_check("summary_type IN ('PROJECTION', 'MITIGATION', 'DISPUTE')")

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column(
            "rule_code",
            existing_type=sa.String(length=20),
            type_=sa.String(length=50),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("penalty_rule", schema=_resolve_schema()) as batch_op:
        batch_op.alter_column(
            "rule_code",
            existing_type=sa.String(length=50),
            type_=sa.String(length=20),
            existing_nullable=False,
        )

    _alter_summary_type_check("summary_type IN ('PROJECTION', 'MITIGATION')")

    op.drop_index(op.f("ix_penalty_dispute_dispute_number"), table_name="penalty_dispute", schema=PENALTIES)
    op.drop_table("penalty_dispute", schema=PENALTIES)
