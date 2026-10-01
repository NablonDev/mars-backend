"""Node-level tests for the ontology-insert POC: interpret ->
context -> required-fields -> conflicts -> propose -> interrupt, no write.
Each node is called directly with a plain dict state, matching
`tests/unit/agents/test_ontology_update_nodes.py`'s convention -- fake
collaborators, no real DB, no real graph/checkpointer involved here (see
test_ontology_insert_graph.py for that level).

Uses the REAL `OntologyContextService` for `load_semantic_context`/
`check_required_fields` -- the entire point of this POC is that "which
fields are required" and "which relationship links MaterialMaster to
Plant" come from the real vocabulary/mapping, not a test double standing
in for it.
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from app.agents.ontology_insert.nodes import OntologyInsertNodes
from app.services.ontology.context_service import OntologyContextService


class FakeMasterDataRepository:
    """Dict-shaped, mirroring `MasterDataRepository`'s real return shapes.
    Deliberately has NO write method at all -- proves structurally that no
    node before `execute_insert` can write anything."""

    def __init__(self) -> None:
        self.materials_by_code: dict[str, dict] = {}
        self.plants_by_code: dict[str, dict] = {}
        self.material_masters: list[dict] = []

    def seed_material(self, material_code: str) -> dict:
        row = {"id": uuid4(), "material_code": material_code, "description": None}
        self.materials_by_code[material_code] = row
        return row

    def seed_plant(self, plant_code: str) -> dict:
        row = {"id": uuid4(), "plant_code": plant_code, "plant_name": None, "country_code": None}
        self.plants_by_code[plant_code] = row
        return row

    def seed_material_master(self, *, sap_material_number: str, plant_id) -> dict:
        row = {"id": uuid4(), "sap_material_number": sap_material_number, "plant_id": plant_id}
        self.material_masters.append(row)
        return row

    def get_material_by_code(self, material_code):
        return self.materials_by_code.get(material_code)

    def get_plant_by_code(self, plant_code):
        return self.plants_by_code.get(plant_code)

    def find_material_master(self, sap_material_number, plant_id):
        return next(
            (
                m
                for m in self.material_masters
                if m["sap_material_number"] == sap_material_number and m["plant_id"] == plant_id
            ),
            None,
        )


class FakeMaterialMasterService:
    """Records every call it receives -- proves `execute_insert` reaches
    this service (and only this service) for the actual write."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.result: dict | None = None

    def create_material_master(self, *, material_code, plant_code, sap_material_number):
        self.calls.append((material_code, plant_code, sap_material_number))
        return self.result or {
            "material_id": str(uuid4()),
            "plant_id": str(uuid4()),
            "material_master_id": str(uuid4()),
        }


def _nodes(
    master_data: FakeMasterDataRepository, material_master_service: FakeMaterialMasterService | None = None
) -> OntologyInsertNodes:
    return OntologyInsertNodes(
        context_service=OntologyContextService(),
        master_data_repository=master_data,
        material_master_service=material_master_service or FakeMaterialMasterService(),
    )


# ---- Request interpretation ----


