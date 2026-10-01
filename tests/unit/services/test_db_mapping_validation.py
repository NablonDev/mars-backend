"""Tests for `app.ontology.config.validation.validate_mapping` -- proves it
both (a) passes cleanly against the real, correct mapping and vocabulary,
and (b) actually detects every kind of drift it's designed to catch, using
deliberately broken fixture mappings rather than the real `db_mapping`
module (which stays untouched/correct throughout this file).

No live Postgres connection is used anywhere in this file -- every check
reads `Base.metadata`/the ORM models (populated by import alone) and a
local `.ttl` parse, which is the whole point of this validator (see
`app/ontology/config/validation.py`'s module docstring).
"""

from __future__ import annotations

import unittest

from app.models.cmir import CmirRecord
from app.models.common import Material, MaterialMaster
from app.ontology.config.db_mapping import (
    EntityMapping,
    PropertyMapping,
    RelationshipKind,
    RelationshipMapping,
)
from app.ontology.config.validation import (
    _validate_entity,
    _validate_relationship,
    validate_mapping,
)


class RealMappingValidatesCleanlyTests(unittest.TestCase):
    def test_the_real_mapping_validates_with_no_issues(self) -> None:
        # Raises MappingValidationError on failure -- reaching this line at
        # all is the assertion.
        validate_mapping()


class EntityValidationTests(unittest.TestCase):
    def test_undeclared_ontology_class_is_flagged(self) -> None:
        issues: list = []
        entity = EntityMapping(
            ontology_class="TotallyMadeUpClass", model=Material, iri_key_column=Material.material_code
        )
        _validate_entity(entity, declared_classes=set(), declared_properties=set(), issues=issues)
        self.assertTrue(any("not declared as an owl:Class" in i.message for i in issues))

    def test_property_column_from_the_wrong_table_is_flagged(self) -> None:
        issues: list = []
        entity = EntityMapping(
            ontology_class="Material",
            model=Material,
            iri_key_column=Material.material_code,
            properties=(PropertyMapping("materialCode", MaterialMaster.sap_material_number),),
        )
        _validate_entity(
            entity, declared_classes={"Material"}, declared_properties={"materialCode"}, issues=issues
        )
        self.assertTrue(any("does not belong to" in i.message for i in issues))

    def test_undeclared_property_is_flagged(self) -> None:
        issues: list = []
        entity = EntityMapping(
            ontology_class="Material",
            model=Material,
            iri_key_column=Material.material_code,
            properties=(PropertyMapping("notARealVocabProperty", Material.material_code),),
        )
        _validate_entity(entity, declared_classes={"Material"}, declared_properties=set(), issues=issues)
        self.assertTrue(any("not declared as an owl:DatatypeProperty" in i.message for i in issues))


class RelationshipValidationTests(unittest.TestCase):
    def test_foreign_key_with_no_real_fk_constraint_is_flagged(self) -> None:
        # material_identity is a plain string column with NO ForeignKey --
        # asserting it as one must fail, not silently "validate."
        issues: list = []
        rel = RelationshipMapping(
            ontology_property="succeededBy",
            kind=RelationshipKind.FOREIGN_KEY,
            from_entity="CmirRecord",
            to_entity="Material",
            fk_column=CmirRecord.material_identity,
        )
        _validate_relationship(rel, declared_properties={"succeededBy"}, issues=issues)
        self.assertTrue(any("no ForeignKey constraint" in i.message for i in issues))

    def test_foreign_key_pointing_at_the_wrong_target_table_is_flagged(self) -> None:
        issues: list = []
        # follow_up_material_id really points at common.material, not common.plant.
        rel = RelationshipMapping(
            ontology_property="succeededBy",
            kind=RelationshipKind.FOREIGN_KEY,
            from_entity="MaterialMaster",
            to_entity="Plant",
            fk_column=MaterialMaster.follow_up_material_id,
        )
        _validate_relationship(rel, declared_properties={"succeededBy"}, issues=issues)
        self.assertTrue(any("expected" in i.message and "Plant" in i.message for i in issues))

    def test_unknown_from_entity_is_flagged(self) -> None:
        issues: list = []
        rel = RelationshipMapping(
            ontology_property="succeededBy",
            kind=RelationshipKind.FOREIGN_KEY,
            from_entity="NoSuchEntity",
            to_entity="Material",
            fk_column=MaterialMaster.follow_up_material_id,
        )
        _validate_relationship(rel, declared_properties={"succeededBy"}, issues=issues)
        self.assertTrue(any("has no EntityMapping" in i.message for i in issues))

    def test_value_match_missing_both_columns_is_flagged(self) -> None:
        issues: list = []
        rel = RelationshipMapping(
            ontology_property="referencesMaterial",
            kind=RelationshipKind.VALUE_MATCH,
            from_entity="CmirRecord",
            to_entity="Material",
        )
        _validate_relationship(rel, declared_properties={"referencesMaterial"}, issues=issues)
        self.assertTrue(
            any("requires both value_from_column and value_to_column" in i.message for i in issues)
        )

    def test_value_match_with_incompatible_datatypes_is_flagged(self) -> None:
        issues: list = []
        rel = RelationshipMapping(
            ontology_property="referencesMaterial",
            kind=RelationshipKind.VALUE_MATCH,
            from_entity="CmirRecord",
            to_entity="MaterialMaster",
            value_from_column=CmirRecord.material_identity,
            value_to_column=MaterialMaster.effective_out_date,
        )
        _validate_relationship(rel, declared_properties={"referencesMaterial"}, issues=issues)
        self.assertTrue(any("not datatype-compatible" in i.message for i in issues))

    def test_value_match_with_compatible_string_columns_raises_no_issue(self) -> None:
        issues: list = []
        rel = RelationshipMapping(
            ontology_property="referencesMaterial",
            kind=RelationshipKind.VALUE_MATCH,
            from_entity="CmirRecord",
            to_entity="Material",
            value_from_column=CmirRecord.material_identity,
            value_to_column=Material.material_code,
        )
        _validate_relationship(rel, declared_properties={"referencesMaterial"}, issues=issues)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
