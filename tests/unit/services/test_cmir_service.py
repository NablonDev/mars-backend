"""Tests for CmirService against the real SQLite-backed `process`/`cmir`
repositories (see tests/conftest.py's `repos` fixture), with a
hand-written fake LangGraph graph standing in for the actual compiled
graph -- the graph itself (`app/agents/cmir/{graph,nodes}.py`) is Phase
4's concern and stays out of scope here; these tests exercise only the
service's own persistence/bookkeeping around whatever the graph returns.

Was against hand-rolled `FakeWorkflowThreads`/`FakePendingActions`/
`FakeHITLState` doubles matching the pre-restructure repository method
shapes; rewritten against the real `WorkflowThreadRepository`/
`HumanActionRepository`/`AgentRunRepository`/`AgentRegistryRepository`/
`CmirRecordRepository`/`JobQueueRepository`/`Cmir{JobItem,JobRun}Context
Repository` instead of re-implementing fakes for every method signature
this phase's schema restructure changed.

Every test first drives one email through `_process_email_thread` (the
service's own internal entry point -- there is no public "conjure a thread
already parked at stage X" API, same posture the old fakes' constructors
took) to get a REAL `workflow_thread`/`human_action` row parked at the
interrupt reason under test, then exercises a resume call against it.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import pytest

from app.core.exceptions import ConflictError, ExternalServiceError, ValidationError
from app.services.cmir.service import CmirService


class _FakeInterrupt:
    def __init__(self, value: dict) -> None:
        self.value = value


class FakeGraph:
    """A configurable stand-in for the compiled LangGraph graph --
    `invoke_plan` is a list of state-dicts (or Exceptions to raise),
    consumed one per call to `.invoke()`; running past the end repeats the
    last entry."""

    def __init__(self, invoke_plan: list[Any] | None = None) -> None:
        self.invoke_plan = invoke_plan or [{}]
        self.invocations: list[tuple[dict, dict]] = []
        self.updated_states: list[tuple[dict, dict]] = []
        self._call_count = 0

    def invoke(self, value, config=None):
        self.invocations.append((copy.deepcopy(value), copy.deepcopy(config)))
        index = min(self._call_count, len(self.invoke_plan) - 1)
        self._call_count += 1
        result = self.invoke_plan[index]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def update_state(self, config, value):
        self.updated_states.append((copy.deepcopy(config), copy.deepcopy(value)))


def _build_service(repos, graph: FakeGraph) -> CmirService:
    return CmirService(
        email_reader=None,
        graph=graph,
        agent_registry=repos.agent_registry,
        agent_runs=repos.agent_runs,
        workflow_threads=repos.workflow_threads,
        human_actions=repos.human_actions,
        cmir_records=repos.cmir_records,
        job_queue=repos.job_queue,
        job_run_context=repos.cmir_job_run_context,
        job_item_context=repos.cmir_job_item_context,
    )


def _new_email(repos) -> UUID:
    return repos.emails.save(sender="customer@example.com", subject="CMIR Request 1", raw_content="body")


def _interrupt_result(reason: str, *, email_id: UUID, cmir: dict | None = None) -> dict:
    payload = {"reason": reason, "email_id": str(email_id), "cmir": cmir or {}}
    return {
        "__interrupt__": [_FakeInterrupt(payload)],
        "email_id": email_id,
        "cmir": cmir or {},
    }


def _start_thread_awaiting(
    repos,
    reason: str,
    email_id: UUID,
    *,
    resume_results: list[Any] | None = None,
) -> tuple[CmirService, FakeGraph, UUID]:
    """Runs one email through the service to create a real workflow_thread
    row parked at the given interrupt reason, returning
    (service, graph, thread_id). `resume_results` are queued as the
    graph's subsequent invoke() results, for the resume call the test
    itself will make."""
    graph = FakeGraph(
        [_interrupt_result(reason, email_id=email_id, cmir={"brand": "Brand A"}), *(resume_results or [])]
    )
    service = _build_service(repos, graph)
    result = service._process_email_thread("batch-1", {"placeholder": True}, existing_email_id=email_id)
    return service, graph, result["id"]


def test_list_runs_rejects_unknown_view(repos) -> None:
    service = _build_service(repos, FakeGraph())

    with pytest.raises(ValidationError) as raised:
        service.list_runs(view="senders")

    assert raised.value.code == "VALIDATION_ERROR"
    assert raised.value.status_code == 422


def test_update_draft_rejects_bad_field_name(repos) -> None:
    email_id = _new_email(repos)
    service, _graph, thread_id = _start_thread_awaiting(repos, "approval_required", email_id)
    stage = service.get_stage(thread_id)

    with pytest.raises(ValidationError) as raised:
        service.update_draft(
            thread_id,
            actor="reviewer@company.com",
            fields={"not_a_cmir_field": "x"},
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "VALIDATION_ERROR"


def test_update_draft_rejects_stale_timestamp(repos) -> None:
    email_id = _new_email(repos)
    service, _graph, thread_id = _start_thread_awaiting(repos, "approval_required", email_id)

    with pytest.raises(ConflictError) as raised:
        service.update_draft(
            thread_id,
            actor="reviewer@company.com",
            fields={"brand": "Brand B"},
            expected_updated_at="2000-01-01T00:00:00+00:00",
        )

    assert raised.value.code == "THREAD_STALE"


def test_reject_decision_requires_reason(repos) -> None:
    email_id = _new_email(repos)
    service, _graph, thread_id = _start_thread_awaiting(repos, "approval_required", email_id)
    stage = service.get_stage(thread_id)

    with pytest.raises(ValidationError) as raised:
        service.submit_decision(
            thread_id,
            actor="reviewer@company.com",
            decision="reject",
            reason="",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "VALIDATION_ERROR"


def test_missing_fields_resume_failure_keeps_pending_action_open(repos) -> None:
    email_id = _new_email(repos)
    service, _graph, thread_id = _start_thread_awaiting(
        repos, "missing_mandatory_fields", email_id, resume_results=[RuntimeError("checkpoint missing")]
    )
    stage = service.get_stage(thread_id)
    pending_before = repos.human_actions.get_open_for_thread(thread_id)

    with pytest.raises(ExternalServiceError) as raised:
        service.submit_missing_fields(
            thread_id,
            actor="reviewer@company.com",
            fields={"existing_cmir_ref": "CMIR-1"},
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "WORKFLOW_RESUME_FAILED"
    pending_after = repos.human_actions.get_open_for_thread(thread_id)
    assert pending_after is not None
    assert pending_after["id"] == pending_before["id"]
    assert service.get_stage(thread_id)["status"] == "waiting_missing_fields"


def test_missing_fields_success_rotates_to_approval_atomically(repos) -> None:
    email_id = _new_email(repos)
    approval_result = _interrupt_result(
        "approval_required",
        email_id=email_id,
        cmir={"brand": "Brand A", "existing_cmir_ref": "CMIR-1", "status": "pending_human_action"},
    )
    service, _graph, thread_id = _start_thread_awaiting(
        repos, "missing_mandatory_fields", email_id, resume_results=[approval_result]
    )
    stage = service.get_stage(thread_id)
    pending_before = repos.human_actions.get_open_for_thread(thread_id)

    response = service.submit_missing_fields(
        thread_id,
        actor="reviewer@company.com",
        fields={"existing_cmir_ref": "CMIR-1"},
        expected_updated_at=stage["updated_at"].isoformat(),
    )

    assert response["status"] == "waiting_approval"
    assert response["stage"] == "AWAITING_APPROVAL"
    pending_after = repos.human_actions.get_open_for_thread(thread_id)
    assert pending_after["interrupt_type"] == "approval_required"
    history = repos.human_actions.list_for_thread(thread_id)
    # The original (now-completed) pending action is still on record, first
    # by requested_at, and a new open one has replaced it atomically.
    assert history[0]["id"] == pending_before["id"]
    assert history[0]["status"] == "completed"


def test_submit_decision_conflict_closes_thread_and_raises_version_conflict(repos) -> None:
    email_id = _new_email(repos)
    conflict_result = {
        "decision": "approve",
        "cmir_write_result": "conflict",
        "cmir": {"customer_identity": "Acme Manufacturing Ltd"},
        "email_id": email_id,
    }
    service, _graph, thread_id = _start_thread_awaiting(
        repos, "approval_required", email_id, resume_results=[conflict_result]
    )
    stage = service.get_stage(thread_id)

    with pytest.raises(ConflictError) as raised:
        service.submit_decision(
            thread_id,
            actor="reviewer@company.com",
            decision="approve",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "CMIR_VERSION_CONFLICT"
    assert raised.value.status_code == 409
    # The thread must still close out consistently (mirrors approve/reject),
    # not be left dangling just because the API call raises.
    assert service.get_stage(thread_id)["status"] == "completed_conflict"


def test_update_draft_merges_against_freshly_fetched_active_record(repos) -> None:
    repos.cmir_records.supersede_and_insert(
        customer_identity="Acme Manufacturing Ltd",
        target_customer_material_ref="ACME-PE200-STD",
        merged={
            "sender_type": "",
            "customer_identity": "Acme Manufacturing Ltd",
            "material_identity": "MAT-1",
            "intent_phrase": None,
            "existing_cmir_ref": "",
            "brand": "",
            "site": "Site 12",
            "target_grd_code": "",
            "target_customer_material_ref": "ACME-PE200-STD",
            "effective_date": None,
            "reason": None,
        },
        expected_current_id=None,
    )
    current = repos.cmir_records.get_current("Acme Manufacturing Ltd", "ACME-PE200-STD")

    email_id = _new_email(repos)
    service, graph, thread_id = _start_thread_awaiting(repos, "approval_required", email_id)

    service.update_draft(
        thread_id,
        actor="reviewer@company.com",
        fields={
            "customer_identity": "Acme Manufacturing Ltd",
            "target_customer_material_ref": "ACME-PE200-STD",
        },
        expected_updated_at=service.get_stage(thread_id)["updated_at"].isoformat(),
    )

    _, pushed_state = graph.updated_states[-1]
    assert pushed_state["cmir_version_token"] == current["id"]
    assert pushed_state["existing_cmir"] == current
    # "site" was never in the reviewer's edit or the pre-existing draft
    # snapshot, so it must have carried forward from the freshly-fetched
    # active record.
    assert pushed_state["cmir"]["site"] == "Site 12"


def test_decision_resume_failure_keeps_pending_action_open(repos) -> None:
    email_id = _new_email(repos)
    service, _graph, thread_id = _start_thread_awaiting(
        repos, "approval_required", email_id, resume_results=[RuntimeError("checkpoint missing")]
    )
    stage = service.get_stage(thread_id)
    pending_before = repos.human_actions.get_open_for_thread(thread_id)

    with pytest.raises(ExternalServiceError) as raised:
        service.submit_decision(
            thread_id,
            actor="reviewer@company.com",
            decision="approve",
            expected_updated_at=stage["updated_at"].isoformat(),
        )

    assert raised.value.code == "WORKFLOW_RESUME_FAILED"
    pending_after = repos.human_actions.get_open_for_thread(thread_id)
    assert pending_after is not None
    assert pending_after["id"] == pending_before["id"]
