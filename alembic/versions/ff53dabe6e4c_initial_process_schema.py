"""Process schema.

Revision ID: ff53dabe6e4c
Revises: 0824321a02a4
Create Date: 2026-08-29

Second of five revisions (see 0824321a02a4's docstring for the overall
squash rationale). Creates the shared job/agent/workflow backbone used by
both the `cmir`/`po_validation` and `penalties` domains, consolidating
what were two parallel stacks (the old `fines` job_run/job_item, and
`cmir`'s agent_runs/agent_traces/workflow_threads/hitl_actions/
pending_human_actions).

D1 (FK-cycle resolution, approved Phase 1 plan): `process.workflow_thread`
is created here, but `process.workflow_thread_subject` -- which FKs into
`cmir.email_event` -- is deliberately NOT created in this revision. It is
created by the *next* (`cmir`) revision instead, positioned after
`cmir.email_event` and before `cmir.job_item_context`, because Postgres
requires a FK's target table to exist at `CREATE TABLE` time and
`cmir.email_event` doesn't exist yet when this revision runs. See that
revision's docstring for the other half of this note.

`process.job_run` deliberately has no `status` column (see
`app/models/process/job.py::JobRun`'s docstring) -- a real, load-bearing
divergence from docs/redesigned-schema.md's own `process.job_run` table
listing, which does show one; that doc was drafted without this
codebase's context and this squash preserves the existing, deliberate
"derived, not stored" design instead of reintroducing it.

Two partial unique indexes are raw DDL here, branched by dialect (not
Postgres-only -- same style as the pre-restructure `uq_job_item_inflight`,
`e803d9470f31`):

- `uq_job_item_inflight` on `job_item (item_type, dedupe_key) WHERE status
  IN ('PENDING', 'RUNNING') AND dedupe_key IS NOT NULL` -- restores the
  in-flight dedupe constraint the initial version of this squash dropped
  (see `app/models/process/job.py::JobItem`'s docstring).
- `uq_agent_one_active_per_code` on `agent (agent_code) WHERE is_active`
  -- guarantees at most one active prompt version per agent.

`job_item.item_type`'s CHECK constraint gained `'MITIGATION_RUN'` the same
way (edited into this squashed revision in place, matching this project's
edit-in-place-rather-than-chain convention for these five revisions --
see `app/models/enums.py::JobTaskType`'s docstring): the penalty-mitigation
batch entry point (`app/api/v1/job_runs.py::_trigger_penalty_mitigation_batch`)
dispatches this new `item_type`, mirroring `ORDER_RUN`'s existing shape.

`processing_error.purchase_order_line_id` (edited into this squashed
revision in place, matching this project's edit-in-place-rather-than-chain
convention for these five revisions -- see `app/models/process/
processing_error.py`'s docstring) FKs into `purchase_order_line.id` (bare,
unqualified -- that table lives in `public`, resolved via Postgres's default
`search_path`), valid here since migrations run in schema order (`public`
-> `process` -> `cmir` -> `penalties`) and the `purchase_order_line` table
has already been created by the time this revision runs.

`job_item.attempt_count`/`max_attempts`/`metadata` gained a `server_default`
(edited into this squashed revision in place, same convention): they were
previously NOT NULL with only a SQLAlchemy-level `default=`
(app/models/process/job.py), so a raw insert bypassing the ORM (e.g. a test,
or a manual DB write) failed NOT NULL before ever reaching
`uq_job_item_inflight`. `metadata`'s default is dialect-branched (`'{}'::jsonb`
on Postgres, `'{}'` elsewhere) for the same SQLite-rendering reason as this
file's partial unique indexes.

`job_item.item_type`'s CHECK constraint gained `'PENALTY_FULL_RUN'` the same
way (edited into this squashed revision in place, same convention -- see
`app/models/enums.py::JobTaskType`'s docstring): the combined-run batch entry
point (`app/api/v1/job_runs.py::_trigger_penalty_full_run_batch`) dispatches
this new `item_type`, storing its requested `steps` on this table's own
`metadata` column (no further schema change needed) rather than a new
`penalty_job_item_context` column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ff53dabe6e4c"
down_revision: str | None = "0824321a02a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "process"


def upgrade() -> None:
    op.create_table(
        "job_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=100), nullable=False),
        sa.Column("trigger_type", sa.String(length=50), nullable=False),
        sa.Column("requested_item_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger_type IN ('MANUAL_BATCH', 'ON_DEMAND', 'SCHEDULED_DAILY')", name="ck_job_run_trigger_type"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )

    op.create_table(
        "agent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_code", sa.String(length=100), nullable=False),
        sa.Column("agent_name", sa.String(length=200), nullable=False),
        sa.Column("domain", sa.String(length=50), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("domain IN ('cmir', 'penalties')", name="ck_agent_domain"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_code", "prompt_version", name="uq_agent_code_prompt_version"),
        schema=SCHEMA,
    )
    op.create_index(op.f("ix_agent_agent_code"), "agent", ["agent_code"], unique=False, schema=SCHEMA)
    # Partial unique index -- at most one active prompt version per agent
    # code. Same "postgresql_where= is silently dropped on SQLite" reason as
    # every other partial index in this squash; raw DDL only, dialect-branched.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_agent_one_active_per_code ON process.agent (agent_code) WHERE is_active"
        )
    else:
        op.execute("CREATE UNIQUE INDEX uq_agent_one_active_per_code ON agent (agent_code) WHERE is_active")

    # attempt_count/max_attempts/metadata get a server_default in addition to
    # their existing SQLAlchemy-level `default=` (app/models/process/job.py),
    # so a raw insert that omits them (e.g. a manual DB write, or a test's
    # raw SQL) doesn't fail NOT NULL before reaching business-logic
    # constraints like uq_job_item_inflight. metadata's default is
    # dialect-branched the same way as this file's partial unique indexes
    # (see below): `'{}'::jsonb` is not valid syntax on SQLite, which this
    # revision also renders for (tests/unit/db/test_migration_parity.py).
    job_item_metadata_default = (
        sa.text("'{}'::jsonb") if op.get_bind().dialect.name == "postgresql" else sa.text("'{}'")
    )
    op.create_table(
        "job_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            server_default=job_item_metadata_default,
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DEAD', 'PENDING', 'RUNNING', 'SUCCEEDED')", name="ck_job_item_status"
        ),
        sa.CheckConstraint(
            "item_type IN ('EMAIL_INGEST', 'MITIGATION_RUN', 'MITIGATION_SUMMARY_REGEN', 'ORDER_RUN', "
            "'PENALTY_FULL_RUN', 'PO_VALIDATION', 'PROJECTION_SUMMARY_REGEN')",
            name="ck_job_item_item_type",
        ),
        sa.ForeignKeyConstraint(["job_run_id"], [f"{SCHEMA}.job_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_job_item_claimable", "job_item", ["status", "available_at"], unique=False, schema=SCHEMA
    )
    op.create_index("ix_job_item_job_run_id", "job_item", ["job_run_id"], unique=False, schema=SCHEMA)
    # Partial unique index -- deliberately NOT expressible via the ORM model
    # (SQLAlchemy's `postgresql_where=` on an `Index` is silently dropped on
    # SQLite, which would otherwise create a *full* unique index there and
    # wrongly reject a legitimate second terminal (SUCCEEDED/DEAD) row, or a
    # second NULL-dedupe_key row, for the same item_type). Raw DDL only; see
    # app/models/process/job.py::JobItem's docstring. Branched by dialect
    # (not left Postgres-only) so it also renders on SQLite -- same style as
    # the pre-restructure e803d9470f31.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_job_item_inflight ON process.job_item "
            "(item_type, dedupe_key) WHERE status IN ('PENDING', 'RUNNING') AND dedupe_key IS NOT NULL"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX uq_job_item_inflight ON job_item "
            "(item_type, dedupe_key) WHERE status IN ('PENDING', 'RUNNING') AND dedupe_key IS NOT NULL"
        )

    op.create_table(
        "workflow_thread",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_item_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("current_node", sa.String(length=100), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_item_id"], [f"{SCHEMA}.job_item.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )

    op.create_table(
        "agent_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_item_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_thread_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("run_type", sa.String(length=50), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], [f"{SCHEMA}.agent.id"]),
        sa.ForeignKeyConstraint(["job_item_id"], [f"{SCHEMA}.job_item.id"]),
        sa.ForeignKeyConstraint(["workflow_thread_id"], [f"{SCHEMA}.workflow_thread.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )

    op.create_table(
        "agent_trace",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("node_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "input_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "output_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], [f"{SCHEMA}.agent_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )

    op.create_table(
        "human_action",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_item_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_thread_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("action_type", sa.String(length=100), nullable=True),
        sa.Column("interrupt_type", sa.String(length=100), nullable=False),
        sa.Column(
            "request_payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "state_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "response_payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("decision", sa.String(length=100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], [f"{SCHEMA}.agent_run.id"]),
        sa.ForeignKeyConstraint(["job_item_id"], [f"{SCHEMA}.job_item.id"]),
        sa.ForeignKeyConstraint(["workflow_thread_id"], [f"{SCHEMA}.workflow_thread.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )

    op.create_table(
        "processing_error",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_item_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=True),
        sa.Column("error_type", sa.String(length=100), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("node_name", sa.String(length=100), nullable=True),
        sa.Column(
            "raw_error_detail",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], [f"{SCHEMA}.agent_run.id"]),
        sa.ForeignKeyConstraint(["job_item_id"], [f"{SCHEMA}.job_item.id"]),
        sa.ForeignKeyConstraint(["purchase_order_line_id"], ["purchase_order_line.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("processing_error", schema=SCHEMA)
    op.drop_table("human_action", schema=SCHEMA)
    op.drop_table("agent_trace", schema=SCHEMA)
    op.drop_table("agent_run", schema=SCHEMA)
    op.drop_table("workflow_thread", schema=SCHEMA)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX process.uq_job_item_inflight")
    else:
        op.execute("DROP INDEX uq_job_item_inflight")
    op.drop_index("ix_job_item_job_run_id", table_name="job_item", schema=SCHEMA)
    op.drop_index("ix_job_item_claimable", table_name="job_item", schema=SCHEMA)
    op.drop_table("job_item", schema=SCHEMA)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX process.uq_agent_one_active_per_code")
    else:
        op.execute("DROP INDEX uq_agent_one_active_per_code")
    op.drop_index(op.f("ix_agent_agent_code"), table_name="agent", schema=SCHEMA)
    op.drop_table("agent", schema=SCHEMA)
    op.drop_table("job_run", schema=SCHEMA)
