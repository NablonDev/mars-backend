"""Tests for `OntologyContextService` -- the semantic context layer's
read-only "what is this entity / what does this relationship mean"
interface (Phase 1G). No database connection, no live RDF graph -- only
`db_mapping` + a local `.ttl` parse.
"""

from __future__ import annotations

import unittest

from app.core.exceptions import NotFoundError
from app.services.ontology.context_service import OntologyContextService


class EntityContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OntologyContextService()

    def test_material_master_context_reports_its_real_table_pk_and_properties(self) -> None:
        context = self.service.get_entity_context("MaterialMaster")

        self.assertEqual(context.physical_table, "common.material_master")
        self.assertEqual(context.primary_key, "id")
        self.assertIn("Per-plant material/stock detail", context.description)

        by_name = {p.name: p for p in context.properties}
        self.assertEqual(by_name["sapMaterialNumber"].column, "sap_material_number")
        self.assertEqual(by_name["sapMaterialNumber"].datatype, "string")
        self.assertTrue(by_name["sapMaterialNumber"].required)
        self.assertFalse(by_name["discontinuationIndicator"].required)

    def test_cmir_record_context_does_not_expose_target_grd_code(self) -> None:
        context = self.service.get_entity_context("CmirRecord")
        self.assertNotIn("targetGrdCode", {p.name for p in context.properties})

    def test_unknown_entity_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.get_entity_context("NoSuchEntity")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.code, "ONTOLOGY_ENTITY_NOT_FOUND")


class RelationshipContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OntologyContextService()

    def test_material_master_succeeded_by_matches_the_required_phase1_demonstration(self) -> None:
        relationships = {r.name: r for r in self.service.get_relationships("MaterialMaster")}
        succeeded_by = relationships["succeededBy"]

        self.assertEqual(succeeded_by.target_entity, "Material")
        self.assertEqual(succeeded_by.kind.value, "foreign_key")
        self.assertEqual(
            succeeded_by.physical_implementation,
            "common.material_master.follow_up_material_id -> common.material.id",
        )

    def test_material_master_located_at_plant_is_also_a_foreign_key(self) -> None:
        relationships = {r.name: r for r in self.service.get_relationships("MaterialMaster")}
        self.assertEqual(relationships["locatedAtPlant"].kind.value, "foreign_key")
        self.assertEqual(
            relationships["locatedAtPlant"].physical_implementation,
            "common.material_master.plant_id -> common.plant.id",
        )

    def test_material_has_plant_record_as_a_reverse_foreign_key(self) -> None:
        relationships = {r.name: r for r in self.service.get_relationships("Material")}
        has_plant_record = relationships["hasPlantRecord"]
        self.assertEqual(has_plant_record.target_entity, "MaterialMaster")
        self.assertEqual(has_plant_record.kind.value, "reverse_foreign_key")

    def test_cmir_record_references_material_matches_the_required_phase1_demonstration(self) -> None:
        relationships = {r.name: r for r in self.service.get_relationships("CmirRecord")}
        references_material = relationships["referencesMaterial"]

        self.assertEqual(references_material.target_entity, "Material")
        self.assertEqual(references_material.kind.value, "value_match")
        self.assertEqual(
            references_material.physical_implementation,
            "cmir.cmir_record.material_identity = common.material.material_code",
        )

    def test_unknown_entity_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.get_relationships("NoSuchEntity")


class OperationContextNotImplementedTests(unittest.TestCase):
    def test_get_operation_context_raises_rather_than_fabricating_an_answer(self) -> None:
        service = OntologyContextService()
        with self.assertRaises(NotImplementedError):
            service.get_operation_context("MaterialMaster", "update")


if __name__ == "__main__":
    unittest.main()
