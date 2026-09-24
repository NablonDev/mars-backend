"""Health endpoint that verifies database connectivity."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from app.api.dependencies import get_database
from app.db.session import Database
from app.schemas.common import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse}},
)
def health(
    response: Response,
    database: Annotated[Database, Depends(get_database)],
) -> HealthResponse:
    """Check application health and database connectivity.

    Returns 503 with a degraded status instead of raising when the
    connectivity probe fails, so callers get a structured health payload
    either way.
    """
    try:
        with database.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        logger.warning("database health check failed", exc_info=True)
        response.status_code = 503
        return HealthResponse(status="degraded", database="unreachable")

    return HealthResponse(status="ok", database="ok")
