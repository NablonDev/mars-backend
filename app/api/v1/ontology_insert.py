"""API endpoints for the ontology-insert POC's LangGraph HITL agent
(`app/agents/ontology_insert/`). Thin, matching `ontology_update.py`'s own
rule: parse the request, call the matching service method, wrap the result
in `Envelope[...]` -- never touches `graph.invoke`, a repository, or
Postgres directly. See
`app.services.ontology_insert.run_service.OntologyInsertRunService`, the
only caller of `graph.invoke`/`Command(resume=...)` for this graph.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_ontology_insert_service
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import ValidationError
from app.schemas.ontology.insert_request import (
    OntologyInsertDecisionRequest,
    OntologyInsertResponse,
    StartOntologyInsertRequest,
)
from app.services.ontology_insert.run_service import OntologyInsertRunService

router = APIRouter(tags=["ontology-insert"])


@router.post(
    "/ontology-insert/requests",
    response_model=Envelope[OntologyInsertResponse],
    status_code=202,
)
def start_ontology_insert(
    body: StartOntologyInsertRequest,
    service: Annotated[OntologyInsertRunService, Depends(get_ontology_insert_service)],
) -> Envelope[OntologyInsertResponse]:
    result = service.start(body.message)
    return success_envelope(result)


@router.post(
    "/ontology-insert/requests/{thread_id}/decisions",
    response_model=Envelope[OntologyInsertResponse],
)
def submit_ontology_insert_decision(
    thread_id: str,
    body: OntologyInsertDecisionRequest,
    service: Annotated[OntologyInsertRunService, Depends(get_ontology_insert_service)],
) -> Envelope[OntologyInsertResponse]:
    if (body.decision is None) == (body.details is None):
        raise ValidationError(
            code="ONTOLOGY_INSERT_DECISION_OR_DETAILS_REQUIRED",
            message="Provide exactly one of 'decision' or 'details'.",
        )
    result = service.submit_decision(thread_id, decision=body.decision, details=body.details)
    return success_envelope(result)
