"""Integration test for `app.services.ontology.materialize_job.rebuild`
against a real Postgres instance -- the builder itself is already covered,
DB-free, by tests/unit/services/test_ontology_builder.py.

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a
reachable Postgres instance, same convention as
tests/integration/test_cmir_repository_integration.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.container import Container
from app.db.session import Database
from app.repositories.cmir.cmir_record import CmirRecordRepository
from app.repositories.common.master_data import MasterDataRepository
from app.services.ontology import materialize_job
from app.services.ontology.ontology_builder import MARS


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
            "No reachable Postgres DATABASE_URL configured -- skipping ontology "
            "materialize_job Postgres integration tests."
        )
    db.create_all_tables()
    yield db
    db.dispose()


@pytest.fixture
def suffix() -> str:
    """Unique per test so repeated runs against a shared dev database never collide."""
    return uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _cleanup(pg_database: Database, suffix: str):
    yield
    with pg_database.session() as session:
        session.execute(
            text("DELETE FROM cmir.cmir_record WHERE customer_identity = :customer_identity"),
            {"customer_identity": f"Ontology Test Customer {suffix}"},
        )
        session.execute(
            text("DELETE FROM common.material_master WHERE sap_material_number LIKE :pattern"),
            {"pattern": f"SAP-{suffix}-%"},
        )
        session.execute(
            text("DELETE FROM common.material WHERE material_code LIKE :pattern"),
            {"pattern": f"GRD-{suffix}-%"},
        )


def _seed(pg_database: Database, suffix: str) -> tuple[str, str]:
    """Seeds one discontinued material with a live successor, and one
    current CMIR record resolving to the discontinued material -- enough to
    exercise every object property `OntologyBuilder` asserts. Returns
    (discontinued_code, successor_code)."""
    discontinued_code = f"GRD-{suffix}-OLD"
    successor_code = f"GRD-{suffix}-NEW"

    with pg_database.session() as session:
        master_data = MasterDataRepository(session)
        cmir_records = CmirRecordRepository(session)

        successor = master_data.add_material(successor_code, description="ontology test successor")
        discontinued = master_data.add_material(discontinued_code, description="ontology test discontinued")
        master_data.add_material_master(
            material_id=discontinued["id"],
            sap_material_number=f"SAP-{suffix}-OLD",
            discontinuation_indicator="Y",
            follow_up_material_id=successor["id"],
        )

        cmir_records.supersede_and_insert(
            customer_identity=f"Ontology Test Customer {suffix}",
            target_customer_material_ref="CUST-REF",
            merged={
                "sender_type": "EMAIL",
                "customer_identity": f"Ontology Test Customer {suffix}",
                # The ontology builder matches referencesMaterial on
                # material_identity, not target_grd_code -- this must equal
                # the seeded Material's code for the edge to form.
                "material_identity": discontinued_code,
                "intent_phrase": None,
                "existing_cmir_ref": "",
                "brand": "TestBrand",
                "site": "TestSite",
                "target_grd_code": "UNUSED-BY-ONTOLOGY",
                "target_customer_material_ref": "CUST-REF",
                "effective_date": None,
                "reason": None,
            },
            expected_current_id=None,
        )

    return discontinued_code, successor_code


def test_rebuild_links_cmir_record_through_to_successor_material(pg_database: Database, suffix: str):
    discontinued_code, successor_code = _seed(pg_database, suffix)
    builder = materialize_job._builder

    graph = materialize_job.rebuild(Container.build())

    material_iri = builder.material_iri(discontinued_code)
    successor_iri = builder.material_iri(successor_code)

    # The CMIR record resolves (via referencesMaterial) to the discontinued
    # material, whose MaterialMaster is in turn succeededBy the successor --
    # the two-hop chain a traceability query will walk in a later phase.
    matching_cmir_iris = list(graph.subjects(MARS.referencesMaterial, material_iri))
    assert len(matching_cmir_iris) == 1

    master_iris_for_material = list(graph.objects(material_iri, MARS.hasPlantRecord))
    assert master_iris_for_material, "expected the discontinued material to have a plant record"

    master_iri = master_iris_for_material[0]
    assert (master_iri, MARS.succeededBy, successor_iri) in graph


def test_rebuild_is_idempotent_and_swaps_the_graph_on_container(pg_database: Database, suffix: str):
    _seed(pg_database, suffix)
    container = Container.build()

    first_graph = materialize_job.rebuild(container)
    second_graph = materialize_job.rebuild(container)

    assert len(first_graph) == len(second_graph)
    assert container.get_ontology_graph() is second_graph
