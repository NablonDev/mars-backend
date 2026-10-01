"""Node-level tests for the ontology-update POC (Checkpoint 2 scope:
interpret -> context -> resolve -> propose -> interrupt, no write). Each
node is called directly with a plain dict state, matching
`tests/unit/agents/test_cmir_nodes.py`'s convention -- fake collaborators,
no real DB, no real graph/checkpointer involved here (see
test_ontology_update_graph.py for that level).

Uses the REAL `OntologyContextService` (cheap -- no DB, just a local `.ttl`
parse) rather than a fake, since the entire point of this POC is that the
Agent's understanding of "replacement" comes from the real vocabulary, not
a test double standing in for it -- see `test_discovers_succeeded_by_...`
below (checklist item B).
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from app.agents.ontology_update.nodes import OntologyUpdateNodes
from app.services.ontology.context_service import OntologyContextService


class FakeMasterDataRepository:
    """Dict-shaped, mirroring `MasterDataRepository`'s real return shapes.
    Deliberately has NO write/update method at all -- see checklist item I
    ("verify NO WRITE"), which is proved structurally, not just asserted."""

    def __init__(self) -> None:
        self.materials_by_code: dict[str, dict] = {}
        self.materials_by_id: dict = {}
        self.material_masters_by_material_id: dict = {}

    def add_material(self, material_code: str, *, material_id=None) -> dict:
        material_id = material_id or uuid4()
        row = {"id": material_id, "material_code": material_code, "description": None}
        self.materials_by_code[material_code] = row
        self.materials_by_id[material_id] = row
        return row

    def add_material_master(self, *, material_id, plant_id, follow_up_material_id=None) -> dict:
        row = {
            "id": uuid4(),
            "material_id": material_id,
            "plant_id": plant_id,
            "sap_material_number": "SAP-TEST",
            "follow_up_material_id": follow_up_material_id,
        }
        self.material_masters_by_material_id.setdefault(material_id, []).append(row)
        return row

    def get_material_by_code(self, material_code):
        return self.materials_by_code.get(material_code)

    def get_material_by_id(self, material_id):
        return self.materials_by_id.get(material_id)

    def list_material_masters_for_material(self, material_id):
        return self.material_masters_by_material_id.get(material_id, [])


class FakeMaterialMasterService:
    """Records every call it receives -- the graph/node tests assert
    against `.calls` to prove `execute_update` reaches this service (and
    only this service, never `FakeMasterDataRepository`'s write side,
    which doesn't exist) for the actual write."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._material_masters: dict = {}

    def register_material_master(self, material_master: dict) -> None:
        self._material_masters[material_master["id"]] = dict(material_master)

    def update_replacement_material(self, *, material_master_id, replacement_material_id):
        self.calls.append((material_master_id, replacement_material_id))
        row = self._material_masters[material_master_id]
        row["follow_up_material_id"] = replacement_material_id
        return row


def _nodes(
    master_data: FakeMasterDataRepository, material_master_service: FakeMaterialMasterService | None = None
) -> OntologyUpdateNodes:
    return OntologyUpdateNodes(
        context_service=OntologyContextService(),
        master_data_repository=master_data,
        material_master_service=material_master_service or FakeMaterialMasterService(),
    )


# ---- A. Request interpretation ----


class InterpretRequestTests(unittest.TestCase):
    def test_parses_the_supported_sentence_shape(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.interpret_request(
            {"user_request": "Update the replacement material for MAT-DISC-1001 to MAT-REPL-1001."}
        )
        self.assertEqual(result["operation"], "UPDATE")
        self.assertEqual(result["source_material_code"], "MAT-DISC-1001")
        self.assertEqual(result["replacement_material_code"], "MAT-REPL-1001")
        self.assertNotIn("error", result)

    def test_unparseable_request_returns_unsupported_operation_error(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.interpret_request({"user_request": "Please delete everything."})
        self.assertEqual(result["error"]["error_type"], "unsupported_operation")


# ---- B. Semantic context consumption ----


class LoadSemanticContextTests(unittest.TestCase):
    def test_discovers_succeeded_by_from_the_real_vocabulary_description_not_a_hardcoded_name(self) -> None:
        # The proof this test exists for: nothing in nodes.py references
        # "succeededBy" or "follow_up_material_id" directly when deciding
        # which relationship means "replacement" -- only the keyword
        # "replac" matched against OntologyContextService's real
        # description text, sourced from mars_ontology.ttl. This calls the
        # real service; nothing about "follow_up_material_id" is mocked in.
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.load_semantic_context({})
        self.assertEqual(result["semantic_context"]["name"], "succeededBy")
        self.assertEqual(result["semantic_context"]["target_entity"], "Material")
        self.assertEqual(result["semantic_context"]["kind"], "foreign_key")


# ---- C/D/E. Material / replacement / MaterialMaster resolution ----


class ResolveTargetTests(unittest.TestCase):
    def test_resolves_the_material_and_its_single_material_master_row(self) -> None:
        master_data = FakeMasterDataRepository()
        source = master_data.add_material("MAT-DISC-1001")
        master_master = master_data.add_material_master(material_id=source["id"], plant_id=uuid4())
        nodes = _nodes(master_data)

        result = nodes.resolve_target({"source_material_code": "MAT-DISC-1001"})

        self.assertNotIn("error", result)
        self.assertNotIn("clarification_required", result)
        self.assertEqual(result["resolved_material"]["material_code"], "MAT-DISC-1001")
        self.assertEqual(result["resolved_material_master"]["id"], master_master["id"])

    def test_unknown_source_identifier_is_an_error(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.resolve_target({"source_material_code": "MAT-DOES-NOT-EXIST"})
        self.assertEqual(result["error"]["error_type"], "unknown_source_identifier")

    # ---- F. Ambiguity ----

    def test_material_with_master_rows_at_multiple_plants_requires_clarification_not_a_guess(self) -> None:
        master_data = FakeMasterDataRepository()
        source = master_data.add_material("MAT-DISC-1001")
        master_data.add_material_master(material_id=source["id"], plant_id=uuid4())
        master_data.add_material_master(material_id=source["id"], plant_id=uuid4())
        nodes = _nodes(master_data)

        result = nodes.resolve_target({"source_material_code": "MAT-DISC-1001"})

        self.assertTrue(result["clarification_required"])
        self.assertNotIn("resolved_material_master", result)
        self.assertNotIn("error", result)

    def test_material_with_no_master_rows_is_an_error_not_a_clarification(self) -> None:
        master_data = FakeMasterDataRepository()
        master_data.add_material("MAT-DISC-1001")
        nodes = _nodes(master_data)

        result = nodes.resolve_target({"source_material_code": "MAT-DISC-1001"})
        self.assertEqual(result["error"]["error_type"], "unknown_source_identifier")


class ResolveReplacementTests(unittest.TestCase):
    def test_resolves_the_replacement_material(self) -> None:
        master_data = FakeMasterDataRepository()
        master_data.add_material("MAT-REPL-1001")
        nodes = _nodes(master_data)

        result = nodes.resolve_replacement({"replacement_material_code": "MAT-REPL-1001"})

        self.assertNotIn("error", result)
        self.assertEqual(result["resolved_replacement_material"]["material_code"], "MAT-REPL-1001")

    def test_unknown_replacement_identifier_is_an_error_and_does_not_continue(self) -> None:
        nodes = _nodes(FakeMasterDataRepository())
        result = nodes.resolve_replacement({"replacement_material_code": "MAT-DOES-NOT-EXIST"})
        self.assertEqual(result["error"]["error_type"], "unknown_replacement_identifier")


# ---- G. Proposal ----


class BuildProposalTests(unittest.TestCase):
    def test_proposal_contains_update_materialmaster_succeededby_and_both_codes(self) -> None:
        master_data = FakeMasterDataRepository()
        nodes = _nodes(master_data)
        state = {
            "operation": "UPDATE",
            "resolved_material": {"id": uuid4(), "material_code": "MAT-DISC-1001"},
            "resolved_material_master": {"id": uuid4(), "follow_up_material_id": None},
            "resolved_replacement_material": {"id": uuid4(), "material_code": "MAT-REPL-1001"},
            "semantic_context": {"name": "succeededBy", "target_entity": "Material", "kind": "foreign_key"},
        }

        result = nodes.build_proposal(state)
        proposal = result["proposal"]

        self.assertEqual(proposal["operation"], "UPDATE")
        self.assertEqual(proposal["entity"], "MaterialMaster")
        self.assertEqual(proposal["target"]["material_code"], "MAT-DISC-1001")
        self.assertEqual(proposal["relationship"]["name"], "succeededBy")
        self.assertEqual(proposal["new_value"]["material_code"], "MAT-REPL-1001")
        self.assertIn("MAT-DISC-1001", proposal["summary"])
        self.assertIn("MAT-REPL-1001", proposal["summary"])
        # Human-readable, not SQL. ("SET " is deliberately not checked here
        # -- the plain-English summary "Set X as the replacement..."
        # legitimately contains it.)
        flattened = " ".join(str(v) for v in proposal.values()).upper()
        for sql_marker in ("SELECT ", "WHERE ", "COMMON.MATERIAL_MASTER", ";"):
            self.assertNotIn(sql_marker, flattened)

    def test_proposal_shows_the_current_replacement_value_when_one_already_exists(self) -> None:
        master_data = FakeMasterDataRepository()
        current_replacement = master_data.add_material("MAT-OLD-REPL")
        nodes = _nodes(master_data)
        state = {
            "operation": "UPDATE",
            "resolved_material": {"id": uuid4(), "material_code": "MAT-DISC-1001"},
            "resolved_material_master": {"id": uuid4(), "follow_up_material_id": current_replacement["id"]},
            "resolved_replacement_material": {"id": uuid4(), "material_code": "MAT-REPL-1001"},
            "semantic_context": {"name": "succeededBy", "target_entity": "Material", "kind": "foreign_key"},
        }

        result = nodes.build_proposal(state)
        self.assertEqual(result["proposal"]["current_value"]["material_code"], "MAT-OLD-REPL")

    def test_proposal_current_value_is_empty_when_no_replacement_was_previously_set(self) -> None:
        master_data = FakeMasterDataRepository()
        nodes = _nodes(master_data)
        state = {
            "operation": "UPDATE",
            "resolved_material": {"id": uuid4(), "material_code": "MAT-DISC-1001"},
            "resolved_material_master": {"id": uuid4(), "follow_up_material_id": None},
            "resolved_replacement_material": {"id": uuid4(), "material_code": "MAT-REPL-1001"},
            "semantic_context": {"name": "succeededBy", "target_entity": "Material", "kind": "foreign_key"},
        }

        result = nodes.build_proposal(state)
        self.assertIsNone(result["proposal"]["current_value"]["material_code"])


class ExecuteUpdateTests(unittest.TestCase):
    def test_calls_material_master_service_not_the_repository_and_records_the_result(self) -> None:
        master_data = FakeMasterDataRepository()
        service = FakeMaterialMasterService()
        master_master_id = uuid4()
        service.register_material_master({"id": master_master_id, "follow_up_material_id": None})
        nodes = _nodes(master_data, service)

        result = nodes.execute_update(
            {
                "resolved_material_master": {"id": master_master_id},
                "resolved_replacement_material": {"id": uuid4()},
            }
        )

        self.assertEqual(len(service.calls), 1)
        self.assertNotIn("error", result)
        self.assertIn("execution_result", result)
        self.assertEqual(result["execution_result"]["material_master_id"], str(master_master_id))

    def test_service_validation_failure_is_surfaced_as_a_routed_error_not_raised(self) -> None:
        master_data = FakeMasterDataRepository()

        class FailingService:
            def update_replacement_material(self, **kwargs):
                from app.core.exceptions import NotFoundError

                raise NotFoundError(code="MATERIAL_NOT_FOUND", message="nope")

        nodes = _nodes(master_data, FailingService())

        result = nodes.execute_update(
            {
                "resolved_material_master": {"id": uuid4()},
                "resolved_replacement_material": {"id": uuid4()},
            }
        )

        self.assertEqual(result["error"]["error_type"], "service_validation_failure")
        self.assertNotIn("execution_result", result)


if __name__ == "__main__":
    unittest.main()
