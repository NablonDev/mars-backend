"""API endpoints for the CMIR <-> Material Master traceability graph
(component B of the ontology plan -- no reasoning/inference here, see
`app.services.ontology.graph_service`) and the semantic context layer
(Phase 1G -- schema/relationship metadata, see
`app.services.ontology.context_service`). Neither performs CRUD."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.dependencies import get_ontology_context_service, get_ontology_graph_service
from app.core.envelope import Envelope, success_envelope
from app.schemas.ontology.context import EntityContext, RelationshipContext
from app.schemas.ontology.traceability import SuccessorChainResult, TraceabilityResult
from app.services.ontology.context_service import OntologyContextService
from app.services.ontology.graph_service import OntologyGraphService

router = APIRouter(tags=["ontology"])


@router.get(
    "/ontology/materials/{material_code}/traceability",
    response_model=Envelope[TraceabilityResult],
)
def get_material_traceability(
    material_code: str,
    graph_service: Annotated[OntologyGraphService, Depends(get_ontology_graph_service)],
) -> Envelope[TraceabilityResult]:
    result = graph_service.find_cmir_records_for_material(material_code)
    return success_envelope(result)


@router.get(
    "/ontology/material-masters/{material_master_id}/successor-chain",
    response_model=Envelope[SuccessorChainResult],
)
def get_successor_chain(
    material_master_id: UUID,
    graph_service: Annotated[OntologyGraphService, Depends(get_ontology_graph_service)],
) -> Envelope[SuccessorChainResult]:
    result = graph_service.successor_chain(material_master_id)
    return success_envelope(result)


@router.get(
    "/ontology/entities/{entity_name}",
    response_model=Envelope[EntityContext],
)
def get_entity_context(
    entity_name: str,
    context_service: Annotated[OntologyContextService, Depends(get_ontology_context_service)],
) -> Envelope[EntityContext]:
    result = context_service.get_entity_context(entity_name)
    return success_envelope(result)


@router.get(
    "/ontology/entities/{entity_name}/relationships",
    response_model=Envelope[list[RelationshipContext]],
)
def get_entity_relationships(
    entity_name: str,
    context_service: Annotated[OntologyContextService, Depends(get_ontology_context_service)],
) -> Envelope[list[RelationshipContext]]:
    result = context_service.get_relationships(entity_name)
    return success_envelope(result)
