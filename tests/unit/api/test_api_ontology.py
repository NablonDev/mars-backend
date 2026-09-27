"""API tests for `app/api/v1/ontology.py` (`GET .../traceability`,
`GET .../successor-chain`) -- component B of the ontology plan.

Overrides `get_ontology_graph_service` directly via `dependency_overrides`
(rather than an `app.state`-memoized fixture like `cmir_run_service`/
`po_validation_service` in tests/conftest.py) since the dependency itself
takes no `Request` and is cheap/stateless to construct per call -- see its
docstring in `app/api/dependencies.py`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.dependencies import get_ontology_graph_service
from app.schemas.ontology.traceability import (
    SuccessorChainLink,
    SuccessorChainResult,
    TraceabilityCmirRecord,
    TraceabilityResult,
)


class FakeOntologyGraphService:
    def __init__(self, *, traceability_result=None, chain_result=None) -> None:
        self._traceability_result = traceability_result
        self._chain_result = chain_result

    def find_cmir_records_for_material(self, material_code: str) -> TraceabilityResult:
        if self._traceability_result is not None:
            return self._traceability_result
        return TraceabilityResult(material_code=material_code, cmir_records=[])

    def successor_chain(self, material_master_id) -> SuccessorChainResult:
        if self._chain_result is not None:
            return self._chain_result
        return SuccessorChainResult(material_master_id=str(material_master_id), chain=[])


@pytest.fixture
def ontology_graph_service() -> FakeOntologyGraphService:
    return FakeOntologyGraphService()


@pytest.fixture(autouse=True)
def _override_ontology_dependency(app, ontology_graph_service: FakeOntologyGraphService):
    app.dependency_overrides[get_ontology_graph_service] = lambda: ontology_graph_service


def test_get_material_traceability_returns_envelope_wrapped_result(client):
    response = client.get("/api/v1/ontology/materials/GRD-88213/traceability")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["data"]["material_code"] == "GRD-88213"
    assert body["data"]["cmir_records"] == []


def test_get_material_traceability_returns_matching_cmir_records(client):
    result = TraceabilityResult(
        material_code="GRD-88213",
        cmir_records=[
            TraceabilityCmirRecord(
                cmir_record_id=str(uuid4()),
                customer_identity="Acme Foods",
                target_customer_material_ref="ACM-4471",
                brand="AcmePlast",
                site="Site 12",
            )
        ],
    )
    client.app.dependency_overrides[get_ontology_graph_service] = lambda: FakeOntologyGraphService(
        traceability_result=result
    )

    response = client.get("/api/v1/ontology/materials/GRD-88213/traceability")

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["cmir_records"][0]["customer_identity"] == "Acme Foods"


def test_get_successor_chain_returns_envelope_wrapped_result_for_empty_graph(client):
    material_master_id = uuid4()

    response = client.get(f"/api/v1/ontology/material-masters/{material_master_id}/successor-chain")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["material_master_id"] == str(material_master_id)
    assert body["data"]["chain"] == []


def test_get_successor_chain_returns_multi_hop_chain(client):
    material_master_id = uuid4()
    result = SuccessorChainResult(
        material_master_id=str(material_master_id),
        chain=[
            SuccessorChainLink(material_code="GRD-MID", discontinuation_indicator="Y"),
            SuccessorChainLink(material_code="GRD-LIVE", discontinuation_indicator=None),
        ],
    )
    client.app.dependency_overrides[get_ontology_graph_service] = lambda: FakeOntologyGraphService(
        chain_result=result
    )

    response = client.get(f"/api/v1/ontology/material-masters/{material_master_id}/successor-chain")

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert [link["material_code"] for link in body["chain"]] == ["GRD-MID", "GRD-LIVE"]
    assert body["chain"][0]["discontinuation_indicator"] == "Y"
    assert body["chain"][1]["discontinuation_indicator"] is None


def test_get_successor_chain_rejects_a_non_uuid_material_master_id(client):
    response = client.get("/api/v1/ontology/material-masters/not-a-uuid/successor-chain")

    assert response.status_code == 422
