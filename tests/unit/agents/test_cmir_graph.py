from __future__ import annotations

import unittest

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.cmir.graph import build_graph
from app.agents.cmir.nodes import WorkflowNodes
from app.repositories.cmir.cmir_record import CmirVersionConflict
from app.schemas.cmir import Cmir
from app.services.cmir.validation import CmirValidator

INTERRUPT_KEY = "__interrupt__"


class FakeTraceRepo:
    def log(self, *args, **kwargs):
        pass


class FakeEmailReader:
    def __init__(self, *, fail_mark_as_read: bool = False) -> None:
        self.marked_read = []
        self._fail_mark_as_read = fail_mark_as_read

    def fetch_unread(self, **kwargs):
        return []

    def mark_as_read(self, imap_id):
        if self._fail_mark_as_read:
            raise RuntimeError("STORE command error: BAD [b'Could not parse command']")
        self.marked_read.append(imap_id)


class FakeExtractor:
    def __init__(self, cmir: Cmir) -> None:
        self.cmir = cmir

    def extract(self, body):
        return self.cmir


class FakeEmailRepository:
    def __init__(self) -> None:
        self._next_id = 1
        self.extractions = []

    def save(self, *, sender, subject, raw_content, source_message_id=None, source_imap_id=None):
        email_id = f"email-{self._next_id}"
        self._next_id += 1
        return email_id

    def update_extraction(self, email_id, extracted_json, missing_fields, status):
        self.extractions.append((email_id, extracted_json, missing_fields, status))


class FakeCmirRepository:
    def __init__(self, current=None, conflict: bool = False) -> None:
        self.current = current
        self.conflict = conflict
        self.inserted = []
        self.lookups = []

    def get_current(self, customer_identity, target_customer_material_ref):
        # Mirrors the real repository's WHERE clause: only match if both identity
        # fields agree with the configured record, so a blank/wrong lookup key
        # correctly misses -- unlike a naive fake that ignores its arguments.
        self.lookups.append((customer_identity, target_customer_material_ref))
        if self.current is None:
            return None
        if customer_identity == self.current.get(
            "customer_identity"
        ) and target_customer_material_ref == self.current.get("target_customer_material_ref"):
            return self.current
        return None

    def supersede_and_insert(
        self, *, customer_identity, target_customer_material_ref, merged, expected_current_id
    ):
        if self.conflict:
            raise CmirVersionConflict("simulated concurrent write")
        self.inserted.append(merged)
        return 999


class FakeActionLogRepository:
    def __init__(self) -> None:
        self.logged = []

    def log(self, email_id, action, actor, details):
        self.logged.append((email_id, action, actor, details))


def _email_payload(**overrides):
    payload = {
        "imap_id": "imap-1",
        "sender": "customer@example.com",
        "subject": "CMIR Request",
        "body": "please update our CMIR",
        "source_message_id": "msg-001",
        "mark_read": True,
    }
    payload.update(overrides)
    return payload


