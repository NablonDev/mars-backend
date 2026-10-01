"""Integration tests for `CmirRecordRepository` against a real Postgres
instance -- unlike the unit suite, which stubs the Session itself (see
tests/unit/repositories/test_cmir_repositories.py).

Was against `PostgresCMIRRepository`/`CMIRRepository` (`app/repositories/cmir.py`,
a per-call `Database`-session repository keyed by `app.schemas.cmir.Cmir`).
Relocated onto `app.repositories.cmir.cmir_record.CmirRecordRepository`
(injected-`Session` pattern, `merged` passed as a plain dict -- see that
module's docstring for why it no longer depends on the `Cmir` schema).

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance, same convention as
tests/integration/test_job_queue_postgres.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.cmir.cmir_record import CmirRecordRepository, CmirVersionConflict


def _connect_or_none() -> Database | None:
    settings = get_settings()
    if not settings.database.url.startswith("postgresql"):
        return None

    db = Database(settings.database.url)
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        db.dispose()
        return None
    return db


@pytest.fixture(scope="module")
def pg_database():
    db = _connect_or_none()
    if db is None:
        pytest.skip(
            "No reachable Postgres DATABASE_URL configured -- skipping CMIR-repository Postgres "
            "integration tests."
        )
    db.create_all_tables()
    yield db
    db.dispose()


@pytest.fixture
def identity():
    """Unique per test so repeated runs against a shared dev database never collide."""
    return f"Integration Test Customer {uuid4().hex[:8]}"


@pytest.fixture
def material_ref():
    return f"MAT-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(pg_database: Database, identity: str):
    yield
    with pg_database.session() as session:
        session.execute(
            text("DELETE FROM cmir.cmir_record WHERE customer_identity = :customer_identity"),
            {"customer_identity": identity},
        )


def _merged(*, customer_identity: str, target_customer_material_ref: str, brand: str) -> dict:
    return {
        "sender_type": "EMAIL",
        "customer_identity": customer_identity,
        "material_identity": "MAT-IDENTITY",
        "intent_phrase": None,
        "existing_cmir_ref": "",
        "brand": brand,
        "site": "",
        "target_grd_code": "",
        "target_customer_material_ref": target_customer_material_ref,
        "effective_date": None,
        "reason": None,
    }


def test_supersede_and_insert_then_get_current_round_trips(
    pg_database: Database, identity: str, material_ref: str
):
    with pg_database.session() as session:
        repo = CmirRecordRepository(session)

        new_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=material_ref,
            merged=_merged(
                customer_identity=identity,
                target_customer_material_ref=material_ref,
                brand="IntegrationBrand",
            ),
            expected_current_id=None,
        )

        current = repo.get_current(identity, material_ref)
        assert current is not None
        assert current["id"] == new_id
        assert current["brand"] == "IntegrationBrand"


def test_supersede_and_insert_retires_previous_current_row(
    pg_database: Database, identity: str, material_ref: str
):
    with pg_database.session() as session:
        repo = CmirRecordRepository(session)

        first_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=material_ref,
            merged=_merged(
                customer_identity=identity, target_customer_material_ref=material_ref, brand="FirstVersion"
            ),
            expected_current_id=None,
        )

        second_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=material_ref,
            merged=_merged(
                customer_identity=identity, target_customer_material_ref=material_ref, brand="SecondVersion"
            ),
            expected_current_id=first_id,
        )

        current = repo.get_current(identity, material_ref)
        assert current is not None
        assert current["id"] == second_id
        assert current["brand"] == "SecondVersion"


def test_supersede_and_insert_raises_conflict_on_stale_expected_id(
    pg_database: Database, identity: str, material_ref: str
):
    with pg_database.session() as session:
        repo = CmirRecordRepository(session)

        repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=material_ref,
            merged=_merged(
                customer_identity=identity, target_customer_material_ref=material_ref, brand="OnlyVersion"
            ),
            expected_current_id=None,
        )

        with pytest.raises(CmirVersionConflict):
            repo.supersede_and_insert(
                customer_identity=identity,
                target_customer_material_ref=material_ref,
                merged=_merged(
                    customer_identity=identity, target_customer_material_ref=material_ref, brand="OnlyVersion"
                ),
                expected_current_id=None,  # stale: a current row already exists now
            )


def test_get_health_trend_reflects_a_record_created_this_month(
    pg_database: Database, identity: str, material_ref: str
):
    with pg_database.session() as session:
        repo = CmirRecordRepository(session)
        record_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=material_ref,
            merged=_merged(
                customer_identity=identity, target_customer_material_ref=material_ref, brand="TrendBrand"
            ),
            expected_current_id=None,
        )

        trend = repo.get_health_trend(months=6)

        assert len(trend) == 6
        current_month = trend[-1]
        # This test's own row is real, freshly created, current, and passes
        # every health check (non-blank mandatory fields, not stale, no
        # duplicate) -- so it must show up in both this month's total and
        # healthy counts, proving the point-in-time reconstruction actually
        # counts real rows rather than returning a static/empty shape.
        assert current_month["total"] >= 1
        assert current_month["healthy"] >= 1
        assert record_id is not None


def test_get_housekeeping_audit_log_labels_create_then_refresh(
    pg_database: Database, identity: str, material_ref: str
):
    with pg_database.session() as session:
        repo = CmirRecordRepository(session)

        first_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=material_ref,
            merged=_merged(
                customer_identity=identity, target_customer_material_ref=material_ref, brand="FirstVersion"
            ),
            expected_current_id=None,
        )
        second_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=material_ref,
            merged=_merged(
                customer_identity=identity, target_customer_material_ref=material_ref, brand="SecondVersion"
            ),
            expected_current_id=first_id,
        )

        log = repo.get_housekeeping_audit_log(limit=10000)
        by_id = {entry["id"]: entry for entry in log}

        # Neither row went through a human_action (both created touchlessly
        # via supersede_and_insert directly, not through a workflow_thread
        # interrupt) -- executed_by/outcome must stay honestly None, not
        # fabricated.
        assert by_id[str(first_id)]["action"] == "Create"
        assert by_id[str(first_id)]["executed_by"] is None
        assert by_id[str(first_id)]["outcome"] is None
        assert by_id[str(second_id)]["action"] == "Refresh"
        assert by_id[str(second_id)]["customer_identity"] == identity


def test_get_health_snapshot_flags_stale_and_missing_field_rows_not_healthy_ones(
    pg_database: Database, identity: str, material_ref: str
):
    """Real-Postgres-only (not the SQLite unit suite): exercises the actual
    `func.date_trunc` GROUP BY query, not just the Python categorization.
    Runs against a shared dev DB, so this asserts on the specific rows this
    test inserts (found by id), not on total/aggregate counts, which other
    tests' data could affect.
    """
    stale_material_ref = f"{material_ref}-STALE"
    healthy_material_ref = f"{material_ref}-HEALTHY"

    with pg_database.session() as session:
        repo = CmirRecordRepository(session)

        stale_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=stale_material_ref,
            merged=_merged(
                customer_identity=identity,
                target_customer_material_ref=stale_material_ref,
                brand="StaleBrand",
            )
            | {"sender_type": ""},  # blank mandatory field -> also missing_required_field
            expected_current_id=None,
        )
        # The repository's public write API always defaults valid_from to
        # now() -- there is no way to insert an already-stale row through
        # it, so this backdates it directly, same technique `_cleanup`
        # already uses for its own raw SQL against this table.
        session.execute(
            text("UPDATE cmir.cmir_record SET valid_from = now() - interval '200 days' WHERE id = :id"),
            {"id": str(stale_id)},
        )
        session.commit()

        healthy_id = repo.supersede_and_insert(
            customer_identity=identity,
            target_customer_material_ref=healthy_material_ref,
            # `_merged()`'s own defaults leave intent_phrase/existing_cmir_ref/
            # site blank -- all three are MANDATORY_FIELDS (app/schemas/cmir/
            # domain.py), so a genuinely "healthy" row must override them, or
            # get_health_snapshot correctly (not a bug) flags it
            # missing_required_field like every other row this helper builds.
            merged=_merged(
                customer_identity=identity,
                target_customer_material_ref=healthy_material_ref,
                brand="HealthyBrand",
            )
            | {"intent_phrase": "Confirm mapping", "existing_cmir_ref": "CMIR-EXISTING", "site": "Site A"},
            expected_current_id=None,
        )

    with pg_database.session() as session:
        repo = CmirRecordRepository(session)
        snapshot = repo.get_health_snapshot(stale_days=180, attention_limit=10000)

    attention_by_id = {item["id"]: item for item in snapshot["needing_attention"]}
    stale_item = attention_by_id.get(str(stale_id))
    assert stale_item is not None, "backdated/blank-sender_type row should appear in needing_attention"
    assert set(stale_item["reasons"]) == {"stale_validation", "missing_required_field"}

    assert str(healthy_id) not in attention_by_id

    categories = {c["category"]: c["count"] for c in snapshot["unhealthy_breakdown"]}
    assert categories["stale_validation"] >= 1
    assert categories["missing_required_field"] >= 1

    # Shape/query-correctness check on the date_trunc'd trend, not exact
    # counts (other tests/data in a shared DB affect the current month's count).
    assert all(set(row.keys()) == {"month", "count"} for row in snapshot["creation_trend"])
    assert all(len(row["month"]) == 7 and row["month"][4] == "-" for row in snapshot["creation_trend"])
