"""
Proves the hand-authored Alembic migrations (the 5-revision "initial" squash
in alembic/versions/: 0824321a02a4_initial_common_schema.py,
ff53dabe6e4c_initial_process_schema.py, 374aa902b053_initial_cmir_schema.py,
4b41f6bcb2f3_initial_penalties_schema.py, and
a5b39c6e2181_initial_langgraph_schema.py, plus every normal chained revision
added since -- currently just 11ce88f609e0_penalty_dispute_schema.py) actually
match app/models/, rather than just asserting it in a docstring. Builds one
SQLite DB via
`alembic upgrade head` (walks the whole chain) and another via
`Base.metadata.create_all()`, then diffs table and column names.

This test compares table and column names ONLY -- no indexes, no
constraints, no types, no nullability. It cannot catch drift in any of
those (e.g. the partial unique index on cmir_record, or the
`num_nonnulls` CHECK on workflow_thread_subject/cmir_job_item_context) --
those must be verified against a real Postgres database instead.

No live Postgres needed for this test itself -- it only checks structural
parity between the migrations and the ORM, not Postgres-specific DDL
correctness. Both engines go through `apply_sqlite_schema_translation`
because `Base.metadata` has tables bound to the `process`, `cmir`, and
`penalties` schemas (app/db/base.py), which SQLite cannot express -- the
same translation app/db/session.py and alembic/env.py apply, so the
tables land unqualified on both sides and stay comparable. The shared
master/fulfillment tables (`app/models/common/`) carry no schema binding
at all -- they resolve to `public` on Postgres and need no translation on
SQLite either, so they compare cleanly on both sides without any special
handling here.
`langgraph` is not part of this comparison: it has no ORM model and its
migration creates no tables (Postgres-only `CREATE SCHEMA`, a no-op on
SQLite).

`Base` is imported from `app.db.base` (not `app.models`) because mars_common
is the single source of truth for ORM model metadata and `app.db.base` is a
straight re-export of it; `app/models/` only re-exports the models themselves.
"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.db.base import Base
from app.db.session import apply_sqlite_schema_translation

REPO_ROOT = Path(__file__).resolve().parents[3]


def _tables_and_columns(engine) -> dict[str, set[str]]:
    inspector = inspect(engine)
    return {
        table: {col["name"] for col in inspector.get_columns(table)}
        for table in inspector.get_table_names()
        if table != "alembic_version"  # Alembic's own bookkeeping table, not part of the domain schema
    }


def test_alembic_migration_matches_orm_models(tmp_path: Path):
    migrated_db = tmp_path / "migrated.db"
    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{migrated_db}")
    command.upgrade(alembic_cfg, "head")

    migrated_engine = create_engine(f"sqlite:///{migrated_db}")
    migrated_schema = _tables_and_columns(migrated_engine)

    orm_engine = apply_sqlite_schema_translation(create_engine("sqlite://"))
    Base.metadata.create_all(orm_engine)
    orm_schema = _tables_and_columns(orm_engine)

    assert migrated_schema.keys() == orm_schema.keys(), (
        f"Table sets differ.\nMigration only: {migrated_schema.keys() - orm_schema.keys()}\n"
        f"ORM only: {orm_schema.keys() - migrated_schema.keys()}"
    )
    for table in orm_schema:
        assert migrated_schema[table] == orm_schema[table], (
            f"Column mismatch in {table!r}.\n"
            f"Migration only: {migrated_schema[table] - orm_schema[table]}\n"
            f"ORM only: {orm_schema[table] - migrated_schema[table]}"
        )


def test_migration_downgrade_reverses_cleanly(tmp_path: Path):
    db_path = tmp_path / "downgrade.db"
    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")

    engine = create_engine(f"sqlite:///{db_path}")
    remaining = {t for t in inspect(engine).get_table_names() if t != "alembic_version"}
    assert remaining == set()
