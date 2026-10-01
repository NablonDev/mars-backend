"""API/service schemas for the semantic context layer
(`app.services.ontology.context_service.OntologyContextService`) --
answers "what is this entity/relationship," never CRUD. See
`docs/ontology/semantic-context-layer-design.md` §20 for the design this
implements."""

from __future__ import annotations

from pydantic import BaseModel

from app.ontology.config.db_mapping import RelationshipKind


class PropertyContext(BaseModel):
    name: str
    column: str
    datatype: str
    required: bool


class EntityContext(BaseModel):
    entity: str
    physical_table: str
    primary_key: str
    description: str
    properties: list[PropertyContext]


class RelationshipContext(BaseModel):
    name: str
    target_entity: str
    kind: RelationshipKind
    physical_implementation: str
    description: str
