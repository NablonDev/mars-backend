"""End-to-end integration test for the ontology-insert POC against a real
Postgres instance -- proves the full chain works, not just that its pieces
pass in isolation against fakes/SQLite:

    User request -> LangGraph -> OntologyContextService -> real DB reads
    -> [missing-fields interrupt -> resume with details]* -> InsertOperationProposal
    -> approval interrupt -> Command(resume=...)
    -> [approve -> MaterialMasterService -> MasterDataRepository -> real INSERTs]
    -> [reject  -> no write at all]

Mirrors `tests/integration/test_ontology_update_hitl_postgres.py`'s
convention exactly: each `graph.invoke(...)` call opens its own fresh
`Database.session()`, a `MemorySaver` carries state across the interrupt,
and every assertion about the database reads back through an
*independent* session, proving durability.

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.agents.ontology_insert.graph import build_ontology_insert_graph
from app.agents.ontology_insert.nodes import OntologyInsertNodes
from app.core.config import get_settings
from app.db.session import Database
from app.repositories.common.master_data import MasterDataRepository
from app.services.common.material_master_service import MaterialMasterService
from app.services.ontology.context_service import OntologyContextService

INTERRUPT_KEY = "__interrupt__"


class _NoOpTraceRepo:
    def log(self, *args, **kwargs):
        pass


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
            "HITL Postgres integration tests."
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
            {"pattern": f"HITLINS-SAP-{suffix}-%"},
        )
        session.execute(
            text("DELETE FROM common.material WHERE material_code LIKE :pattern"),
            {"pattern": f"HITLINS-MAT-{suffix}-%"},
        )
        session.execute(
            text("DELETE FROM common.plant WHERE plant_code LIKE :pattern"),
            {"pattern": f"HITLINS-PLANT-{suffix}-%"},
        )


def _invoke(pg_database: Database, checkpointer, thread_id: str, payload):
    """One graph.invoke, against its own fresh Session/repositories --
    committed (or rolled back) by Database.session()'s own context manager
    on exit, exactly as the real application would wire a per-call
    Container.*_repos()."""
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        service = MaterialMasterService(master_data_repository=md)
        nodes = OntologyInsertNodes(
            context_service=OntologyContextService(), master_data_repository=md, material_master_service=service
        )
        graph = build_ontology_insert_graph(nodes, checkpointer, _NoOpTraceRepo())
        return graph.invoke(payload, config={"configurable": {"thread_id": thread_id}})


def _read_material_master(pg_database: Database, material_code: str) -> dict | None:
    """Independent session read-back -- proves durability, not just an
    in-memory view from the session that made the write."""
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        material = md.get_material_by_code(material_code)
        if material is None:
            return None
        masters = md.list_material_masters_for_material(material["id"])
        return masters[0] if masters else None


def test_approve_creates_the_material_plant_and_material_master_rows(pg_database: Database, suffix: str):
    checkpointer = MemorySaver()
    thread_id = f"hitl-insert-approve-{suffix}"
    material_code = f"HITLINS-MAT-{suffix}-1"
    plant_code = f"HITLINS-PLANT-{suffix}-1"
    sap_number = f"HITLINS-SAP-{suffix}-1"
    request = f"Create material {material_code} at plant {plant_code} with SAP number {sap_number}."

    state = _invoke(pg_database, checkpointer, thread_id, {"user_request": request})
    assert INTERRUPT_KEY in state, state
    payload = state[INTERRUPT_KEY][0].value
    assert payload["reason"] == "material_master_insert_approval"
    assert payload["proposal"]["relationship"]["target_entity"] == "Plant"

    # Not written yet -- independent read confirms nothing exists.
    assert _read_material_master(pg_database, material_code) is None

    resumed = _invoke(pg_database, checkpointer, thread_id, Command(resume={"decision": "approve"}))
    assert INTERRUPT_KEY not in resumed
    assert resumed["approval_status"] == "approve"
    assert "execution_result" in resumed

    master = _read_material_master(pg_database, material_code)
    assert master is not None
    assert master["sap_material_number"] == sap_number


def test_reject_leaves_the_database_completely_unchanged(pg_database: Database, suffix: str):
    checkpointer = MemorySaver()
    thread_id = f"hitl-insert-reject-{suffix}"
    material_code = f"HITLINS-MAT-{suffix}-2"
    plant_code = f"HITLINS-PLANT-{suffix}-2"
    sap_number = f"HITLINS-SAP-{suffix}-2"
    request = f"Create material {material_code} at plant {plant_code} with SAP number {sap_number}."

    state = _invoke(pg_database, checkpointer, thread_id, {"user_request": request})
    assert INTERRUPT_KEY in state, state

    resumed = _invoke(pg_database, checkpointer, thread_id, Command(resume={"decision": "reject"}))
    assert INTERRUPT_KEY not in resumed
    assert resumed["approval_status"] == "reject"
    assert "execution_result" not in resumed

    assert _read_material_master(pg_database, material_code) is None


def test_missing_details_round_trip_then_approve(pg_database: Database, suffix: str):
    checkpointer = MemorySaver()
    thread_id = f"hitl-insert-details-{suffix}"
    material_code = f"HITLINS-MAT-{suffix}-3"
    plant_code = f"HITLINS-PLANT-{suffix}-3"
    sap_number = f"HITLINS-SAP-{suffix}-3"

    state = _invoke(pg_database, checkpointer, thread_id, {"user_request": f"Create material {material_code}."})
    assert INTERRUPT_KEY in state, state
    payload = state[INTERRUPT_KEY][0].value
    assert payload["reason"] == "missing_required_fields"
    assert set(payload["missing_fields"]) == {"plant_code", "sap_material_number"}

    resumed = _invoke(
        pg_database,
        checkpointer,
        thread_id,
        Command(resume={"details": {"plant_code": plant_code, "sap_material_number": sap_number}}),
    )
    assert INTERRUPT_KEY in resumed
    approval_payload = resumed[INTERRUPT_KEY][0].value
    assert approval_payload["reason"] == "material_master_insert_approval"

    approved = _invoke(pg_database, checkpointer, thread_id, Command(resume={"decision": "approve"}))
    assert approved["approval_status"] == "approve"

    master = _read_material_master(pg_database, material_code)
    assert master is not None
    assert master["sap_material_number"] == sap_number


def test_a_second_insert_for_the_same_material_code_after_approval_is_rejected(pg_database: Database, suffix: str):
    material_code = f"HITLINS-MAT-{suffix}-4"
    plant_code = f"HITLINS-PLANT-{suffix}-4"
    request = (
        f"Create material {material_code} at plant {plant_code} with SAP number HITLINS-SAP-{suffix}-4a."
    )

    first_checkpointer = MemorySaver()
    first_thread = f"hitl-insert-dup-first-{suffix}"
    _invoke(pg_database, first_checkpointer, first_thread, {"user_request": request})
    approved = _invoke(pg_database, first_checkpointer, first_thread, Command(resume={"decision": "approve"}))
    assert approved["approval_status"] == "approve"

    second_checkpointer = MemorySaver()
    second_thread = f"hitl-insert-dup-second-{suffix}"
    second_request = (
        f"Create material {material_code} at plant {plant_code} with SAP number HITLINS-SAP-{suffix}-4b."
    )
    state = _invoke(pg_database, second_checkpointer, second_thread, {"user_request": second_request})
    assert INTERRUPT_KEY not in state
    assert state["error"]["error_type"] == "material_already_exists"
