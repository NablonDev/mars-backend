"""Aggregates all v1 API routers under the /api/v1 prefix."""

from fastapi import APIRouter, Depends

from app.api.dependencies import require_internal_api_key
from app.api.v1 import (
    admin,
    cmir,
    health,
    internal,
    job_runs,
    po_validation,
    processing_errors,
    workflow_threads,
)
from app.api.v1.common import delivery_change_requests, fulfillment, master_data, purchase_orders
from app.api.v1.penalties import (
    actual_penalties,
    disputes,
    mitigations,
    projections,
    rule_extraction,
    rules,
)

router = APIRouter()
router.include_router(health.router)

protected_router = APIRouter(dependencies=[Depends(require_internal_api_key)])
protected_router.include_router(cmir.router)
protected_router.include_router(internal.router)
protected_router.include_router(po_validation.router)
protected_router.include_router(workflow_threads.router)
protected_router.include_router(processing_errors.router)
protected_router.include_router(master_data.router)
protected_router.include_router(purchase_orders.router)
protected_router.include_router(fulfillment.router)
protected_router.include_router(delivery_change_requests.router)
protected_router.include_router(rules.router)
protected_router.include_router(rule_extraction.router)
protected_router.include_router(projections.router)
protected_router.include_router(mitigations.router)
protected_router.include_router(actual_penalties.router)
protected_router.include_router(disputes.router)
protected_router.include_router(job_runs.router)
protected_router.include_router(admin.router)

router.include_router(protected_router)
