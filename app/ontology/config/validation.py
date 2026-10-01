"""Validates `app.ontology.config.db_mapping` against the real SQLAlchemy
schema and the `mars_ontology.ttl` vocabulary.

Needs **no live Postgres connection** -- every check here reads either
`Base.metadata`/the ORM models directly (populated purely by importing
them, already true of every existing unit test) or a local file parse of
`mars_ontology.ttl`. This is what makes it safe to call unconditionally at
process startup, before anything has connected to the database, and cheap
enough to call again on every `materialize_job.rebuild()` cycle.

Callers must never publish a graph built while `validate_mapping()` raises
-- see `app/services/ontology/materialize_job.py` and `app/main.py`'s
`lifespan` for the two call sites and what each does on failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rdflib import RDF, Graph
from rdflib.namespace import OWL
from sqlalchemy import Column

from app.ontology.config.db_mapping import (
    ENTITIES,
    RELATIONSHIPS,
    EntityMapping,
    RelationshipKind,
    RelationshipMapping,
)

_TTL_PATH = Path(__file__).resolve().parents[1] / "schema" / "mars_ontology.ttl"


class MappingValidationError(Exception):
    """Raised when `db_mapping` no longer matches the real schema or
    vocabulary -- e.g. a mapped column was renamed/removed, or a mapped
    relationship's `ontology_property` isn't declared in `mars_ontology.ttl`.
    The message lists every problem found, not just the first."""


@dataclass(frozen=True)
class ValidationIssue:
    scope: str
    message: str


def _local_name(iri: object) -> str:
    return str(iri).rsplit("/", 1)[-1]


def _entity_by_name(name: str) -> EntityMapping | None:
    return next((e for e in ENTITIES if e.ontology_class == name), None)


def _column_of(attr) -> Column:
    """The underlying SQLAlchemy `Column` for a mapped class attribute
    (`Material.material_code.expression` for a plain `mapped_column`)."""
    return attr.expression


def _validate_entity(entity: EntityMapping, declared_classes: set[str], declared_properties: set[str],
                      issues: list[ValidationIssue]) -> None:
    scope = f"entity:{entity.ontology_class}"
    if entity.ontology_class not in declared_classes:
        issues.append(ValidationIssue(scope, "not declared as an owl:Class in mars_ontology.ttl"))

    table = entity.model.__table__
    key_column = _column_of(entity.iri_key_column)
    if key_column.table is not table:
        issues.append(ValidationIssue(scope, f"iri_key_column {key_column} does not belong to {table}"))
    elif key_column.name not in table.columns:
        issues.append(ValidationIssue(scope, f"iri_key_column {key_column.name!r} not found on {table}"))

    for prop in entity.properties:
        prop_scope = f"{scope}.{prop.ontology_property}"
        if prop.ontology_property not in declared_properties:
            issues.append(ValidationIssue(prop_scope, "not declared as an owl:DatatypeProperty in mars_ontology.ttl"))
        column = _column_of(prop.column)
        if column.table is not table:
            issues.append(ValidationIssue(prop_scope, f"column {column} does not belong to {table}"))
        elif column.name not in table.columns:
            issues.append(ValidationIssue(prop_scope, f"column {column.name!r} not found on {table}"))


def _validate_foreign_key(rel: RelationshipMapping, *, owner: EntityMapping, target: EntityMapping,
                           issues: list[ValidationIssue]) -> None:
    scope = f"relationship:{rel.ontology_property}"
    if rel.fk_column is None:
        issues.append(ValidationIssue(scope, f"{rel.kind.value} requires fk_column, none given"))
        return

    column = _column_of(rel.fk_column)
    owner_table = owner.model.__table__
    if column.table is not owner_table:
        issues.append(
            ValidationIssue(scope, f"fk_column {column} does not belong to {owner.ontology_class}'s table {owner_table}")
        )
        return

    if not column.foreign_keys:
        issues.append(ValidationIssue(scope, f"column {column.name!r} on {owner_table} has no ForeignKey constraint"))
        return

    target_table = target.model.__table__
    fk_targets = {fk.column.table for fk in column.foreign_keys}
    if target_table not in fk_targets:
        actual = ", ".join(str(t) for t in fk_targets)
        issues.append(
            ValidationIssue(
                scope,
                f"column {column.name!r}'s ForeignKey points at [{actual}], expected {target_table} "
                f"(to_entity={target.ontology_class!r})",
            )
        )


def _validate_value_match(rel: RelationshipMapping, *, from_entity: EntityMapping, to_entity: EntityMapping,
                           issues: list[ValidationIssue]) -> None:
    scope = f"relationship:{rel.ontology_property}"
    if rel.value_from_column is None or rel.value_to_column is None:
        issues.append(ValidationIssue(scope, "value_match requires both value_from_column and value_to_column"))
        return

    from_column = _column_of(rel.value_from_column)
    to_column = _column_of(rel.value_to_column)

    if from_column.table is not from_entity.model.__table__:
        issues.append(
            ValidationIssue(scope, f"value_from_column {from_column} does not belong to {from_entity.ontology_class}")
        )
    if to_column.table is not to_entity.model.__table__:
        issues.append(
            ValidationIssue(scope, f"value_to_column {to_column} does not belong to {to_entity.ontology_class}")
        )

    from_type = from_column.type.python_type
    to_type = to_column.type.python_type
    if from_type is not to_type:
        issues.append(
            ValidationIssue(
                scope,
                f"value_from_column {from_column} (python type {from_type.__name__}) and value_to_column "
                f"{to_column} (python type {to_type.__name__}) are not datatype-compatible for a value match",
            )
        )


def _validate_relationship(rel: RelationshipMapping, declared_properties: set[str],
                            issues: list[ValidationIssue]) -> None:
    scope = f"relationship:{rel.ontology_property}"
    if rel.ontology_property not in declared_properties:
        issues.append(ValidationIssue(scope, "not declared as an owl:ObjectProperty in mars_ontology.ttl"))

    from_entity = _entity_by_name(rel.from_entity)
    to_entity = _entity_by_name(rel.to_entity)
    if from_entity is None:
        issues.append(ValidationIssue(scope, f"from_entity {rel.from_entity!r} has no EntityMapping"))
    if to_entity is None:
        issues.append(ValidationIssue(scope, f"to_entity {rel.to_entity!r} has no EntityMapping"))
    if from_entity is None or to_entity is None:
        return

    if rel.kind is RelationshipKind.FOREIGN_KEY:
        _validate_foreign_key(rel, owner=from_entity, target=to_entity, issues=issues)
    elif rel.kind is RelationshipKind.REVERSE_FOREIGN_KEY:
        # The FK column physically lives on to_entity, pointing back at
        # from_entity -- see RelationshipMapping's own docstring.
        _validate_foreign_key(rel, owner=to_entity, target=from_entity, issues=issues)
    elif rel.kind is RelationshipKind.VALUE_MATCH:
        _validate_value_match(rel, from_entity=from_entity, to_entity=to_entity, issues=issues)


def validate_mapping(*, ttl_path: Path = _TTL_PATH) -> None:
    """Raises `MappingValidationError` listing every problem found in
    `db_mapping.ENTITIES`/`RELATIONSHIPS`, checked against the real ORM
    models' tables/columns/foreign keys and the real `mars_ontology.ttl`
    file. Returns `None` (no exception) when everything matches."""
    issues: list[ValidationIssue] = []

    vocab = Graph()
    vocab.parse(ttl_path, format="turtle")
    declared_classes = {_local_name(s) for s in vocab.subjects(RDF.type, OWL.Class)}
    declared_properties = {_local_name(s) for s in vocab.subjects(RDF.type, OWL.ObjectProperty)} | {
        _local_name(s) for s in vocab.subjects(RDF.type, OWL.DatatypeProperty)
    }

    for entity in ENTITIES:
        _validate_entity(entity, declared_classes, declared_properties, issues)

    for rel in RELATIONSHIPS:
        _validate_relationship(rel, declared_properties, issues)

    if issues:
        details = "\n".join(f"  - [{issue.scope}] {issue.message}" for issue in issues)
        raise MappingValidationError(f"Ontology DB mapping validation failed:\n{details}")