class CmirWorkflowGraphTests(unittest.TestCase):
    def _build(
        self, *, current=None, conflict: bool = False, extractor_cmir: Cmir, fail_mark_as_read: bool = False
    ):
        self.email_reader = FakeEmailReader(fail_mark_as_read=fail_mark_as_read)
        self.email_repository = FakeEmailRepository()
        self.cmir_repository = FakeCmirRepository(current=current, conflict=conflict)
        self.action_log = FakeActionLogRepository()
        nodes = WorkflowNodes(
            email_reader=self.email_reader,
            extractor=FakeExtractor(extractor_cmir),
            validator=CmirValidator(),
            email_repository=self.email_repository,
            cmir_repository=self.cmir_repository,
            action_log_repository=self.action_log,
        )
        return build_graph(nodes, MemorySaver(), FakeTraceRepo())

    def _config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def _initial_state(self, thread_id: str) -> dict:
        return {
            "batch_id": "batch-1",
            "email": _email_payload(),
            "cmir": {},
            "decision": None,
            "run_id": "00000000-0000-0000-0000-000000000001",
            "thread_id": thread_id,
        }

    def test_create_path_shows_full_diff_from_blank_and_commits_on_approval(self) -> None:
        complete_cmir = Cmir(
            sender_type="external",
            customer_identity="Acme Manufacturing Ltd",
            material_identity="Polyethylene Resin PE-200",
            intent_phrase="update",
            existing_cmir_ref="CMIR-1",
            brand="AcmePlast",
            site="Site 12",
            target_customer_material_ref="ACME-PE200-STD",
        )
        graph = self._build(current=None, extractor_cmir=complete_cmir)

        state = graph.invoke(self._initial_state("t1"), config=self._config("t1"))

        self.assertIn(INTERRUPT_KEY, state)
        payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(payload["reason"], "approval_required")
        self.assertIsNone(payload["existing_cmir"])
        self.assertEqual(payload["diff"]["brand"], {"from": "", "to": "AcmePlast"})

        state = graph.invoke(Command(resume={"decision": "approve"}), config=self._config("t1"))

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(len(self.cmir_repository.inserted), 1)
        self.assertEqual(self.cmir_repository.inserted[0]["brand"], "AcmePlast")

    def test_update_path_merges_onto_active_record_and_shows_partial_diff(self) -> None:
        existing = {
            "id": 42,
            "sender_type": "external",
            "customer_identity": "Acme Manufacturing Ltd",
            "material_identity": "Polyethylene Resin PE-200",
            "intent_phrase": "update",
            "existing_cmir_ref": "CMIR-2025-0099",
            "brand": "AcmePlast",
            "site": "Site 12",
            "target_grd_code": "GRD-01",
            "target_customer_material_ref": "ACME-PE200-STD",
            "effective_date": "2026-01-01",
            "reason": "Annual refresh",
        }
        # This email only mentions the identity fields (needed for the lookup) plus a
        # new effective_date -- everything else comes back blank from "extraction."
        partial_cmir = Cmir(
            customer_identity="Acme Manufacturing Ltd",
            target_customer_material_ref="ACME-PE200-STD",
            effective_date="2026-09-01",
        )
        graph = self._build(current=existing, extractor_cmir=partial_cmir)

        state = graph.invoke(self._initial_state("t2"), config=self._config("t2"))

        payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(payload["existing_cmir"], existing)
        self.assertEqual(payload["diff"], {"effective_date": {"from": "2026-01-01", "to": "2026-09-01"}})
        # Fields the email left blank carried forward from the active record, so the
        # merged draft that reached human_approval is already complete -- no
        # missing-fields interrupt happened first.
        self.assertEqual(payload["cmir"]["brand"], "AcmePlast")

        graph.invoke(Command(resume={"decision": "approve"}), config=self._config("t2"))

        self.assertEqual(self.cmir_repository.inserted[0]["brand"], "AcmePlast")
        self.assertEqual(self.cmir_repository.inserted[0]["effective_date"], "2026-09-01")

    def test_missing_mandatory_field_loops_back_through_identify_existing_cmir(self) -> None:
        # customer_identity and brand (both mandatory) come back blank from
        # "extraction" -- this must trigger collect_missing_fields. The human only
        # supplies customer_identity; brand must then be filled in by merging against
        # whatever identify_existing_cmir finds on the *next* pass (not the original,
        # wrongly-blank lookup) -- that's what should make the merged draft complete
        # enough to reach human_approval without a second missing-fields round trip.
        incomplete_cmir = Cmir(
            sender_type="external",
            material_identity="Polyethylene Resin PE-200",
            intent_phrase="update",
            existing_cmir_ref="CMIR-1",
            site="Site 12",
            target_customer_material_ref="ACME-PE200-STD",
        )
        existing = {
            "id": 7,
            "customer_identity": "Acme Manufacturing Ltd",
            "target_customer_material_ref": "ACME-PE200-STD",
            "brand": "OldBrand",
        }
        graph = self._build(current=existing, extractor_cmir=incomplete_cmir)

        state = graph.invoke(self._initial_state("t3"), config=self._config("t3"))

        self.assertIn(INTERRUPT_KEY, state)
        first_payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(first_payload["reason"], "missing_mandatory_fields")
        self.assertIn("customer_identity", first_payload["missing_fields"])

        # Supplying the missing identity should re-trigger identify_existing_cmir with
        # the now-known customer_identity, correctly finding `existing` this time.
        state = graph.invoke(
            Command(resume={"customer_identity": "Acme Manufacturing Ltd"}),
            config=self._config("t3"),
        )

        self.assertIn(INTERRUPT_KEY, state)
        second_payload = state[INTERRUPT_KEY][0].value
        self.assertEqual(second_payload["reason"], "approval_required")
        self.assertEqual(second_payload["existing_cmir"], existing)
        # brand was never supplied (only customer_identity was), so it must have
        # carried forward from the record identify_existing_cmir found on this
        # second pass -- proof the loop-back re-ran the lookup instead of reusing
        # the first (wrongly blank) one.
        self.assertEqual(second_payload["cmir"]["brand"], "OldBrand")

    def test_version_conflict_routes_to_handle_version_conflict_without_crashing(self) -> None:
        complete_cmir = Cmir(
            sender_type="external",
            customer_identity="Acme Manufacturing Ltd",
            material_identity="Polyethylene Resin PE-200",
            intent_phrase="update",
            existing_cmir_ref="CMIR-1",
            brand="AcmePlast",
            site="Site 12",
            target_customer_material_ref="ACME-PE200-STD",
        )
        graph = self._build(current=None, conflict=True, extractor_cmir=complete_cmir)

        graph.invoke(self._initial_state("t4"), config=self._config("t4"))
        state = graph.invoke(Command(resume={"decision": "approve"}), config=self._config("t4"))

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(len(self.cmir_repository.inserted), 0)
        self.assertTrue(any(action == "CMIR Version Conflict" for _, action, _, _ in self.action_log.logged))
        # mark_email_read still ran -- the conflict path is terminal, not a crash.
        self.assertIn("imap-1", self.email_reader.marked_read)

    def test_rejected_decision_still_marks_email_read(self) -> None:
        complete_cmir = Cmir(
            sender_type="external",
            customer_identity="Acme Manufacturing Ltd",
            material_identity="Polyethylene Resin PE-200",
            intent_phrase="update",
            existing_cmir_ref="CMIR-1",
            brand="AcmePlast",
            site="Site 12",
            target_customer_material_ref="ACME-PE200-STD",
        )
        graph = self._build(current=None, extractor_cmir=complete_cmir)

        graph.invoke(self._initial_state("t5"), config=self._config("t5"))
        graph.invoke(
            Command(resume={"decision": "reject", "reason": "not needed"}), config=self._config("t5")
        )

        self.assertEqual(len(self.cmir_repository.inserted), 0)
        self.assertIn("imap-1", self.email_reader.marked_read)

    def test_approval_still_commits_when_marking_email_read_fails(self) -> None:
        """Regression guard: mark_email_read runs last on every decision path,
        in the same graph.invoke() call (and DB transaction) as the approval
        that already committed in an earlier node -- an IMAP failure here
        (stale/reused sequence number, deleted message, transient network
        issue, ...) must not roll back a reviewer's already-recorded
        decision. Previously this raised and aborted the whole graph.invoke(),
        which meant the approval was silently never persisted."""
        complete_cmir = Cmir(
            sender_type="external",
            customer_identity="Acme Manufacturing Ltd",
            material_identity="Polyethylene Resin PE-200",
            intent_phrase="update",
            existing_cmir_ref="CMIR-1",
            brand="AcmePlast",
            site="Site 12",
            target_customer_material_ref="ACME-PE200-STD",
        )
        graph = self._build(current=None, extractor_cmir=complete_cmir, fail_mark_as_read=True)

        graph.invoke(self._initial_state("t6"), config=self._config("t6"))
        state = graph.invoke(Command(resume={"decision": "approve"}), config=self._config("t6"))

        self.assertNotIn(INTERRUPT_KEY, state)
        self.assertEqual(len(self.cmir_repository.inserted), 1)
        self.assertEqual([], self.email_reader.marked_read)


if __name__ == "__main__":
    unittest.main()
