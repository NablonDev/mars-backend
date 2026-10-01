"""Structural sanity tests for `app.ontology.config.db_mapping` -- the
DB<->ontology mapping itself (not the validator, see
test_db_mapping_validation.py, and not the builder that consumes it, see
test_ontology_builder.py).

These tests exist to pin down, in one place, exactly which relationship is
which kind and what its physical implementation is -- so a future change to
db_mapping.py that accidentally reclassifies e.g. referencesMaterial as a
FOREIGN_KEY (it is not one -- see docs/ontology/semantic-context-layer-design.md
§10) fails here immediately, not just in the less obvious validator/builder
tests.
"""

from __future__ import annotations

import unittest

from app.models.cmir import CmirRecord
from app.models.common import Material, MaterialMaster, Plant
from app.ontology.config.db_mapping import ENTITIES, RELATIONSHIPS, RelationshipKind


def _entity(name: str):
    return next(e for e in ENTITIES if e.ontology_class == name)


def _relationship(name: str):
    return next(r for r in RELATIONSHIPS if r.ontology_property == name)


class EntityMappingTests(unittest.TestCase):
    def test_every_shared_entity_is_mapped(self) -> None:
        self.assertEqual(
            {e.ontology_class for e in ENTITIES},
            {"CmirRecord", "Material", "MaterialMaster", "Plant"},
        )

    def test_material_maps_to_the_real_model_and_natural_key(self) -> None:
        entity = _entity("Material")
        self.assertIs(entity.model, Material)
        self.assertIs(entity.iri_key_column, Material.material_code)

    def test_plant_maps_to_the_real_model_and_natural_key(self) -> None:
        entity = _entity("Plant")
        self.assertIs(entity.model, Plant)
        self.assertIs(entity.iri_key_column, Plant.plant_code)

    def test_material_master_and_cmir_record_fall_back_to_primary_key(self) -> None:
        # Neither has a natural business key of its own.
        self.assertIs(_entity("MaterialMaster").iri_key_column, MaterialMaster.id)
        self.assertIs(_entity("CmirRecord").iri_key_column, CmirRecord.id)

    def test_material_master_properties_map_to_real_columns(self) -> None:
        properties = {p.ontology_property: p.column for p in _entity("MaterialMaster").properties}
        self.assertIs(properties["sapMaterialNumber"], MaterialMaster.sap_material_number)
        self.assertIs(properties["discontinuationIndicator"], MaterialMaster.discontinuation_indicator)

    def test_cmir_record_properties_do_not_include_material_identity_or_target_grd_code(self) -> None:
        # material_identity drives the referencesMaterial relationship
        # (below), not a plain datatype property; target_grd_code is
        # unrelated to ontology matching entirely (see RELATIONSHIPS test).
        property_names = {p.ontology_property for p in _entity("CmirRecord").properties}
        self.assertNotIn("materialIdentity", property_names)
        self.assertNotIn("targetGrdCode", property_names)


class RelationshipMappingTests(unittest.TestCase):
    def test_succeeded_by_is_a_real_foreign_key_on_material_master(self) -> None:
        rel = _relationship("succeededBy")
        self.assertEqual(rel.kind, RelationshipKind.FOREIGN_KEY)
        self.assertEqual(rel.from_entity, "MaterialMaster")
        self.assertEqual(rel.to_entity, "Material")
        self.assertIs(rel.fk_column, MaterialMaster.follow_up_material_id)

    def test_located_at_plant_is_a_real_foreign_key_on_material_master(self) -> None:
        rel = _relationship("locatedAtPlant")
        self.assertEqual(rel.kind, RelationshipKind.FOREIGN_KEY)
        self.assertEqual(rel.from_entity, "MaterialMaster")
        self.assertEqual(rel.to_entity, "Plant")
        self.assertIs(rel.fk_column, MaterialMaster.plant_id)

    def test_has_plant_record_is_the_reverse_direction_of_material_masters_own_fk(self) -> None:
        rel = _relationship("hasPlantRecord")
        self.assertEqual(rel.kind, RelationshipKind.REVERSE_FOREIGN_KEY)
        self.assertEqual(rel.from_entity, "Material")
        self.assertEqual(rel.to_entity, "MaterialMaster")
        # The FK column physically lives on MaterialMaster, not Material --
        # that's what makes this "reverse."
        self.assertIs(rel.fk_column, MaterialMaster.material_id)

    def test_references_material_is_a_value_match_not_a_foreign_key(self) -> None:
        rel = _relationship("referencesMaterial")
        self.assertEqual(rel.kind, RelationshipKind.VALUE_MATCH)
        self.assertEqual(rel.from_entity, "CmirRecord")
        self.assertEqual(rel.to_entity, "Material")
        self.assertIsNone(rel.fk_column)
        self.assertIs(rel.value_from_column, CmirRecord.material_identity)
        self.assertIs(rel.value_to_column, Material.material_code)

    def test_references_material_is_keyed_on_material_identity_not_target_grd_code(self) -> None:
        # The specific regression this whole mapping module exists to
        # prevent from silently recurring -- see this session's own
        # target_grd_code -> material_identity fix.
        rel = _relationship("referencesMaterial")
        self.assertIsNot(rel.value_from_column, CmirRecord.target_grd_code)


if __name__ == "__main__":
    unittest.main()
