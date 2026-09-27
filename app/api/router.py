"""Aggregates all v1 API routers under the /api/v1 prefix.

Every router below is included on ``protected_router``, which carries the
``require_internal_api_key`` dependency at construction time -- FastAPI
applies an ``APIRouter(dependencies=...)`` to every route nested under it
however deeply, so a new domain router only has to be added here to be
covered automatically. ``/health`` is the one deliberate exception: it is
included directly on the unprotected top-level ``router`` instead, so load
balancers/uptime monitors can reach it with no key.

Phase 7a folder-split (approved plan §2/§5): the old flat/`fine_*`-prefixed
router modules (`fine_master_data`, `fine_rules`, `orders`,
`fine_projection/*`, `fine_mitigation/*`, `fine_runs`, `batches`) are gone,
replaced by `app.api.v1.common.{master_data,purchase_orders}` and
`app.api.v1.penalties.{rules,projections,mitigations,delivery_change_requests}`.
`admin` (seed/replay) stays a top-level module -- it spans both domains, not
either one exclusively.

Phase 7b (approved plan §6): `app.api.v1.cmir`/`po_validation` dropped their
stale `create_router()` factory in favor of a plain module-level `router`
(Phase 7a's convention). `app.api.v1.workflow_threads` and
`app.api.v1.processing_errors` are new -- `workflow_thread`/`processing_error`
are shared `process`-schema resources used by both domains, not owned by
either router.

`app.api.v1.job_runs` (was `app.api.v1.penalties.batches`) moved out of the
`penalties`-owned folder for the same reason: it operates on the shared
`process.job_run`/`job_item` tables used by both `penalties` and `cmir`, and
is placed top-level alongside `workflow_threads`/`processing_errors`.
"""

from fastapi import APIRouter, Depends

from app.api.dependencies import require_internal_api_key
from app.api.v1 import (
    admin,
    cmir,
    health,
    job_runs,
    ontology,
    ontology_insert,
    ontology_update,
    po_validation,
    processing_errors,
    workflow_threads,
)
from app.api.v1.common import master_data, purchase_orders
from app.api.v1.penalties import delivery_change_requests, mitigations, projections, rules

router = APIRouter()
router.include_router(health.router)

protected_router = APIRouter(dependencies=[Depends(require_internal_api_key)])
protected_router.include_router(cmir.router)
protected_router.include_router(po_validation.router)
protected_router.include_router(workflow_threads.router)
protected_router.include_router(processing_errors.router)
protected_router.include_router(master_data.router)
protected_router.include_router(purchase_orders.router)
protected_router.include_router(rules.router)
protected_router.include_router(projections.router)
protected_router.include_router(mitigations.router)
protected_router.include_router(delivery_change_requests.router)
protected_router.include_router(job_runs.router)
protected_router.include_router(ontology.router)
protected_router.include_router(ontology_update.router)
protected_router.include_router(ontology_insert.router)
protected_router.include_router(admin.router)

router.include_router(protected_router)
