"""End-to-end integration test for the ontology-update POC's HTTP API
(`app/api/v1/ontology_update.py`), against a real Postgres instance and the
*production* composition root -- unlike
`tests/integration/test_ontology_update_hitl_postgres.py` (which drives the
graph directly with a `MemorySaver`), this test goes through:

    HTTP request -> FastAPI route -> OntologyUpdateRunService
    -> Container.ontology_update_unit_of_work -> the real `PostgresSaver`
    checkpointer -> LangGraph -> [approve -> MaterialMasterService ->
    MasterDataRepository -> real UPDATE] / [reject -> no write]

`with TestClient(app) as client:` runs `app.main`'s real lifespan, so
`Container.build()` opens the actual `PostgresSaver` checkpointer -- the
same one CMIR/PO-validation use -- proving the ontology-update graph's
checkpoints durably survive a real interrupt/resume round trip through the
production persistence layer, not just the in-memory `MemorySaver` the
lower-level graph test uses. Every assertion about the database reads back
through an independent session, proving durability, not just an in-memory
view.

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance, same convention as
`test_ontology_update_hitl_postgres.py`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.container import Container
from app.db.session import Database
from app.main import create_app
from app.repositories.common.master_data import MasterDataRepository
from tests.conftest import TEST_INTERNAL_API_KEY

HEADERS = {"X-Internal-Api-Key": TEST_INTERNAL_API_KEY}


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
            "No reachable Postgres DATABASE_URL configured -- skipping ontology-update "
            "API Postgres integration tests."
        )
    db.create_all_tables()
    yield db
    db.dispose()


@pytest.fixture
def suffix() -> str:
    return uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _cleanup(pg_database: Database, suffix: str):
    yield
    with pg_database.session() as session:
        session.execute(
            text("DELETE FROM common.material_master WHERE sap_material_number LIKE :pattern"),
            {"pattern": f"HITLAPI-SAP-{suffix}-%"},
        )
        session.execute(
            text("DELETE FROM common.material WHERE material_code LIKE :pattern"),
            {"pattern": f"HITLAPI-MAT-{suffix}-%"},
        )


@pytest.fixture(autouse=True)
def _reset_container():
    """`Container` is a process-wide singleton (`Container._instance`) --
    reset it before and after each test so this module's real lifespan
    always builds its own `Container` against `pg_database`, and never
    leaks a Postgres-backed `Container` into unrelated tests that run
    afterward in the same process."""
    Container.close()
    yield
    Container.close()


def _seed(pg_database: Database, suffix: str) -> dict:
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        source = md.add_material(f"HITLAPI-MAT-{suffix}-DISC")
        replacement = md.add_material(f"HITLAPI-MAT-{suffix}-REPL")
        plant = md.get_or_create_plant(f"HITLAPI-PLANT-{suffix}")
        master = md.add_material_master(
            material_id=source["id"],
            sap_material_number=f"HITLAPI-SAP-{suffix}-1",
            plant_id=plant["id"],
        )
    return {"source": source, "replacement": replacement, "master": master}


def _read_follow_up_material_id(pg_database: Database, material_id) -> object:
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        rows = md.list_material_masters_for_material(material_id)
    return rows[0]["follow_up_material_id"]


def test_start_then_approve_updates_postgres_through_the_real_api(pg_database: Database, suffix: str):
    seeded = _seed(pg_database, suffix)
    message = (
        f"Update the replacement material for {seeded['source']['material_code']} "
        f"to {seeded['replacement']['material_code']}."
    )

    app = create_app()
    with TestClient(app) as client:
        start_response = client.post(
            "/api/v1/ontology-update/requests", json={"message": message}, headers=HEADERS
        )
        assert start_response.status_code == 202, start_response.text
        started = start_response.json()["data"]
        assert started["status"] == "awaiting_approval"
        assert started["proposal"]["relationship"]["name"] == "succeededBy"
        thread_id = started["thread_id"]

        # Not written yet -- independent read confirms it's still unset.
        assert _read_follow_up_material_id(pg_database, seeded["source"]["id"]) is None

        decide_response = client.post(
            f"/api/v1/ontology-update/requests/{thread_id}/decisions",
            json={"decision": "approve"},
            headers=HEADERS,
        )
        assert decide_response.status_code == 200, decide_response.text
        decided = decide_response.json()["data"]
        assert decided["status"] == "completed"
        assert decided["execution_result"]["material_master_id"] == str(seeded["master"]["id"])

    # The actual proof: an independent session, opened after the app (and
    # its Container/checkpointer) has shut down, sees the real persisted
    # change made through the production PostgresSaver-backed checkpoint
    # resume, not an in-memory one.
    assert _read_follow_up_material_id(pg_database, seeded["source"]["id"]) == seeded["replacement"]["id"]


def test_start_then_reject_leaves_postgres_unchanged_through_the_real_api(pg_database: Database, suffix: str):
    seeded = _seed(pg_database, suffix)
    message = (
        f"Update the replacement material for {seeded['source']['material_code']} "
        f"to {seeded['replacement']['material_code']}."
    )

    app = create_app()
    with TestClient(app) as client:
        start_response = client.post(
            "/api/v1/ontology-update/requests", json={"message": message}, headers=HEADERS
        )
        assert start_response.status_code == 202, start_response.text
        thread_id = start_response.json()["data"]["thread_id"]

        decide_response = client.post(
            f"/api/v1/ontology-update/requests/{thread_id}/decisions",
            json={"decision": "reject"},
            headers=HEADERS,
        )
        assert decide_response.status_code == 200, decide_response.text
        decided = decide_response.json()["data"]
        assert decided["status"] == "rejected"
        assert decided["execution_result"] is None

    assert _read_follow_up_material_id(pg_database, seeded["source"]["id"]) is None


def test_decision_on_an_unknown_thread_id_returns_404_through_the_real_api():
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/ontology-update/requests/thread_ontology_update_does-not-exist/decisions",
            json={"decision": "approve"},
            headers=HEADERS,
        )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "ONTOLOGY_UPDATE_THREAD_NOT_FOUND"