class InterpretRequestTests(unittest.TestCase):
    def test_parses_the_full_supported_sentence_shape(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.interpret_request(
            {"user_request": "Create material MAT-3000 at plant P100 with SAP number SAP-3000."}
        )
        self.assertEqual(result["operation"], "INSERT")
        self.assertEqual(result["material_code"], "MAT-3000")
        self.assertEqual(result["plant_code"], "P100")
        self.assertEqual(result["sap_material_number"], "SAP-3000")
        self.assertNotIn("error", result)

    def test_parses_a_partial_sentence_leaving_absent_fields_unset(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.interpret_request({"user_request": "Create material MAT-3000."})
        self.assertEqual(result["material_code"], "MAT-3000")
        self.assertNotIn("plant_code", result)
        self.assertNotIn("sap_material_number", result)
        self.assertNotIn("error", result)

    def test_unrelated_request_returns_unsupported_operation_error(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.interpret_request({"user_request": "Please delete everything."})
        self.assertEqual(result["error"]["error_type"], "unsupported_operation")


# ---- Semantic context consumption ----


class LoadSemanticContextTests(unittest.TestCase):
    def test_discovers_required_fields_and_the_plant_relationship_from_the_real_ontology(self) -> None:
        # The proof this test exists for: nothing in nodes.py hardcodes
        # "material_code/plant_code/sap_material_number are required" or
        # "locatedAtPlant" as a literal string used to *find* the
        # relationship -- both come from OntologyContextService reading
        # the real mapping/vocabulary.
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.load_semantic_context({})
        required = result["semantic_context"]["required_properties"]
        self.assertEqual(required["Material"], ["materialCode"])
        self.assertEqual(required["Plant"], ["plantCode"])
        self.assertIn("sapMaterialNumber", required["MaterialMaster"])

        relationship = result["semantic_context"]["relationship"]
        self.assertEqual(relationship["target_entity"], "Plant")
        self.assertEqual(relationship["kind"], "foreign_key")


class CheckRequiredFieldsTests(unittest.TestCase):
    def _semantic_context(self) -> dict:
        return _nodes(FakeMasterDataRepository()).load_semantic_context({})["semantic_context"]

    def test_reports_every_missing_field(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        state = {"semantic_context": self._semantic_context(), "material_code": "MAT-3000"}
        result = nodes.check_required_fields(state)
        self.assertEqual(result["missing_fields"], ["plant_code", "sap_material_number"])

    def test_reports_no_missing_fields_when_everything_is_present(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        state = {
            "semantic_context": self._semantic_context(),
            "material_code": "MAT-3000",
            "plant_code": "P100",
            "sap_material_number": "SAP-3000",
        }
        result = nodes.check_required_fields(state)
        self.assertEqual(result["missing_fields"], [])


# ---- Conflict detection ----


class CheckForConflictsTests(unittest.TestCase):
    def test_no_conflict_for_brand_new_material_and_plant(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.check_for_conflicts(
            {"material_code": "MAT-3000", "plant_code": "P100", "sap_material_number": "SAP-3000"}
        )
        self.assertNotIn("error", result)

    def test_existing_material_code_is_a_conflict(self) -> None:
        master_data = FakeMasterDataRepository()
        master_data.seed_material("MAT-3000")
        nodes = _nodes(master_data)

        result = nodes.check_for_conflicts(
            {"material_code": "MAT-3000", "plant_code": "P100", "sap_material_number": "SAP-3000"}
        )
        self.assertEqual(result["error"]["error_type"], "material_already_exists")

    def test_existing_material_master_at_the_same_plant_and_sap_number_is_a_conflict(self) -> None:
        master_data = FakeMasterDataRepository()
        plant = master_data.seed_plant("P100")
        master_data.seed_material_master(sap_material_number="SAP-3000", plant_id=plant["id"])
        nodes = _nodes(master_data)

        result = nodes.check_for_conflicts(
            {"material_code": "MAT-3000", "plant_code": "P100", "sap_material_number": "SAP-3000"}
        )
        self.assertEqual(result["error"]["error_type"], "material_master_already_exists")

    def test_reusing_an_existing_plant_with_a_different_sap_number_is_not_a_conflict(self) -> None:
        master_data = FakeMasterDataRepository()
        plant = master_data.seed_plant("P100")
        master_data.seed_material_master(sap_material_number="SAP-OTHER", plant_id=plant["id"])
        nodes = _nodes(master_data)

        result = nodes.check_for_conflicts(
            {"material_code": "MAT-3000", "plant_code": "P100", "sap_material_number": "SAP-3000"}
        )
        self.assertNotIn("error", result)


# ---- Proposal ----


class BuildProposalTests(unittest.TestCase):
    def test_proposal_describes_the_three_new_rows_and_the_plant_relationship(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        state = {
            "material_code": "MAT-3000",
            "plant_code": "P100",
            "sap_material_number": "SAP-3000",
            "semantic_context": {"relationship": {"name": "locatedAtPlant", "target_entity": "Plant", "kind": "foreign_key"}},
        }

        result = nodes.build_proposal(state)
        proposal = result["proposal"]

        self.assertEqual(proposal["operation"], "INSERT")
        self.assertEqual(proposal["material"]["material_code"], "MAT-3000")
        self.assertEqual(proposal["plant"]["plant_code"], "P100")
        self.assertFalse(proposal["plant"]["reused_existing"])
        self.assertEqual(proposal["material_master"]["sap_material_number"], "SAP-3000")
        self.assertEqual(proposal["relationship"]["name"], "locatedAtPlant")
        self.assertIn("MAT-3000", proposal["summary"])
        self.assertIn("SAP-3000", proposal["summary"])

    def test_proposal_flags_an_existing_plant_as_reused(self) -> None:
        master_data = FakeMasterDataRepository()
        master_data.seed_plant("P100")
        nodes = _nodes(master_data)
        state = {
            "material_code": "MAT-3000",
            "plant_code": "P100",
            "sap_material_number": "SAP-3000",
            "semantic_context": {"relationship": {"name": "locatedAtPlant", "target_entity": "Plant", "kind": "foreign_key"}},
        }

        result = nodes.build_proposal(state)
        self.assertTrue(result["proposal"]["plant"]["reused_existing"])


class ExecuteInsertTests(unittest.TestCase):
    def test_calls_material_master_service_not_the_repository_and_records_the_result(self) -> None:
        service = FakeMaterialMasterService()
        nodes = _nodes(FakeMasterDataRepository(), service)

        result = nodes.execute_insert(
            {"material_code": "MAT-3000", "plant_code": "P100", "sap_material_number": "SAP-3000"}
        )

        self.assertEqual(service.calls, [("MAT-3000", "P100", "SAP-3000")])
        self.assertNotIn("error", result)
        self.assertIn("execution_result", result)

    def test_service_validation_failure_is_surfaced_as_a_routed_error_not_raised(self) -> None:
        class FailingService:
            def create_material_master(self, **kwargs):
                from app.core.exceptions import ConflictError

                raise ConflictError(code="MATERIAL_ALREADY_EXISTS", message="nope")

        nodes = _nodes(FakeMasterDataRepository(), FailingService())

        result = nodes.execute_insert(
            {"material_code": "MAT-3000", "plant_code": "P100", "sap_material_number": "SAP-3000"}
        )

        self.assertEqual(result["error"]["error_type"], "service_validation_failure")
        self.assertNotIn("execution_result", result)


if __name__ == "__main__":
    unittest.main()
