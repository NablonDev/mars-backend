"""FastAPI application factory and application entry point."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.dependencies import build_po_validation_service, build_service
from app.api.router import router as api_v1_router
from app.core.config import Settings, get_settings
from app.core.container import Container
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import AccessLogMiddleware, RequestIdMiddleware
from app.db.session import Database
from app.queue.factory import build_job_queue
from app.services.cmir.service import CmirService
from app.services.po_validation.service import PoValidationService


def create_app(
    service: CmirService | None = None,
    po_service: PoValidationService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the FastAPI app, wiring middleware, routers, and exception handlers.

    `service`/`po_service` let tests inject fakes directly; left as None, the
    lifespan builds the real LangGraph-backed services on startup instead.
    """
    resolved = settings or get_settings()
    configure_logging(resolved.app.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Create process-wide DB and queue resources and dispose them on shutdown."""
        app.state.database = Database(
            resolved.database.url,
            pool_size=resolved.database.pool_size,
            max_overflow=resolved.database.max_overflow,
            pool_timeout=resolved.database.pool_timeout,
        )
        app.state.job_queue = build_job_queue(resolved, app.state.database)
        # CMIR/PO-validation services carry their own composition root (LangGraph
        # plus a shared Postgres checkpointer), built here unless a test already
        # injected a fake via create_app(service=...).
        if app.state.service is None:
            app.state.service = build_service()
        if app.state.po_service is None:
            app.state.po_service = build_po_validation_service()
        try:
            yield
        finally:
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

    # Last-added middleware is outermost; request IDs must wrap access logging.
    # CORSMiddleware added first (innermost), matching mars-bff's ordering.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(api_v1_router, prefix="/api/v1")
    register_exception_handlers(app)

    return app


app = create_app()
