"""End-to-end integration test for the ontology-insert POC's HTTP API
(`app/api/v1/ontology_insert.py`), against a real Postgres instance and the
*production* composition root -- mirrors
`tests/integration/test_ontology_update_api_postgres.py`'s convention:

    HTTP request -> FastAPI route -> OntologyInsertRunService
    -> Container.ontology_insert_unit_of_work -> the real `PostgresSaver`
    checkpointer -> LangGraph -> [approve -> MaterialMasterService ->
    MasterDataRepository -> real INSERTs] / [reject -> no write]

`with TestClient(app) as client:` runs `app.main`'s real lifespan, so
`Container.build()` opens the actual `PostgresSaver` checkpointer -- the
same one CMIR/PO-validation/ontology-update use -- proving this graph's
checkpoints (including its extra missing-details interrupt/resume round
trip) durably survive through the production persistence layer.

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance.
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
            "No reachable Postgres DATABASE_URL configured -- skipping ontology-insert "
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
            {"pattern": f"HITLINSAPI-SAP-{suffix}-%"},
        )
        session.execute(
            text("DELETE FROM common.material WHERE material_code LIKE :pattern"),
            {"pattern": f"HITLINSAPI-MAT-{suffix}-%"},
        )
        session.execute(
            text("DELETE FROM common.plant WHERE plant_code LIKE :pattern"),
            {"pattern": f"HITLINSAPI-PLANT-{suffix}-%"},
        )


@pytest.fixture(autouse=True)
def _reset_container():
    """`Container` is a process-wide singleton -- reset it before/after
    each test so this module's real lifespan always builds its own
    Container against `pg_database`, and never leaks a Postgres-backed
    Container into unrelated tests that run afterward in the same
    process."""
    Container.close()
    yield
    Container.close()


def _read_material_master(pg_database: Database, material_code: str) -> dict | None:
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        material = md.get_material_by_code(material_code)
        if material is None:
            return None
        masters = md.list_material_masters_for_material(material["id"])
        return masters[0] if masters else None


def test_start_then_approve_creates_the_rows_through_the_real_api(pg_database: Database, suffix: str):
    material_code = f"HITLINSAPI-MAT-{suffix}-1"
    plant_code = f"HITLINSAPI-PLANT-{suffix}-1"
    sap_number = f"HITLINSAPI-SAP-{suffix}-1"
    message = f"Create material {material_code} at plant {plant_code} with SAP number {sap_number}."

    app = create_app()
    with TestClient(app) as client:
        start_response = client.post(
            "/api/v1/ontology-insert/requests", json={"message": message}, headers=HEADERS
        )
        assert start_response.status_code == 202, start_response.text
        started = start_response.json()["data"]
        assert started["status"] == "awaiting_approval"
        assert started["proposal"]["material"]["material_code"] == material_code
        thread_id = started["thread_id"]

        assert _read_material_master(pg_database, material_code) is None

        decide_response = client.post(
            f"/api/v1/ontology-insert/requests/{thread_id}/decisions",
            json={"decision": "approve"},
            headers=HEADERS,
        )
        assert decide_response.status_code == 200, decide_response.text
        decided = decide_response.json()["data"]
        assert decided["status"] == "completed"

    master = _read_material_master(pg_database, material_code)
    assert master is not None
    assert master["sap_material_number"] == sap_number


def test_start_then_reject_leaves_postgres_unchanged_through_the_real_api(pg_database: Database, suffix: str):
    material_code = f"HITLINSAPI-MAT-{suffix}-2"
    plant_code = f"HITLINSAPI-PLANT-{suffix}-2"
    sap_number = f"HITLINSAPI-SAP-{suffix}-2"
    message = f"Create material {material_code} at plant {plant_code} with SAP number {sap_number}."

    app = create_app()
    with TestClient(app) as client:
        start_response = client.post(
            "/api/v1/ontology-insert/requests", json={"message": message}, headers=HEADERS
        )
        assert start_response.status_code == 202, start_response.text
        thread_id = start_response.json()["data"]["thread_id"]

        decide_response = client.post(
            f"/api/v1/ontology-insert/requests/{thread_id}/decisions",
            json={"decision": "reject"},
            headers=HEADERS,
        )
        assert decide_response.status_code == 200, decide_response.text
        assert decide_response.json()["data"]["status"] == "rejected"

    assert _read_material_master(pg_database, material_code) is None


def test_start_with_missing_details_then_supply_them_and_approve_through_the_real_api(
    pg_database: Database, suffix: str
):
    material_code = f"HITLINSAPI-MAT-{suffix}-3"
    plant_code = f"HITLINSAPI-PLANT-{suffix}-3"
    sap_number = f"HITLINSAPI-SAP-{suffix}-3"

    app = create_app()
    with TestClient(app) as client:
        start_response = client.post(
            "/api/v1/ontology-insert/requests",
            json={"message": f"Create material {material_code}."},
            headers=HEADERS,
        )
        assert start_response.status_code == 202, start_response.text
        started = start_response.json()["data"]
        assert started["status"] == "awaiting_details"
        assert set(started["missing_fields"]) == {"plant_code", "sap_material_number"}
        thread_id = started["thread_id"]

        details_response = client.post(
            f"/api/v1/ontology-insert/requests/{thread_id}/decisions",
            json={"details": {"plant_code": plant_code, "sap_material_number": sap_number}},
            headers=HEADERS,
        )
        assert details_response.status_code == 200, details_response.text
        assert details_response.json()["data"]["status"] == "awaiting_approval"

        approve_response = client.post(
            f"/api/v1/ontology-insert/requests/{thread_id}/decisions",
            json={"decision": "approve"},
            headers=HEADERS,
        )
        assert approve_response.status_code == 200, approve_response.text
        assert approve_response.json()["data"]["status"] == "completed"

    master = _read_material_master(pg_database, material_code)
    assert master is not None
    assert master["sap_material_number"] == sap_number


def test_decision_on_an_unknown_thread_id_returns_404_through_the_real_api():
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/ontology-insert/requests/thread_ontology_insert_does-not-exist/decisions",
            json={"decision": "approve"},
            headers=HEADERS,
        )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "ONTOLOGY_INSERT_THREAD_NOT_FOUND"
