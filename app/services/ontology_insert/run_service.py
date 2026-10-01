"""Orchestrates the ontology-insert POC's LangGraph runs -- the only
caller of `graph.invoke`/`Command(resume=...)` for this graph, matching
`OntologyUpdateRunService`'s exact convention.

Deliberately self-contained, same confirmed decision as the UPDATE POC: no
`process.workflow_thread`/`human_action` row is read or written anywhere
in this file. `Container.checkpointer` (the same process-lifetime
`PostgresSaver` shared with CMIR/PO-validation/ontology-update) is reused
unchanged; this file adds no new persistence mechanism.

Unlike `OntologyUpdateRunService`, `submit_decision` here can resume either
of two different paused interrupts on the same thread_id (a
missing-required-fields request, or the final approval) -- which one is
current is read back from `graph.get_state(...).next` (the node about to
re-run) so a client sending the wrong payload kind for the graph's current
state gets a clear 422, not a silently misinterpreted resume (see
`app/agents/ontology_insert/nodes.py`'s module docstring for why the graph
has two interrupt points).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from app.core.exceptions import NotFoundError, ValidationError
from app.schemas.ontology.insert_request import OntologyInsertResponse, OntologyInsertStatus

INTERRUPT_KEY = "__interrupt__"

# Namespaced distinctly from CMIR's "thread_...", PO-validation's
# "thread_po_...", and ontology-update's "thread_ontology_update_..." so
# thread_ids can never collide across domains in the shared checkpointer.
_THREAD_PREFIX = "thread_ontology_insert_"

_MISSING_DETAILS_NODE = "request_missing_details"
_APPROVAL_NODE = "human_approval"


class OntologyInsertRunService:
    def __init__(
        self, *, unit_of_work_factory: Callable[[], AbstractContextManager[SimpleNamespace]]
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    @staticmethod
    def _new_thread_id() -> str:
        return f"{_THREAD_PREFIX}{uuid4().hex[:12]}"

    @staticmethod
    def _thread_config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    def start(self, message: str) -> OntologyInsertResponse:
        thread_id = self._new_thread_id()
        with self._unit_of_work_factory() as uow:
            state = uow.graph.invoke({"user_request": message}, config=self._thread_config(thread_id))
        return self._to_response(thread_id, state)

    def submit_decision(
        self, thread_id: str, *, decision: str | None = None, details: dict[str, str] | None = None
    ) -> OntologyInsertResponse:
        with self._unit_of_work_factory() as uow:
            snapshot = uow.graph.get_state(self._thread_config(thread_id))
            if not snapshot.next:
                raise NotFoundError(
                    code="ONTOLOGY_INSERT_THREAD_NOT_FOUND",
                    message=f"No pending ontology-insert approval for thread_id={thread_id!r}.",
                )

            paused_node = snapshot.next[0]
            if paused_node == _MISSING_DETAILS_NODE:
                if details is None:
                    raise ValidationError(
                        code="ONTOLOGY_INSERT_DETAILS_REQUIRED",
                        message=f"Thread {thread_id!r} is awaiting missing field details, not a decision.",
                    )
                resume_payload: dict[str, Any] = {"details": details}
            elif paused_node == _APPROVAL_NODE:
                if decision is None:
                    raise ValidationError(
                        code="ONTOLOGY_INSERT_DECISION_REQUIRED",
                        message=f"Thread {thread_id!r} is awaiting an approve/reject decision, not details.",
                    )
                resume_payload = {"decision": decision}
            else:  # pragma: no cover - defensive; every interrupt node is one of the two above
                raise ValidationError(
                    code="ONTOLOGY_INSERT_UNEXPECTED_STATE",
                    message=f"Thread {thread_id!r} is paused at an unrecognized node {paused_node!r}.",
                )

            state = uow.graph.invoke(Command(resume=resume_payload), config=self._thread_config(thread_id))
        return self._to_response(thread_id, state)

    @staticmethod
    def _to_response(thread_id: str, state: dict[str, Any]) -> OntologyInsertResponse:
        if state.get(INTERRUPT_KEY):
            payload = state[INTERRUPT_KEY][0].value
            if payload.get("reason") == "missing_required_fields":
                return OntologyInsertResponse(
                    thread_id=thread_id,
                    status=OntologyInsertStatus.AWAITING_DETAILS,
                    missing_fields=payload["missing_fields"],
                )
            return OntologyInsertResponse(
                thread_id=thread_id,
                status=OntologyInsertStatus.AWAITING_APPROVAL,
                proposal=payload["proposal"],
            )
        if state.get("error"):
            return OntologyInsertResponse(
                thread_id=thread_id, status=OntologyInsertStatus.FAILED, error=state["error"]
            )
        if state.get("approval_status") == "reject":
            return OntologyInsertResponse(thread_id=thread_id, status=OntologyInsertStatus.REJECTED)
        if state.get("execution_result"):
            return OntologyInsertResponse(
                thread_id=thread_id,
                status=OntologyInsertStatus.COMPLETED,
                execution_result=state["execution_result"],
            )
        # Should be unreachable given the graph's own routing (every path
        # ends in one of the above) -- surfaced as a failure, not a silent
        # 200 with no useful information, if it ever is reached.
        return OntologyInsertResponse(
            thread_id=thread_id,
            status=OntologyInsertStatus.FAILED,
            error={
                "error_type": "unrecognized_graph_outcome",
                "error_message": "The graph ended without producing a recognized outcome.",
            },
        )
