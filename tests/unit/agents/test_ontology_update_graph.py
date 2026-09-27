"""Graph-level tests for the ontology-update POC. A real
`graph.invoke(...)` run through interrupt and resume, using a real
`MemorySaver` checkpointer, matching
`tests/unit/agents/test_po_validation_graph.py`'s convention exactly (no
mocking of `graph.invoke` itself). Self-contained checkpoint/thread_id --
no `process.workflow_thread`/`human_action` row is created or touched
anywhere in this file (per the confirmed Checkpoint 1 decision).

Checklist items H/I/J/K from the Phase 2 POC task are each their own test.
Checkpoint 3 adds the approve-executes / reject-never-executes pair,
proving the write only happens through `MaterialMasterService`, never
`MasterDataRepository` directly (`FakeMasterDataRepository` has no write
method at all -- structurally cannot be written to by this graph).
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.ontology_update.graph import build_ontology_update_graph
from app.agents.ontology_update.nodes import OntologyUpdateNodes
from app.services.ontology.context_service import OntologyContextService
from tests.unit.agents.test_ontology_update_nodes import FakeMasterDataRepository, FakeMaterialMasterService

INTERRUPT_KEY = "__interrupt__"


class FakeTraceRepo:
    def log(self, *args, **kwargs):
        pass


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class OntologyUpdateGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master_data = FakeMasterDataRepository()
        self.source = self.master_data.add_material("MAT-DISC-1001")
        self.replacement = self.master_data.add_material("MAT-REPL-1001")
        self.master_master = self.master_data.add_material_master(
            material_id=self.source["id"], plant_id=uuid4()
        )
        self.material_master_service = FakeMaterialMasterService()
        self.material_master_service.register_material_master(self.master_master)

        nodes = OntologyUpdateNodes(
            context_service=OntologyContextService(),
            master_data_repository=self.master_data,
            material_master_service=self.material_master_service,
        )
        self.graph = build_ontology_update_graph(nodes, MemorySaver(), FakeTraceRepo())

    def _run_to_interrupt(self) -> tuple[str, dict]:
        thread_id = f"test-{uuid4().hex[:8]}"
        state = self.graph.invoke(
            {"user_request": "Update the replacement material for MAT-DISC-1001 to MAT-REPL-1001."},
            config=_config(thread_id),
        )
        return thread_id, state

    # ---- H. Interrupt ----

    def test_graph_invocation_returns_interrupt_with_reason_and_proposal(self) -> None:
        _thread_id, state = self._run_to_interrupt()

        self.assertIn(INTERRUPT_KEY, state)
        payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(payload["reason"], "material_master_update_approval")
        self.assertIn("proposal", payload)
        self.assertEqual(payload["proposal"]["target"]["material_code"], "MAT-DISC-1001")
        self.assertEqual(payload["proposal"]["relationship"]["name"], "succeededBy")
        self.assertEqual(payload["proposal"]["new_value"]["material_code"], "MAT-REPL-1001")

    def test_ambiguous_material_master_short_circuits_before_any_interrupt(self) -> None:
        self.master_data.add_material_master(material_id=self.source["id"], plant_id=uuid4())
        _thread_id, state = self._run_to_interrupt()

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertTrue(state.get("clarification_required"))

    def test_unknown_source_identifier_ends_cleanly_with_an_error_not_an_interrupt(self) -> None:
        thread_id = f"test-{uuid4().hex[:8]}"
        state = self.graph.invoke(
            {"user_request": "Update the replacement material for MAT-NOPE to MAT-REPL-1001."},
            config=_config(thread_id),
        )
        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(state["error"]["error_type"], "unknown_source_identifier")

    # ---- I. Verify NO WRITE happens before the interrupt is reached/approved ----

    def test_no_repository_write_method_exists_and_service_is_untouched_before_approval(self) -> None:
        self._run_to_interrupt()
        for write_method in ("update_material_master_follow_up", "update", "save", "commit"):
            self.assertFalse(hasattr(self.master_data, write_method))
        self.assertEqual(self.material_master_service.calls, [])

    # ---- J. Resume rejection ----

    def test_reject_terminates_without_write_and_never_calls_the_service(self) -> None:
        thread_id, _state = self._run_to_interrupt()

        state = self.graph.invoke(Command(resume={"decision": "reject"}), config=_config(thread_id))

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(state["approval_status"], "reject")
        self.assertNotIn("execution_result", state)
        self.assertEqual(self.material_master_service.calls, [])

    # ---- K. Resume approval -- Checkpoint 3: this now actually executes ----

    def test_approve_resumes_and_calls_the_service_exactly_once_with_resolved_ids(self) -> None:
        thread_id, _state = self._run_to_interrupt()

        state = self.graph.invoke(Command(resume={"decision": "approve"}), config=_config(thread_id))

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(state["approval_status"], "approve")
        self.assertEqual(self.material_master_service.calls, [(self.master_master["id"], self.replacement["id"])])
        self.assertEqual(state["execution_result"]["material_master_id"], str(self.master_master["id"]))
        self.assertEqual(state["execution_result"]["follow_up_material_id"], str(self.replacement["id"]))

    def test_approve_never_calls_the_repository_directly(self) -> None:
        # FakeMasterDataRepository (injected for the earlier read-only
        # nodes) has no write method at all -- so even on the approve path,
        # there is nothing for execute_update to have called on it.
        thread_id, _state = self._run_to_interrupt()
        self.graph.invoke(Command(resume={"decision": "approve"}), config=_config(thread_id))

        for write_method in ("update_material_master_follow_up", "update", "save", "commit"):
            self.assertFalse(hasattr(self.master_data, write_method))


if __name__ == "__main__":
    unittest.main()
