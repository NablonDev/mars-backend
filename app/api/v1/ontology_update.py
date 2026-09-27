"""API endpoints for the ontology-update POC's LangGraph HITL agent
(`app/agents/ontology_update/`). Thin per `docs/ARCHITECTURE.md`'s own
rule for backend routes: parse the request, call the matching service
method, wrap the result in `Envelope[...]` -- never touches
`graph.invoke`, a repository, or Postgres directly. See
`app.services.ontology_update.run_service.OntologyUpdateRunService`,
the only caller of `graph.invoke`/`Command(resume=...)` for this graph.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_ontology_update_service
from app.core.envelope import Envelope, success_envelope
from app.schemas.ontology.update_request import (
    OntologyUpdateDecisionRequest,
    OntologyUpdateResponse,
    StartOntologyUpdateRequest,
)
from app.services.ontology_update.run_service import OntologyUpdateRunService

router = APIRouter(tags=["ontology-update"])


@router.post(
    "/ontology-update/requests",
    response_model=Envelope[OntologyUpdateResponse],
    status_code=202,
)
def start_ontology_update(
    body: StartOntologyUpdateRequest,
    service: OntologyUpdateRunService = Depends(get_ontology_update_service),
) -> Envelope[OntologyUpdateResponse]:
    result = service.start(body.message)
    return success_envelope(result)


@router.post(
    "/ontology-update/requests/{thread_id}/decisions",
    response_model=Envelope[OntologyUpdateResponse],
)
def submit_ontology_update_decision(
    thread_id: str,
    body: OntologyUpdateDecisionRequest,
    service: OntologyUpdateRunService = Depends(get_ontology_update_service),
) -> Envelope[OntologyUpdateResponse]:
    result = service.submit_decision(thread_id, body.decision)
    return success_envelope(result)
