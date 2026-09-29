"""Deterministic engine for adjudicating post-delivery penalty disputes.

Package layout mirrors `app.services.penalties.projection`:
- Pure (no SQLAlchemy/FastAPI imports): `types.py`, `engine.py`.
- I/O-performing (DB/repository access): `service.py`, `summary_service.py`.
"""

from app.services.penalties.dispute.engine import (
    ROUNDING_TOLERANCE,
    classify,
    compute_deadline,
    compute_is_late,
    compute_shortfall_units,
    price_violation,
    recompute_dispute,
)
from app.services.penalties.dispute.service import DisputeResolutionService
from app.services.penalties.dispute.summary_service import (
    DisputeSummaryOutputWithReuse,
    DisputeSummaryService,
)
from app.services.penalties.dispute.types import (
    DisputeCalculation,
    DisputeFacts,
    DisputeVerdict,
    InsufficientDataForDisputeError,
    UnsupportedDisputeCalcError,
)

__all__ = [
    "ROUNDING_TOLERANCE",
    "DisputeCalculation",
    "DisputeFacts",
    "DisputeResolutionService",
    "DisputeSummaryOutputWithReuse",
    "DisputeSummaryService",
    "DisputeVerdict",
    "InsufficientDataForDisputeError",
    "UnsupportedDisputeCalcError",
    "classify",
    "compute_deadline",
    "compute_is_late",
    "compute_shortfall_units",
    "price_violation",
    "recompute_dispute",
]
