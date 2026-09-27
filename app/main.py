"""FastAPI application factory and application entry point."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dependencies import (
    build_ontology_insert_service,
    build_ontology_update_service,
    build_po_validation_service,
    build_service,
)
from app.api.router import router as api_v1_router
from app.core.config import Settings, get_settings
from app.core.container import Container
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import AccessLogMiddleware, RequestIdMiddleware
from app.db.session import Database
from app.ontology.config.validation import validate_mapping
from app.queue.factory import build_job_queue
from app.services.cmir.run_service import CmirRunService
from app.services.ontology import materialize_job
from app.services.ontology_insert.run_service import OntologyInsertRunService
from app.services.ontology_update.run_service import OntologyUpdateRunService
from app.services.po_validation.service import PoValidationService

logger = logging.getLogger(__name__)


async def _run_ontology_materialize_loop(interval_seconds: int) -> None:
    """Rebuilds the ontology graph once immediately (so the first request
    after startup doesn't see an empty graph), then again every
    `interval_seconds` until cancelled at shutdown. Runs `rebuild` (a
    blocking, synchronous function) on a worker thread via `asyncio.to_thread`
    so it never blocks the event loop the API is also using.

    `Container.build()` is called from inside the loop, not once up front,
    and inside the same try/except as the rebuild itself: like
    `build_service()`/`build_po_validation_service()` above, it opens a real
    Postgres-backed checkpointer, which tests deliberately avoid triggering
    by injecting fake services (see tests/unit/api/test_main_lifespan.py) --
    doing the build here means a Postgres-less environment logs and retries
    next interval instead of failing app startup outright.
    """
    while True:
        try:
            container = Container.build()
            await asyncio.to_thread(materialize_job.rebuild, container)
        except Exception:
            logger.exception("Ontology graph rebuild failed; will retry next interval.")
        await asyncio.sleep(interval_seconds)


def create_app(
    service: CmirRunService | None = None,
    po_service: PoValidationService | None = None,
    ontology_update_service: OntologyUpdateRunService | None = None,
    ontology_insert_service: OntologyInsertRunService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved.app.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Create process-wide DB and queue resources and dispose them on shutdown."""
        # Structural check of the ontology DB<->vocabulary mapping -- needs
        # no DB connection (reads Base.metadata + a local .ttl parse), so
        # it's safe and cheap to run unconditionally, synchronously, before
        # anything else starts. Deliberately allowed to raise and crash
        # startup here (unlike the periodic rebuild's own call to the same
        # function, wrapped in _run_ontology_materialize_loop's try/except)
        # -- there is no "last known-good graph" yet to fall back to on the
        # very first run, so starting with the mapping silently broken
        # would be worse than refusing to start at all.
        validate_mapping()

        app.state.database = Database(
            resolved.database.url,
            pool_size=resolved.database.pool_size,
            max_overflow=resolved.database.max_overflow,
            pool_timeout=resolved.database.pool_timeout,
        )
        app.state.job_queue = build_job_queue(resolved, app.state.database)
        # CMIR/PO-validation services carry their own composition root
        # (LangGraph + a shared Postgres checkpointer) -- built here unless a
        # test already injected a fake via create_app(service=...).
        if app.state.service is None:
            app.state.service = build_service()
        if app.state.po_service is None:
            app.state.po_service = build_po_validation_service()
        if app.state.ontology_update_service is None:
            app.state.ontology_update_service = build_ontology_update_service()
        if app.state.ontology_insert_service is None:
            app.state.ontology_insert_service = build_ontology_insert_service()

        ontology_task = asyncio.create_task(
            _run_ontology_materialize_loop(resolved.ontology.refresh_interval_seconds)
        )
        try:
            yield
        finally:
            ontology_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ontology_task
            dispatcher, _source = app.state.job_queue
            dispatcher.close()
            app.state.database.dispose()
            Container.close()

    app = FastAPI(
        title=resolved.app.project_name,
        version=resolved.app.version,
        lifespan=lifespan,
        docs_url="/docs" if resolved.app.docs_enabled else None,
        redoc_url="/redoc" if resolved.app.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.app.docs_enabled else None,
    )
    app.state.service = service
    app.state.po_service = po_service
    app.state.ontology_update_service = ontology_update_service
    app.state.ontology_insert_service = ontology_insert_service

    # Last-added middleware is outermost; request IDs must wrap access logging.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(api_v1_router, prefix="/api/v1")
    register_exception_handlers(app)

    return app


app = create_app()
