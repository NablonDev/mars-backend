"""Single source of truth for how physical PostgreSQL structure maps onto
the `mars_ontology.ttl` vocabulary -- the "DB <-> ontology mapping" from
`docs/ontology/ontology-context-layer-design.md` (design) and
`docs/ontology/semantic-context-layer-design.md` (design, relationship-kind
detail).

Deliberately data-only: no queries, no control flow. `OntologyBuilder`
(`app/services/ontology/ontology_builder.py`) and `OntologyContextService`
(`app/services/ontology/context_service.py`) read this; neither extends it.

Columns are referenced as real SQLAlchemy `InstrumentedAttribute`s
(`Material.material_code`, never the string `"material_code"`) -- a
renamed/removed column breaks this module at **import time**
(`AttributeError`), before any request or rebuild ever runs. This is the
single biggest reason this mapping is a Python module and not YAML/JSON
(see the design docs' comparison tables for the full argument): a string
column name can silently drift from reality; an attribute reference cannot.

Physical facts (table, column, FK target) live here. Business meaning
(what a class/property/relationship *means*) lives in `mars_ontology.ttl`
and is never duplicated into this file -- see `app/ontology/config/validation.py`
for the check that keeps the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import InstrumentedAttribute

from app.db.base import Base
from app.models.cmir.cmir_record import CmirRecord
from app.models.common.material import Material, MaterialMaster
from app.models.common.plant import Plant


@dataclass(frozen=True)
class PropertyMapping:
    """One `owl:DatatypeProperty` <-> one column on one entity's model."""

    ontology_property: str
    column: InstrumentedAttribute


@dataclass(frozen=True)
class EntityMapping:
    """One `owl:Class` <-> one ORM model / table.

    `iri_key_column` is the column stable IRIs are derived from -- a
    natural business key where one exists (`Material.material_code`,
    `Plant.plant_code`), the primary key otherwise (`MaterialMaster.id`,
    `CmirRecord.id`, neither of which has a natural key of its own).
    """

    ontology_class: str
    model: type[Base]
    iri_key_column: InstrumentedAttribute
    properties: tuple[PropertyMapping, ...] = ()


class RelationshipKind(str, Enum):
    """How a semantic relationship is physically implemented -- see
    `docs/ontology/semantic-context-layer-design.md` §10/§7 for the full
    reasoning behind treating these as three distinct kinds rather than
    flattening every relationship into "it's a foreign key."""

    #: A real, declared `ForeignKey` column on the relationship's own
    #: `from_entity` (e.g. `succeededBy`: the FK lives on `MaterialMaster`,
    #: the same entity the relationship's semantic domain is).
    FOREIGN_KEY = "foreign_key"

    #: A real, declared `ForeignKey`, but the FK column physically lives on
    #: the relationship's `to_entity`, pointing back at `from_entity` (e.g.
    #: `hasPlantRecord`: `Material -> MaterialMaster` semantically, but the
    #: FK column (`material_master.material_id`) lives on `MaterialMaster`,
    #: not `Material`).
    REVERSE_FOREIGN_KEY = "reverse_foreign_key"

    #: No database-level constraint at all -- the relationship holds only
    #: because two columns' *values* happen to match (e.g.
    #: `referencesMaterial`: `cmir_record.material_identity` is a plain,
    #: unconstrained string column that happens to equal some
    #: `material.material_code`). Must never be validated as if it were an
    #: FK -- there is no FK to find.
    VALUE_MATCH = "value_match"


@dataclass(frozen=True)
class RelationshipMapping:
    """One `owl:ObjectProperty` <-> its physical implementation.

    `from_entity`/`to_entity` are always the relationship's *semantic*
    domain/range (matching `mars_ontology.ttl`'s `rdfs:domain`/`rdfs:range`
    for this property) -- they do not change based on `kind`. What changes
    per `kind` is where the physical implementation is found:

    - FOREIGN_KEY / REVERSE_FOREIGN_KEY: `fk_column` is the physical FK
      column. For FOREIGN_KEY it lives on `from_entity`'s table; for
      REVERSE_FOREIGN_KEY it lives on `to_entity`'s table (see the
      `RelationshipKind` docstrings above for why).
    - VALUE_MATCH: `value_from_column`/`value_to_column` are the two
      columns whose values must match; there is no FK to check.
    """

    ontology_property: str
    kind: RelationshipKind
    from_entity: str
    to_entity: str
    fk_column: InstrumentedAttribute | None = None
    value_from_column: InstrumentedAttribute | None = None
    value_to_column: InstrumentedAttribute | None = None


