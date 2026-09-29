"""Langgraph schema.

Revision ID: a5b39c6e2181
Revises: 4b41f6bcb2f3
Create Date: 2026-08-29

Fifth and last of five revisions (see 0824321a02a4's docstring for the
overall squash rationale). Creates an empty `langgraph` Postgres schema --
no tables. LangGraph's own `PostgresSaver.setup()` creates and owns its
checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`) inside this schema at runtime, never via Alembic;
they were previously created in `public` and are moved here so `public`
holds no application-adjacent tables of any kind.

Postgres-only: `CREATE SCHEMA`/`DROP SCHEMA` have no SQLite equivalent and
no ORM model is bound to this schema (see
`app/db/session.py::apply_sqlite_schema_translation`, which deliberately
does not include `LANGGRAPH_SCHEMA`), so this migration is a no-op on
SQLite -- including in
`tests/unit/db/test_migration_parity.py`'s `alembic upgrade head` run.
"""

from collections.abc import Sequence

from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5b39c6e2181"
down_revision: str | None = "4b41f6bcb2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "langgraph"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(CreateSchema(SCHEMA, if_not_exists=True))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(DropSchema(SCHEMA, cascade=True, if_exists=True))
