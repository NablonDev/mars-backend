"""Penalty rule extraction schema.

Revision ID: 1d92b65b8eb4
Revises: 11ce88f609e0
Create Date: 2026-09-10

Creates `retailer_agreement` (public). Creates `penalties.extracted_penalty_rule`,
`.extracted_penalty_rule_attribute`, and `.rule_publication`;
`extracted_penalty_rule.retailer_agreement_id` FKs into `retailer_agreement.id`.
`penalties.penalty_rule` gains `basis_type`, `applies_per`, and
`currency_code`.

`workflow_thread_subject` drops its `email_event_id`/
`purchase_order_line_id` columns and the CHECK constraint guarding them,
replacing both with a polymorphic `subject_type`/`subject_id` pair backed
by a composite index.

`ix_extracted_penalty_rule_retailer_agreement_status`, `uq_retailer_agreement_contract_code`,
and `uq_retailer_agreement_document_sha256` are partial indexes (`WHERE deleted_at IS
NULL`), branched by dialect since `postgresql_where=` on a declarative `Index` is
silently dropped on SQLite.

Also corrects `process.job_item`'s `ck_job_item_item_type` CHECK constraint, which never
gained `'DISPUTE_SUMMARY_REGEN'` when that value was added to `JobTaskType`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d92b65b8eb4"
down_revision: str | None = "11ce88f609e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENALTIES = "penalties"
PROCESS = "process"

_JSONB_OR_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

_ITEM_TYPE_CHECK = "ck_job_item_item_type"
_NEW_ITEM_TYPE_CONDITION = (
    "item_type IN ('DISPUTE_SUMMARY_REGEN', 'EMAIL_INGEST', 'MITIGATION_RUN', "
    "'MITIGATION_SUMMARY_REGEN', 'ORDER_RUN', 'PENALTY_FULL_RUN', 'PO_VALIDATION', "
    "'PROJECTION_SUMMARY_REGEN')"
)
_OLD_ITEM_TYPE_CONDITION = (
    "item_type IN ('EMAIL_INGEST', 'MITIGATION_RUN', 'MITIGATION_SUMMARY_REGEN', "
    "'ORDER_RUN', 'PENALTY_FULL_RUN', 'PO_VALIDATION', 'PROJECTION_SUMMARY_REGEN')"
)


def _resolve_schema(schema: str) -> str | None:
    """Postgres has real schemas; `batch_alter_table` on SQLite needs `schema=None`,
    same reasoning as `11ce88f609e0`'s `_resolve_schema`.
    """
    return schema if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    op.create_table(
        "retailer_agreement",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("retailer_id", sa.Uuid(), nullable=False),
        sa.Column("contract_code", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("document_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=500), nullable=True),
        sa.Column("markdown_text", sa.Text(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiration_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["retailer_id"], ["retailer.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_retailer_agreement_contract_code"), "retailer_agreement", ["contract_code"], unique=False
    )
    op.create_index(
        op.f("ix_retailer_agreement_document_sha256"),
        "retailer_agreement",
        ["document_sha256"],
        unique=False,
    )
    # Partial unique indexes, not plain UNIQUE constraints: a contract re-uploaded after
    # its prior row was soft-deleted must not collide with the deleted row. Raw DDL only
    # (see this migration's docstring). Unlike the penalties-schema indexes below, no
    # dialect branch is needed: retailer_agreement carries no schema qualifier to vary
    # between Postgres and SQLite.
    op.execute(
        "CREATE UNIQUE INDEX uq_retailer_agreement_contract_code ON "
        "retailer_agreement (contract_code) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_retailer_agreement_document_sha256 ON "
        "retailer_agreement (document_sha256) WHERE deleted_at IS NULL"
    )

    op.create_table(
        "extracted_penalty_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("retailer_agreement_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.String(length=200), nullable=True),
        sa.Column("clause_text", sa.Text(), nullable=False),
        sa.Column("clause_fingerprint", sa.CHAR(length=32), nullable=False),
        sa.Column("penalty_category", sa.String(length=60), nullable=False),
        sa.Column("calc_type", sa.String(length=30), nullable=False),
        sa.Column("po_shortage_flag", sa.Boolean(), nullable=False),
        sa.Column("po_delay_flag", sa.Boolean(), nullable=False),
        sa.Column("pricing_readiness", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("extra", _JSONB_OR_JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["retailer_agreement_id"], ["retailer_agreement.id"]),
        sa.ForeignKeyConstraint(["agent_run_id"], [f"{PROCESS}.agent_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_run_id", "clause_fingerprint", name="uq_extracted_penalty_rule_run_fingerprint"
        ),
        schema=PENALTIES,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_extracted_penalty_rule_retailer_agreement_status ON "
            "penalties.extracted_penalty_rule (retailer_agreement_id, status) WHERE deleted_at IS NULL"
        )
    else:
        op.execute(
            "CREATE INDEX ix_extracted_penalty_rule_retailer_agreement_status ON "
            "extracted_penalty_rule (retailer_agreement_id, status) WHERE deleted_at IS NULL"
        )

    op.create_table(
        "extracted_penalty_rule_attribute",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extracted_rule_id", sa.Uuid(), nullable=False),
        sa.Column("branch_no", sa.Integer(), nullable=False),
        sa.Column("attribute_role", sa.String(length=30), nullable=False),
        sa.Column("metric_code", sa.String(length=40), nullable=True),
        sa.Column("metric_denominator", sa.String(length=30), nullable=True),
        sa.Column("operator", sa.String(length=10), nullable=True),
        sa.Column("value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("value_max", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("value_unit", sa.String(length=20), nullable=True),
        sa.Column("value_status", sa.String(length=30), nullable=False),
        sa.Column("currency_code", sa.CHAR(length=3), nullable=True),
        sa.Column("basis_type", sa.String(length=30), nullable=True),
        sa.Column("applies_per", sa.String(length=20), nullable=True),
        sa.Column("tier_application", sa.String(length=20), nullable=True),
        sa.Column("cap_scope", sa.String(length=20), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("extra", _JSONB_OR_JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["extracted_rule_id"],
            [f"{PENALTIES}.extracted_penalty_rule.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )
    op.create_index(
        "ix_extracted_penalty_rule_attribute_rule_branch",
        "extracted_penalty_rule_attribute",
        ["extracted_rule_id", "branch_no"],
        unique=False,
        schema=PENALTIES,
    )

    op.create_table(
        "rule_publication",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extracted_rule_id", sa.Uuid(), nullable=False),
        sa.Column("penalty_rule_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=True),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["extracted_rule_id"], [f"{PENALTIES}.extracted_penalty_rule.id"]),
        sa.ForeignKeyConstraint(["penalty_rule_id"], [f"{PENALTIES}.penalty_rule.id"]),
        sa.ForeignKeyConstraint(["agent_run_id"], [f"{PROCESS}.agent_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=PENALTIES,
    )

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema(PENALTIES)) as batch_op:
        batch_op.add_column(sa.Column("basis_type", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("applies_per", sa.String(length=20), nullable=True))
        batch_op.add_column(
            sa.Column("currency_code", sa.CHAR(length=3), nullable=False, server_default=sa.text("'USD'"))
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE process.workflow_thread_subject DROP CONSTRAINT ck_workflow_thread_subject_one_of"
        )

    with op.batch_alter_table("workflow_thread_subject", schema=_resolve_schema(PROCESS)) as batch_op:
        batch_op.drop_column("email_event_id")
        batch_op.drop_column("purchase_order_line_id")
        batch_op.add_column(sa.Column("subject_type", sa.String(length=30), nullable=False))
        batch_op.add_column(sa.Column("subject_id", sa.Uuid(), nullable=False))

    op.create_index(
        "ix_workflow_thread_subject_subject_type_subject_id",
        "workflow_thread_subject",
        ["subject_type", "subject_id"],
        unique=False,
        schema=PROCESS,
    )

    with op.batch_alter_table("job_item", schema=_resolve_schema(PROCESS)) as batch_op:
        batch_op.drop_constraint(_ITEM_TYPE_CHECK, type_="check")
        batch_op.create_check_constraint(_ITEM_TYPE_CHECK, _NEW_ITEM_TYPE_CONDITION)


def downgrade() -> None:
    with op.batch_alter_table("job_item", schema=_resolve_schema(PROCESS)) as batch_op:
        batch_op.drop_constraint(_ITEM_TYPE_CHECK, type_="check")
        batch_op.create_check_constraint(_ITEM_TYPE_CHECK, _OLD_ITEM_TYPE_CONDITION)

    op.drop_index(
        "ix_workflow_thread_subject_subject_type_subject_id",
        table_name="workflow_thread_subject",
        schema=PROCESS,
    )

    with op.batch_alter_table("workflow_thread_subject", schema=_resolve_schema(PROCESS)) as batch_op:
        batch_op.drop_column("subject_id")
        batch_op.drop_column("subject_type")
        batch_op.add_column(sa.Column("email_event_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("purchase_order_line_id", sa.Uuid(), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE process.workflow_thread_subject ADD CONSTRAINT "
            "ck_workflow_thread_subject_one_of "
            "CHECK (num_nonnulls(email_event_id, purchase_order_line_id) = 1)"
        )

    with op.batch_alter_table("penalty_rule", schema=_resolve_schema(PENALTIES)) as batch_op:
        batch_op.drop_column("currency_code")
        batch_op.drop_column("applies_per")
        batch_op.drop_column("basis_type")

    op.drop_table("rule_publication", schema=PENALTIES)

    op.drop_index(
        "ix_extracted_penalty_rule_attribute_rule_branch",
        table_name="extracted_penalty_rule_attribute",
        schema=PENALTIES,
    )
    op.drop_table("extracted_penalty_rule_attribute", schema=PENALTIES)

    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX penalties.ix_extracted_penalty_rule_retailer_agreement_status")
    else:
        op.execute("DROP INDEX ix_extracted_penalty_rule_retailer_agreement_status")
    op.drop_table("extracted_penalty_rule", schema=PENALTIES)

    op.execute("DROP INDEX uq_retailer_agreement_document_sha256")
    op.execute("DROP INDEX uq_retailer_agreement_contract_code")
    op.drop_index(op.f("ix_retailer_agreement_document_sha256"), table_name="retailer_agreement")
    op.drop_index(op.f("ix_retailer_agreement_contract_code"), table_name="retailer_agreement")
    op.drop_table("retailer_agreement")
