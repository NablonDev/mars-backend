"""Maps CmirRecord/Material/MaterialMaster/Plant rows into RDF triples
against the shared `mars_ontology.ttl` vocabulary, driven generically by
`app.ontology.config.db_mapping` (`ENTITIES`/`RELATIONSHIPS`).

This is the one place the row -> individual mapping lives, so
`OntologyGraphService`/`OntologyContextService` and `materialize_job` never
drift into different notions of what a CmirRecord's IRI or properties are.
Deliberately pure and deterministic -- no database access, no RDF/OWL
reasoning; IRIs are derived from each row's natural key where one exists
(material_code, plant_code) or its primary key otherwise (cmir_record.id,
material_master.id), so the same row always maps to the same IRI across
rebuilds.

Refactored from four hand-written `_add_*` methods (one hardcoded block per
relationship) to a generic pair of loops over the mapping config -- see
`docs/ontology/ontology-context-layer-design.md` §18 Phase 4 for why: a
future new entity/relationship needs a `db_mapping.py` entry, not a new
method here. The public IRI helper methods (`material_iri`, etc.) and
`build_rdf_triples`'s signature are unchanged, since `OntologyGraphService`
and every existing test call them directly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from rdflib import RDF, Graph, Literal, Namespace
from rdflib.term import URIRef

from app.models.cmir.cmir_record import CmirRecord
from app.models.common.material import Material, MaterialMaster
from app.models.common.plant import Plant
from app.ontology.config.db_mapping import ENTITIES, RELATIONSHIPS, EntityMapping, RelationshipKind

MARS = Namespace("https://ontology.mars-cmir.nablon.ai/")

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _kebab(ontology_class: str) -> str:
    """`MaterialMaster` -> `material-master`, `CmirRecord` -> `cmir-record`
    -- exactly the IRI path segments already in use, derived generically
    from the ontology class name instead of one hardcoded string per
    entity."""
    return _CAMEL_BOUNDARY.sub("-", ontology_class).lower()


class OntologyBuilder:
    """Row -> RDF triple mapping for the CMIR <-> Material Master traceability graph."""

    NS = MARS

    def cmir_record_iri(self, record_id: UUID) -> URIRef:
        return self._entity_iri("CmirRecord", record_id)

    def material_iri(self, material_code: str) -> URIRef:
        return self._entity_iri("Material", material_code)

    def material_master_iri(self, material_master_id: UUID) -> URIRef:
        return self._entity_iri("MaterialMaster", material_master_id)

    def plant_iri(self, plant_code: str) -> URIRef:
        return self._entity_iri("Plant", plant_code)

    def _entity_iri(self, ontology_class: str, key_value: Any) -> URIRef:
        return self.NS[f"{_kebab(ontology_class)}/{key_value}"]

    def build_rdf_triples(
        self,
        graph: Graph,
        *,
        cmir_records: Iterable[CmirRecord],
        materials: Iterable[Material],
        material_masters: Iterable[MaterialMaster],
        plants: Iterable[Plant],
    ) -> None:
        """Populate `graph` with individuals for every row passed in.

        Callers (`materialize_job`) decide scope -- this method only maps
        what it's given, and is safe to call repeatedly against a fresh
        `Graph` for a full rebuild.
        """
        rows_by_entity: dict[str, list[Any]] = {
            "Material": list(materials),
            "Plant": list(plants),
            "MaterialMaster": list(material_masters),
            "CmirRecord": list(cmir_records),
        }

        # Pass 1: type + datatype-property triples for every row, plus two
        # indexes every relationship pass below needs -- id -> IRI (for
        # FOREIGN_KEY/REVERSE_FOREIGN_KEY, which resolve a target row by its
        # primary key) and natural-key-value -> IRI (for VALUE_MATCH, which
        # resolves a target row by a business-key column instead).
        id_to_iri: dict[str, dict[Any, URIRef]] = {}
        key_value_to_iri: dict[str, dict[Any, URIRef]] = {}

        for entity in ENTITIES:
            id_index: dict[Any, URIRef] = {}
            key_index: dict[Any, URIRef] = {}
            for row in rows_by_entity.get(entity.ontology_class, []):
                iri = self._add_entity_row(graph, entity, row)
                id_index[row.id] = iri
                key_index[getattr(row, entity.iri_key_column.key)] = iri
            id_to_iri[entity.ontology_class] = id_index
            key_value_to_iri[entity.ontology_class] = key_index

        # Pass 2: relationship triples -- every row is already in the graph
        # (with a known IRI) by this point, so a relationship pass never
        # needs to worry about ordering between entities.
        for rel in RELATIONSHIPS:
            self._add_relationship_triples(
                graph, rel, rows_by_entity=rows_by_entity, id_to_iri=id_to_iri, key_value_to_iri=key_value_to_iri
            )

    def _add_entity_row(self, graph: Graph, entity: EntityMapping, row: Any) -> URIRef:
        iri = self._entity_iri(entity.ontology_class, getattr(row, entity.iri_key_column.key))
        graph.add((iri, RDF.type, MARS[entity.ontology_class]))
        for prop in entity.properties:
            value = getattr(row, prop.column.key)
            # None (a nullable column with no value, e.g.
            # MaterialMaster.discontinuation_indicator) is skipped -- an
            # absent triple, not a triple with an empty literal. A
            # non-nullable column's "" is still a real value and IS
            # emitted (real dev data has blank CmirRecord.brand/.site on
            # legitimate rows -- skipping those would silently break
            # traceability for them, since OntologyGraphService's SPARQL
            # query requires every one of these triples to match).
            if value is not None:
                graph.add((iri, MARS[prop.ontology_property], Literal(value)))
        return iri

    def _add_relationship_triples(
        self,
        graph: Graph,
        rel,
        *,
        rows_by_entity: dict[str, list[Any]],
        id_to_iri: dict[str, dict[Any, URIRef]],
        key_value_to_iri: dict[str, dict[Any, URIRef]],
    ) -> None:
        predicate = MARS[rel.ontology_property]

        if rel.kind is RelationshipKind.FOREIGN_KEY:
            # fk_column lives on from_entity, pointing at to_entity's PK.
            for row in rows_by_entity.get(rel.from_entity, []):
                fk_value = getattr(row, rel.fk_column.key)
                if fk_value is None:
                    continue
                target_iri = id_to_iri.get(rel.to_entity, {}).get(fk_value)
                if target_iri is not None:
                    from_iri = id_to_iri[rel.from_entity][row.id]
                    graph.add((from_iri, predicate, target_iri))

        elif rel.kind is RelationshipKind.REVERSE_FOREIGN_KEY:
            # fk_column lives on to_entity, pointing back at from_entity's
            # PK -- the semantic edge still runs from_entity -> to_entity,
            # sourced by iterating to_entity's own rows.
            for row in rows_by_entity.get(rel.to_entity, []):
                fk_value = getattr(row, rel.fk_column.key)
                if fk_value is None:
                    continue
                source_iri = id_to_iri.get(rel.from_entity, {}).get(fk_value)
                if source_iri is not None:
                    to_iri = id_to_iri[rel.to_entity][row.id]
                    graph.add((source_iri, predicate, to_iri))

        elif rel.kind is RelationshipKind.VALUE_MATCH:
            target_index = key_value_to_iri.get(rel.to_entity, {})
            for row in rows_by_entity.get(rel.from_entity, []):
                value = getattr(row, rel.value_from_column.key)
                target_iri = target_index.get(value)
                if target_iri is not None:
                    from_iri = id_to_iri[rel.from_entity][row.id]
                    graph.add((from_iri, predicate, target_iri))