# ---------------------------------------------------------------------------
# Entities -- the four shared classes already in mars_ontology.ttl. Adding a
# column here is a deliberate "this is now something the context layer
# should know about" decision, not automatic -- plenty of real columns on
# these same tables (Material.description, MaterialMaster.available_quantity,
# .uom, .effective_out_date, .source_system, .last_synced_at, ...) are
# intentionally left unmapped because no current ontology consumer needs
# them (see docs/ontology/ontology-context-layer-design.md §9).
# ---------------------------------------------------------------------------

ENTITIES: tuple[EntityMapping, ...] = (
    EntityMapping(
        ontology_class="Material",
        model=Material,
        iri_key_column=Material.material_code,
        properties=(PropertyMapping("materialCode", Material.material_code),),
    ),
    EntityMapping(
        ontology_class="Plant",
        model=Plant,
        iri_key_column=Plant.plant_code,
        properties=(PropertyMapping("plantCode", Plant.plant_code),),
    ),
    EntityMapping(
        ontology_class="MaterialMaster",
        model=MaterialMaster,
        iri_key_column=MaterialMaster.id,
        properties=(
            PropertyMapping("sapMaterialNumber", MaterialMaster.sap_material_number),
            PropertyMapping("discontinuationIndicator", MaterialMaster.discontinuation_indicator),
        ),
    ),
    EntityMapping(
        ontology_class="CmirRecord",
        model=CmirRecord,
        iri_key_column=CmirRecord.id,
        properties=(
            PropertyMapping("brand", CmirRecord.brand),
            PropertyMapping("site", CmirRecord.site),
            PropertyMapping("customerIdentity", CmirRecord.customer_identity),
            PropertyMapping("targetCustomerMaterialRef", CmirRecord.target_customer_material_ref),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Relationships -- the four already in mars_ontology.ttl, per the exact
# physical facts confirmed against app/models/cmir/cmir_record.py and
# app/models/common/material.py.
# ---------------------------------------------------------------------------

RELATIONSHIPS: tuple[RelationshipMapping, ...] = (
    RelationshipMapping(
        ontology_property="succeededBy",
        kind=RelationshipKind.FOREIGN_KEY,
        from_entity="MaterialMaster",
        to_entity="Material",
        fk_column=MaterialMaster.follow_up_material_id,
    ),
    RelationshipMapping(
        ontology_property="locatedAtPlant",
        kind=RelationshipKind.FOREIGN_KEY,
        from_entity="MaterialMaster",
        to_entity="Plant",
        fk_column=MaterialMaster.plant_id,
    ),
    RelationshipMapping(
        ontology_property="hasPlantRecord",
        kind=RelationshipKind.REVERSE_FOREIGN_KEY,
        from_entity="Material",
        to_entity="MaterialMaster",
        # The FK column lives on MaterialMaster (the "to_entity" here),
        # pointing back at Material -- this is exactly what makes it
        # "reverse": Material has no column of its own pointing at
        # MaterialMaster.
        fk_column=MaterialMaster.material_id,
    ),
    RelationshipMapping(
        ontology_property="referencesMaterial",
        kind=RelationshipKind.VALUE_MATCH,
        from_entity="CmirRecord",
        to_entity="Material",
        # NOT a foreign key -- cmir.cmir_record.material_identity is a
        # plain, unconstrained string column. The join exists only because
        # its value happens to equal some material.material_code; nothing
        # in Postgres enforces this. See RelationshipKind.VALUE_MATCH.
        value_from_column=CmirRecord.material_identity,
        value_to_column=Material.material_code,
    ),
)
