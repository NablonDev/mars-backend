"""End-to-end integration test for the ontology-update POC against a real
Postgres instance -- proves the full chain actually works, not just that
its pieces pass in isolation against fakes/SQLite:

    User request -> LangGraph -> OntologyContextService -> real DB reads
    -> OperationProposal -> HITL interrupt -> Command(resume=...)
    -> [approve -> MaterialMasterService -> MasterDataRepository -> real UPDATE]
    -> [reject  -> no write at all]

Each `graph.invoke(...)` call in this file opens its own fresh
`Database.session()` (committed/closed by the context manager on exit),
mirroring how the real application would wire per-call repositories
(`Container.*_repos()`) rather than holding one session open across an
interrupt -- the checkpointer (a `MemorySaver`, shared across both calls
for a given thread_id) is what actually carries state across the pause,
not the DB session. Every assertion about the database reads back through
an *independent* session, proving durability, not just an in-memory view.

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance, same convention as
tests/integration/test_ontology_materialize_job.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.agents.ontology_update.graph import build_ontology_update_graph
from app.agents.ontology_update.nodes import OntologyUpdateNodes
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
            "No reachable Postgres DATABASE_URL configured -- skipping ontology-update "
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
            {"pattern": f"HITL-SAP-{suffix}-%"},
        )
        session.execute(
            text("DELETE FROM common.material WHERE material_code LIKE :pattern"),
            {"pattern": f"HITL-MAT-{suffix}-%"},
        )


def _seed(pg_database: Database, suffix: str) -> dict:
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        source = md.add_material(f"HITL-MAT-{suffix}-DISC")
        replacement = md.add_material(f"HITL-MAT-{suffix}-REPL")
        plant = md.get_or_create_plant(f"HITL-PLANT-{suffix}")
        master = md.add_material_master(
            material_id=source["id"],
            sap_material_number=f"HITL-SAP-{suffix}-1",
            plant_id=plant["id"],
        )
    return {"source": source, "replacement": replacement, "master": master}


def _invoke(pg_database: Database, checkpointer, thread_id: str, payload):
    """One graph.invoke, against its own fresh Session/repositories --
    committed (or rolled back) by Database.session()'s own context manager
    on exit, exactly as the real application would wire a per-call
    Container.*_repos()."""
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        service = MaterialMasterService(master_data_repository=md)
        nodes = OntologyUpdateNodes(
            context_service=OntologyContextService(), master_data_repository=md, material_master_service=service
        )
        graph = build_ontology_update_graph(nodes, checkpointer, _NoOpTraceRepo())
        return graph.invoke(payload, config={"configurable": {"thread_id": thread_id}})


def _read_follow_up_material_id(pg_database: Database, material_id) -> object:
    """Independent session read-back -- proves durability, not just an
    in-memory view from the session that made the write."""
    with pg_database.session() as session:
        md = MasterDataRepository(session)
        rows = md.list_material_masters_for_material(material_id)
    return rows[0]["follow_up_material_id"]


def test_approve_performs_the_real_database_update(pg_database: Database, suffix: str):
    seeded = _seed(pg_database, suffix)
    checkpointer = MemorySaver()
    thread_id = f"hitl-approve-{suffix}"
    request = f"Update the replacement material for {seeded['source']['material_code']} to {seeded['replacement']['material_code']}."

    state = _invoke(pg_database, checkpointer, thread_id, {"user_request": request})
    assert INTERRUPT_KEY in state, state
    payload = state[INTERRUPT_KEY][0].value
    assert payload["reason"] == "material_master_update_approval"
    assert payload["proposal"]["relationship"]["name"] == "succeededBy"

    # Not written yet -- independent read confirms it's still unset.
    assert _read_follow_up_material_id(pg_database, seeded["source"]["id"]) is None

    resumed = _invoke(pg_database, checkpointer, thread_id, Command(resume={"decision": "approve"}))
    assert INTERRUPT_KEY not in resumed
    assert resumed["approval_status"] == "approve"
    assert resumed["execution_result"]["material_master_id"] == str(seeded["master"]["id"])

    # The actual proof: an independent session, opened after both calls
    # above have closed/committed, sees the real persisted change.
    assert _read_follow_up_material_id(pg_database, seeded["source"]["id"]) == seeded["replacement"]["id"]


def test_reject_leaves_the_database_completely_unchanged(pg_database: Database, suffix: str):
    seeded = _seed(pg_database, suffix)
    checkpointer = MemorySaver()
    thread_id = f"hitl-reject-{suffix}"
    request = f"Update the replacement material for {seeded['source']['material_code']} to {seeded['replacement']['material_code']}."

    state = _invoke(pg_database, checkpointer, thread_id, {"user_request": request})
    assert INTERRUPT_KEY in state, state

    resumed = _invoke(pg_database, checkpointer, thread_id, Command(resume={"decision": "reject"}))
    assert INTERRUPT_KEY not in resumed
    assert resumed["approval_status"] == "reject"
    assert "execution_result" not in resumed

    assert _read_follow_up_material_id(pg_database, seeded["source"]["id"]) is None
