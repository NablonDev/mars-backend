"""Ranks mitigation actions against accepting the projected penalty as-is.

The candidate actions are speeding up production, splitting the shipment, and
switching to a faster carrier. This package holds the pure calculation engine
(`engine.py`, `types.py`, neither importing SQLAlchemy or FastAPI) alongside
the I/O-performing orchestration built on it (`service.py`,
`summary_service.py`).

Summary-service names are re-exported lazily through `__getattr__` (PEP 562)
rather than imported at the top of this file. `summary_service.py` imports
`app.repositories.penalties.mitigation`, which in turn imports
`app.services.penalties.mitigation.types` at module level, so a module-level
import of `.summary_service` here resumes that repository module's own import
through this partially-initialized package and fails. Deferring to first
attribute access keeps the same import surface for every caller.
"""

from app.services.penalties.mitigation.engine import MitigationEngine
from app.services.penalties.mitigation.types import MitigationInputs, MitigationOption, ShortageCause

__all__ = [
    "MitigationEngine",
    "MitigationInputs",
    "MitigationOption",
    "MitigationSummaryService",
    "PenaltyMitigationSummaryOutputWithReuse",
    "ShortageCause",
    "SummaryJob",
]

_LAZY_SUMMARY_SERVICE_EXPORTS = frozenset(
    {"MitigationSummaryService", "PenaltyMitigationSummaryOutputWithReuse", "SummaryJob"}
)


def __getattr__(name: str):
    if name in _LAZY_SUMMARY_SERVICE_EXPORTS:
        from app.services.penalties.mitigation import summary_service

        return getattr(summary_service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
