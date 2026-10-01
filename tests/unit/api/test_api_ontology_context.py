"""API tests for the semantic context routes (`GET /ontology/entities/{name}`,
`GET /ontology/entities/{name}/relationships`) -- Phase 1G.

Unlike `test_api_ontology.py`'s traceability routes, `OntologyContextService`
needs no live graph/database at all (only `db_mapping` + a local `.ttl`
parse), so these tests exercise the real service through the real
dependency rather than a fake -- there's nothing worth faking.
"""

from __future__ import annotations


def test_get_entity_context_returns_material_master_schema_context(client):
    response = client.get("/api/v1/ontology/entities/MaterialMaster")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["physical_table"] == "common.material_master"
    assert data["primary_key"] == "id"
    property_names = {p["name"] for p in data["properties"]}
    assert {"sapMaterialNumber", "discontinuationIndicator"} <= property_names


def test_get_entity_context_for_unknown_entity_returns_404(client):
    response = client.get("/api/v1/ontology/entities/NoSuchEntity")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "ONTOLOGY_ENTITY_NOT_FOUND"


def test_get_relationships_reports_succeeded_by_as_a_foreign_key(client):
    response = client.get("/api/v1/ontology/entities/MaterialMaster/relationships")

    assert response.status_code == 200, response.text
    relationships = {r["name"]: r for r in response.json()["data"]}
    succeeded_by = relationships["succeededBy"]
    assert succeeded_by["target_entity"] == "Material"
    assert succeeded_by["kind"] == "foreign_key"
    assert succeeded_by["physical_implementation"] == (
        "common.material_master.follow_up_material_id -> common.material.id"
    )


def test_get_relationships_reports_references_material_as_a_value_match(client):
    response = client.get("/api/v1/ontology/entities/CmirRecord/relationships")

    assert response.status_code == 200, response.text
    relationships = {r["name"]: r for r in response.json()["data"]}
    references_material = relationships["referencesMaterial"]
    assert references_material["target_entity"] == "Material"
    assert references_material["kind"] == "value_match"
    assert references_material["physical_implementation"] == (
        "cmir.cmir_record.material_identity = common.material.material_code"
    )
