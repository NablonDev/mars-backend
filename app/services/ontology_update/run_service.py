"""Orchestrates the ontology-update POC's LangGraph runs -- the only
caller of `graph.invoke`/`Command(resume=...)` for this graph, matching
`CmirService`/`PoValidationService`'s exact convention (see
`docs/ARCHITECTURE.md` §1: "Services... are the only callers of
`graph.invoke`/`Command(resume=...)` in the whole system").

Deliberately self-contained, per the confirmed Checkpoint 1 decision: no
`process.workflow_thread`/`human_action` row is read or written anywhere
in this file. The checkpoint `thread_id` this service mints *is* the only
persistent handle a caller needs to resume a paused run -- there is no
separate "pending action" row to look up first. `Container.checkpointer`
(one process-lifetime `PostgresSaver`, shared with the CMIR/PO-validation
graphs) is reused unchanged; this file adds no new persistence mechanism.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from app.core.exceptions import NotFoundError
from app.schemas.ontology.update_request import OntologyUpdateResponse, OntologyUpdateStatus

INTERRUPT_KEY = "__interrupt__"

# Namespaced distinctly from CMIR's "thread_..." and PO-validation's
# "thread_po_..." (see Container's own docstring for that precedent) so
# thread_ids can never collide across domains in the shared checkpointer.
_THREAD_PREFIX = "thread_ontology_update_"


class OntologyUpdateRunService:
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

    def start(self, message: str) -> OntologyUpdateResponse:
        thread_id = self._new_thread_id()
        with self._unit_of_work_factory() as uow:
            state = uow.graph.invoke({"user_request": message}, config=self._thread_config(thread_id))
        return self._to_response(thread_id, state)

    def submit_decision(self, thread_id: str, decision: str) -> OntologyUpdateResponse:
        with self._unit_of_work_factory() as uow:
            snapshot = uow.graph.get_state(self._thread_config(thread_id))
            if not snapshot.next:
                # No paused task waiting on this thread_id -- either it
                # never existed, already ran to completion, or was already
                # resumed once. Either way, there is nothing left to
                # resume; this is the one genuine HTTP error this service
                # raises (a caller mistake, not a graph outcome).
                raise NotFoundError(
                    code="ONTOLOGY_UPDATE_THREAD_NOT_FOUND",
                    message=f"No pending ontology-update approval for thread_id={thread_id!r}.",
                )
            state = uow.graph.invoke(
                Command(resume={"decision": decision}), config=self._thread_config(thread_id)
            )
        return self._to_response(thread_id, state)

    @staticmethod
    def _to_response(thread_id: str, state: dict[str, Any]) -> OntologyUpdateResponse:
        if state.get(INTERRUPT_KEY):
            payload = state[INTERRUPT_KEY][0].value
            return OntologyUpdateResponse(
                thread_id=thread_id,
                status=OntologyUpdateStatus.AWAITING_APPROVAL,
                proposal=payload["proposal"],
            )
        if state.get("clarification_required"):
            return OntologyUpdateResponse(
                thread_id=thread_id,
                status=OntologyUpdateStatus.CLARIFICATION_REQUIRED,
                clarification_reason=state.get("clarification_reason"),
            )
        if state.get("error"):
            return OntologyUpdateResponse(
                thread_id=thread_id, status=OntologyUpdateStatus.FAILED, error=state["error"]
            )
        if state.get("approval_status") == "reject":
            return OntologyUpdateResponse(thread_id=thread_id, status=OntologyUpdateStatus.REJECTED)
        if state.get("execution_result"):
            return OntologyUpdateResponse(
                thread_id=thread_id,
                status=OntologyUpdateStatus.COMPLETED,
                execution_result=state["execution_result"],
            )
        # Should be unreachable given the graph's own routing (every path
        # ends in one of the above) -- surfaced as a failure, not a silent
        # 200 with no useful information, if it ever is reached.
        return OntologyUpdateResponse(
            thread_id=thread_id,
            status=OntologyUpdateStatus.FAILED,
            error={
                "error_type": "unrecognized_graph_outcome",
                "error_message": "The graph ended without producing a recognized outcome.",
            },
        )
