"""Graph-level tests for the ontology-insert POC. Real `graph.invoke(...)`
runs through both interrupt points and their resumes, using a real
`MemorySaver` checkpointer, matching
`tests/unit/agents/test_ontology_update_graph.py`'s convention (no mocking
of `graph.invoke` itself). Self-contained checkpoint/thread_id -- no
`process.workflow_thread`/`human_action` row is created or touched
anywhere in this file.
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.ontology_insert.graph import build_ontology_insert_graph
from app.agents.ontology_insert.nodes import OntologyInsertNodes
from app.services.ontology.context_service import OntologyContextService
from tests.unit.agents.test_ontology_insert_nodes import FakeMasterDataRepository, FakeMaterialMasterService

INTERRUPT_KEY = "__interrupt__"


class FakeTraceRepo:
    def log(self, *args, **kwargs):
        pass


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class OntologyInsertGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master_data = FakeMasterDataRepository()
        self.material_master_service = FakeMaterialMasterService()

        nodes = OntologyInsertNodes(
            context_service=OntologyContextService(),
            master_data_repository=self.master_data,
            material_master_service=self.material_master_service,
        )
        self.graph = build_ontology_insert_graph(nodes, MemorySaver(), FakeTraceRepo())

    def _run_to_first_pause(self, message: str) -> tuple[str, dict]:
        thread_id = f"test-{uuid4().hex[:8]}"
        state = self.graph.invoke({"user_request": message}, config=_config(thread_id))
        return thread_id, state

    # ---- Full sentence: straight to the approval interrupt ----

    def test_full_sentence_reaches_the_approval_interrupt_directly(self) -> None:
        _thread_id, state = self._run_to_first_pause(
            "Create material MAT-3000 at plant P100 with SAP number SAP-3000."
        )

        self.assertIn(INTERRUPT_KEY, state)
        payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(payload["reason"], "material_master_insert_approval")
        self.assertEqual(payload["proposal"]["material"]["material_code"], "MAT-3000")
        self.assertEqual(payload["proposal"]["plant"]["plant_code"], "P100")

    def test_unrelated_request_ends_cleanly_with_an_error_not_an_interrupt(self) -> None:
        _thread_id, state = self._run_to_first_pause("Please delete everything.")
        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(state["error"]["error_type"], "unsupported_operation")

    def test_existing_material_code_short_circuits_before_any_interrupt(self) -> None:
        self.master_data.seed_material("MAT-3000")
        _thread_id, state = self._run_to_first_pause(
            "Create material MAT-3000 at plant P100 with SAP number SAP-3000."
        )
        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(state["error"]["error_type"], "material_already_exists")

    # ---- Partial sentence: missing-details interrupt, then the loop-back ----

    def test_partial_sentence_pauses_asking_for_the_missing_fields(self) -> None:
        _thread_id, state = self._run_to_first_pause("Create material MAT-3000.")

        self.assertIn(INTERRUPT_KEY, state)
        payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(payload["reason"], "missing_required_fields")
        self.assertEqual(payload["missing_fields"], ["plant_code", "sap_material_number"])

    def test_supplying_the_missing_details_advances_to_the_approval_interrupt(self) -> None:
        thread_id, _state = self._run_to_first_pause("Create material MAT-3000.")

        state = self.graph.invoke(
            Command(resume={"details": {"plant_code": "P100", "sap_material_number": "SAP-3000"}}),
            config=_config(thread_id),
        )

        self.assertIn(INTERRUPT_KEY, state)
        payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(payload["reason"], "material_master_insert_approval")
        self.assertEqual(payload["proposal"]["material_master"]["sap_material_number"], "SAP-3000")

    def test_a_second_missing_details_round_is_asked_for_if_still_incomplete(self) -> None:
        thread_id, _state = self._run_to_first_pause("Create material MAT-3000.")

        state = self.graph.invoke(
            Command(resume={"details": {"plant_code": "P100"}}),
            config=_config(thread_id),
        )

        self.assertIn(INTERRUPT_KEY, state)
        payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(payload["reason"], "missing_required_fields")
        self.assertEqual(payload["missing_fields"], ["sap_material_number"])

    # ---- Verify NO WRITE happens before approval ----

    def test_no_repository_write_method_exists_and_service_is_untouched_before_approval(self) -> None:
        self._run_to_first_pause("Create material MAT-3000 at plant P100 with SAP number SAP-3000.")
        for write_method in ("add_material", "add_material_master", "add_plant", "get_or_create_plant"):
            self.assertFalse(hasattr(self.master_data, write_method))
        self.assertEqual(self.material_master_service.calls, [])

    # ---- Resume rejection ----

    def test_reject_terminates_without_write_and_never_calls_the_service(self) -> None:
        thread_id, _state = self._run_to_first_pause(
            "Create material MAT-3000 at plant P100 with SAP number SAP-3000."
        )

        state = self.graph.invoke(Command(resume={"decision": "reject"}), config=_config(thread_id))

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(state["approval_status"], "reject")
        self.assertNotIn("execution_result", state)
        self.assertEqual(self.material_master_service.calls, [])

    # ---- Resume approval ----

    def test_approve_resumes_and_calls_the_service_exactly_once(self) -> None:
        thread_id, _state = self._run_to_first_pause(
            "Create material MAT-3000 at plant P100 with SAP number SAP-3000."
        )

        state = self.graph.invoke(Command(resume={"decision": "approve"}), config=_config(thread_id))

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(state["approval_status"], "approve")
        self.assertEqual(self.material_master_service.calls, [("MAT-3000", "P100", "SAP-3000")])
        self.assertIn("execution_result", state)

    def test_approve_never_calls_the_repository_directly(self) -> None:
        thread_id, _state = self._run_to_first_pause(
            "Create material MAT-3000 at plant P100 with SAP number SAP-3000."
        )
        self.graph.invoke(Command(resume={"decision": "approve"}), config=_config(thread_id))

        for write_method in ("add_material", "add_material_master", "add_plant", "get_or_create_plant"):
            self.assertFalse(hasattr(self.master_data, write_method))


if __name__ == "__main__":
    unittest.main()
