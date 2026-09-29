"""CMIR schema.

Revision ID: 374aa902b053
Revises: ff53dabe6e4c
Create Date: 2026-08-29

Third of five revisions (see 0824321a02a4's docstring for the overall
squash rationale). Creates every `cmir`-schema table.

D1 (FK-cycle resolution, approved Phase 1 plan): this revision creates
`process.workflow_thread_subject` -- even though that table's Python class
(`WorkflowThreadSubject`) is declared in `app/models/process/workflow.py`
-- placed here, after `cmir.email_event` and before
`cmir.job_item_context`. The cycle: `process.workflow_thread_subject.
email_event_id` -> `cmir.email_event.id` (process needs cmir) vs.
`cmir.job_item_context.job_item_id` -> `process.job_item.id` (cmir needs
process). Postgres requires the referenced table to exist at
`CREATE TABLE` time; this is the one place in the whole chain where a
table is created inside a different schema's revision than the one its
Python class lives in.

The `CHECK (num_nonnulls(email_event_id, purchase_order_line_id) = 1)` on
`workflow_thread_subject`, and the equivalent CHECK on
`cmir_job_item_context`, are both PostgreSQL-only builtins -- raw DDL,
skipped on SQLite (the whole test suite, including
tests/unit/db/test_migration_parity.py's `alembic upgrade head` run).

`cmir_record`'s partial unique index -- `(customer_identity_key,
target_customer_material_ref_key) WHERE is_current` -- is also raw DDL
for the same reason no partial index can live on the ORM model without
silently becoming a full unique index on SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "374aa902b053"
down_revision: str | None = "ff53dabe6e4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CMIR = "cmir"
PROCESS = "process"


def upgrade() -> None:
    op.create_table(
        "email_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("raw_content", sa.Text(), nullable=True),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column("source_imap_id", sa.String(length=100), nullable=True),
        sa.Column(
            "extracted_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "missing_fields",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("queue_status", sa.String(length=30), nullable=False),
        sa.Column("queue_message_id", sa.String(length=200), nullable=True),
        sa.Column("queue_error", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queue_delivery_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema=CMIR,
    )

    # D1 -- see module docstring: created here (cmir revision), not in the
    # process revision, because it FKs into cmir.email_event which must
    # already exist.
    op.create_table(
        "workflow_thread_subject",
        sa.Column("workflow_thread_id", sa.Uuid(), nullable=False),
        sa.Column("email_event_id", sa.Uuid(), nullable=True),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["email_event_id"], [f"{CMIR}.email_event.id"]),
        sa.ForeignKeyConstraint(["purchase_order_line_id"], ["purchase_order_line.id"]),
        sa.ForeignKeyConstraint(["workflow_thread_id"], [f"{PROCESS}.workflow_thread.id"]),
        sa.PrimaryKeyConstraint("workflow_thread_id"),
        schema=PROCESS,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE process.workflow_thread_subject ADD CONSTRAINT "
            "ck_workflow_thread_subject_one_of "
            "CHECK (num_nonnulls(email_event_id, purchase_order_line_id) = 1)"
        )

    op.create_table(
        "cmir_job_run_context",
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("external_batch_id", sa.String(length=100), nullable=True),
        sa.Column("service_bus_topic", sa.String(length=200), nullable=True),
        sa.Column("service_bus_subscription", sa.String(length=200), nullable=True),
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
        schema=CMIR,
    )

    op.create_table(
        "cmir_job_item_context",
        sa.Column("job_item_id", sa.Uuid(), nullable=False),
        sa.Column("email_event_id", sa.Uuid(), nullable=True),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["email_event_id"], [f"{CMIR}.email_event.id"]),
        sa.ForeignKeyConstraint(["job_item_id"], [f"{PROCESS}.job_item.id"]),
        sa.ForeignKeyConstraint(["purchase_order_line_id"], ["purchase_order_line.id"]),
        sa.PrimaryKeyConstraint("job_item_id"),
        schema=CMIR,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE cmir.cmir_job_item_context ADD CONSTRAINT "
            "ck_cmir_job_item_context_one_of "
            "CHECK (num_nonnulls(email_event_id, purchase_order_line_id) = 1)"
        )

    op.create_table(
        "cmir_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=True),
        sa.Column("email_event_id", sa.Uuid(), nullable=True),
        sa.Column("sender_type", sa.String(length=100), nullable=False),
        sa.Column("customer_identity", sa.String(length=255), nullable=False),
        sa.Column("material_identity", sa.String(length=255), nullable=False),
        sa.Column("intent_phrase", sa.Text(), nullable=True),
        sa.Column("existing_cmir_ref", sa.String(length=255), nullable=False),
        sa.Column("brand", sa.String(length=100), nullable=False),
        sa.Column("site", sa.String(length=100), nullable=False),
        sa.Column("target_grd_code", sa.String(length=255), nullable=False),
        sa.Column("target_customer_material_ref", sa.String(length=255), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("customer_identity_key", sa.String(length=255), nullable=False),
        sa.Column("target_customer_material_ref_key", sa.String(length=255), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["email_event_id"], [f"{CMIR}.email_event.id"]),
        sa.ForeignKeyConstraint(["purchase_order_line_id"], ["purchase_order_line.id"]),
        sa.ForeignKeyConstraint(["superseded_by_id"], [f"{CMIR}.cmir_record.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=CMIR,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_cmir_record_current_identity ON cmir.cmir_record "
            "(customer_identity_key, target_customer_material_ref_key) WHERE is_current"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX uq_cmir_record_current_identity ON cmir_record "
            "(customer_identity_key, target_customer_material_ref_key) WHERE is_current"
        )

    op.create_table(
        "email_action_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email_event_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["email_event_id"], [f"{CMIR}.email_event.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=CMIR,
    )


def downgrade() -> None:
    op.drop_table("email_action_log", schema=CMIR)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX cmir.uq_cmir_record_current_identity")
    else:
        op.execute("DROP INDEX uq_cmir_record_current_identity")
    op.drop_table("cmir_record", schema=CMIR)
    op.drop_table("cmir_job_item_context", schema=CMIR)
    op.drop_table("cmir_job_run_context", schema=CMIR)
    op.drop_table("workflow_thread_subject", schema=PROCESS)
    op.drop_table("email_event", schema=CMIR)
