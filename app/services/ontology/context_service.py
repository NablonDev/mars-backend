"""Semantic context layer, Phase 1G -- answers "what is this entity /
what does this relationship mean" from `app.ontology.config.db_mapping` and
`mars_ontology.ttl`. Read-only, no database connection, no RDF/SPARQL
involved (this is metadata about the vocabulary + mapping themselves, not a
query over instance data -- contrast with `OntologyGraphService`, which
does query the live graph).

This is the interface a future HITL Agent is meant to consume instead of
touching raw RDF/SPARQL or the mapping module directly -- see
`docs/ontology/semantic-context-layer-design.md` §20/§22. No agent exists
yet; this service has no agent-specific code in it, only entity/relationship
context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from rdflib import RDFS, Graph
from sqlalchemy import Column

from app.core.exceptions import NotFoundError
from app.ontology.config.db_mapping import (
    ENTITIES,
    RELATIONSHIPS,
    EntityMapping,
    RelationshipKind,
    RelationshipMapping,
)
from app.schemas.ontology.context import EntityContext, PropertyContext, RelationshipContext

_TTL_PATH = Path(__file__).resolve().parents[2] / "ontology" / "schema" / "mars_ontology.ttl"

_DATATYPE_LABELS: dict[type, str] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
}


def _column_of(attr) -> Column:
    return attr.expression


def _datatype_label(column: Column) -> str:
    python_type = column.type.python_type
    return _DATATYPE_LABELS.get(python_type, python_type.__name__)


def _qualified_table_name(column: Column | Any) -> str:
    table = column.table
    return f"{table.schema}.{table.name}" if table.schema else table.name


def _entity_by_name(entity: str) -> EntityMapping:
    match = next((e for e in ENTITIES if e.ontology_class == entity), None)
    if match is None:
        raise NotFoundError(
            code="ONTOLOGY_ENTITY_NOT_FOUND",
            message=f"No ontology entity named {entity!r}. Known entities: "
            f"{sorted(e.ontology_class for e in ENTITIES)}.",
        )
    return match


def _physical_implementation(
    rel: RelationshipMapping, from_entity: EntityMapping, to_entity: EntityMapping
) -> str:
    if rel.kind is RelationshipKind.VALUE_MATCH:
        from_col = _column_of(rel.value_from_column)
        to_col = _column_of(rel.value_to_column)
        return f"{_qualified_table_name(from_col)}.{from_col.name} = {_qualified_table_name(to_col)}.{to_col.name}"

    # FOREIGN_KEY: fk_column lives on from_entity. REVERSE_FOREIGN_KEY:
    # fk_column lives on to_entity, pointing back at from_entity -- see
    # RelationshipMapping's own docstring in db_mapping.py.
    fk_col = _column_of(rel.fk_column)
    target = to_entity if rel.kind is RelationshipKind.FOREIGN_KEY else from_entity
    target_pk = next(iter(target.model.__table__.primary_key))
    return f"{_qualified_table_name(fk_col)}.{fk_col.name} -> {_qualified_table_name(target_pk)}.{target_pk.name}"


class OntologyContextService:
    """Answers schema/relationship questions from `db_mapping` + the
    vocabulary file -- never touches Postgres or the live RDF graph.
    Cheap enough to construct fresh per call (it parses one small local
    `.ttl` file); no state worth memoizing."""

    def __init__(self, *, ttl_path: Path = _TTL_PATH) -> None:
        self._vocab = Graph()
        self._vocab.parse(ttl_path, format="turtle")

    def _comment_for(self, local_name: str) -> str:
        term = next(
            (s for s in self._vocab.subjects() if str(s).rsplit("/", 1)[-1] == local_name),
            None,
        )
        if term is None:
            return ""
        comment = self._vocab.value(term, RDFS.comment)
        return str(comment) if comment is not None else ""

    def get_entity_context(self, entity: str) -> EntityContext:
        mapping = _entity_by_name(entity)
        table = mapping.model.__table__
        pk_column = next(iter(table.primary_key))

        properties = [
            PropertyContext(
                name=prop.ontology_property,
                column=_column_of(prop.column).name,
                datatype=_datatype_label(_column_of(prop.column)),
                required=not _column_of(prop.column).nullable,
            )
            for prop in mapping.properties
        ]

        return EntityContext(
            entity=mapping.ontology_class,
            physical_table=_qualified_table_name(pk_column),
            primary_key=pk_column.name,
            description=self._comment_for(mapping.ontology_class),
            properties=properties,
        )

    def get_relationships(self, entity: str) -> list[RelationshipContext]:
        _entity_by_name(entity)  # raises NotFoundError for an unknown entity
        results = []
        for rel in RELATIONSHIPS:
            if rel.from_entity != entity:
                continue
            from_mapping = _entity_by_name(rel.from_entity)
            to_mapping = _entity_by_name(rel.to_entity)
            results.append(
                RelationshipContext(
                    name=rel.ontology_property,
                    target_entity=rel.to_entity,
                    kind=rel.kind,
                    physical_implementation=_physical_implementation(rel, from_mapping, to_mapping),
                    description=self._comment_for(rel.ontology_property),
                )
            )
        return results

    def get_operation_context(self, entity: str, operation: str) -> NoReturn:
        """Not implemented in Phase 1 -- deliberately.

        Answering "what context is required before a CREATE/UPDATE/DELETE
        on this entity" needs a registry of which service/repository method
        owns writes for each entity, which nothing in this codebase has
        today (flagged as an open question in both design documents). Per
        this task's own instruction not to over-engineer or silently invent
        behavior, this raises rather than returning a fabricated answer.
        """
        raise NotImplementedError(
            "get_operation_context is not implemented in Phase 1 -- no canonical "
            "'which service owns writes for this entity' registry exists yet. "
            "See docs/ontology/semantic-context-layer-design.md §31, item 2."
        )
