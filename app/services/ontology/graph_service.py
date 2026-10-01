"""Read-side traceability/lookup over the CMIR <-> Material Master graph
(`app.core.container.Container.get_ontology_graph`) -- component B of the
ontology plan. No reasoning/inference here: every query below runs against
whatever `rdflib.Graph` `materialize_job.rebuild` last swapped in, via plain
SPARQL, and returns in well under the time any reasoner invocation would
take -- that split (cheap reads here, an expensive reasoner elsewhere) is
the whole reason this class and a future reasoning service stay separate.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from rdflib import Graph
from rdflib.query import ResultRow
from rdflib.term import Identifier, URIRef

from app.schemas.ontology.traceability import (
    SuccessorChainLink,
    SuccessorChainResult,
    TraceabilityCmirRecord,
    TraceabilityResult,
)
from app.services.ontology.ontology_builder import MARS, OntologyBuilder

_TRACEABILITY_QUERY = """
PREFIX mars: <https://ontology.mars-cmir.nablon.ai/>
SELECT ?cmir ?customerIdentity ?targetRef ?brand ?site WHERE {
  ?cmir mars:referencesMaterial ?material ;
        mars:customerIdentity ?customerIdentity ;
        mars:targetCustomerMaterialRef ?targetRef ;
        mars:brand ?brand ;
        mars:site ?site .
}
"""

# One hop of the successor chain: from a MaterialMaster, follow
# succeededBy to the Material that replaces it, then hasPlantRecord
# (Material -> MaterialMaster) forward onto that Material's own
# MaterialMaster row. Composed as a single SPARQL 1.1 property path (not
# two separate lookups) so the path traversal itself is the query's job,
# not application code walking the graph.
_CHAIN_STEP_QUERY = """
PREFIX mars: <https://ontology.mars-cmir.nablon.ai/>
SELECT ?next WHERE {
  ?start (mars:succeededBy/mars:hasPlantRecord) ?next .
}
"""

# Guards a malformed/cyclic graph (e.g. a bad "follow_up_material_id" data
# fix that points back at an earlier material) from spinning forever --
# real successor chains are a handful of hops at most.
_MAX_CHAIN_HOPS = 50


def _cmir_record_id_from_iri(iri: Identifier) -> str:
    return str(iri).rsplit("/", 1)[-1]


def _select_rows(result: object) -> Iterable[ResultRow]:
    """Narrows a SPARQL query's result type to the `ResultRow` iterable a
    `SELECT` always produces -- every query in this module is a `SELECT`,
    never `ASK`/`CONSTRUCT`, so the `bool`/graph alternatives `Graph.query`'s
    return type otherwise allows for never actually occur here."""
    return result  # type: ignore[return-value]


class OntologyGraphService:
    """SPARQL-backed traceability queries over a materialized `Graph`."""

    def __init__(self, *, graph: Graph) -> None:
        self._graph = graph
        self._builder = OntologyBuilder()

    def find_cmir_records_for_material(self, material_code: str) -> TraceabilityResult:
        material_iri = self._builder.material_iri(material_code)
        rows = _select_rows(self._graph.query(_TRACEABILITY_QUERY, initBindings={"material": material_iri}))

        records = [
            TraceabilityCmirRecord(
                cmir_record_id=_cmir_record_id_from_iri(row[0]),
                customer_identity=str(row[1]),
                target_customer_material_ref=str(row[2]),
                brand=str(row[3]),
                site=str(row[4]),
            )
            for row in rows
        ]
        return TraceabilityResult(material_code=material_code, cmir_records=records)

    def successor_chain(self, material_master_id: UUID) -> SuccessorChainResult:
        current: URIRef = self._builder.material_master_iri(material_master_id)
        visited = {current}
        chain: list[SuccessorChainLink] = []

        for _ in range(_MAX_CHAIN_HOPS):
            rows = list(_select_rows(self._graph.query(_CHAIN_STEP_QUERY, initBindings={"start": current})))
            if not rows:
                break
            next_iri = URIRef(rows[0][0])
            if next_iri in visited:
                break
            visited.add(next_iri)
            chain.append(self._chain_link_for_master(next_iri))
            current = next_iri

        return SuccessorChainResult(material_master_id=str(material_master_id), chain=chain)

    def _chain_link_for_master(self, master_iri: URIRef) -> SuccessorChainLink:
        material_iri = self._graph.value(predicate=MARS.hasPlantRecord, object=master_iri)
        material_code = ""
        if material_iri is not None:
            code = self._graph.value(material_iri, MARS.materialCode)
            material_code = str(code) if code is not None else ""

        discontinuation = self._graph.value(master_iri, MARS.discontinuationIndicator)
        return SuccessorChainLink(
            material_code=material_code,
            discontinuation_indicator=str(discontinuation) if discontinuation is not None else None,
        )
