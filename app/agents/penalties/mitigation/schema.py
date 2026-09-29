"""Schema for the output of the penalty mitigation summary generation."""

from __future__ import annotations

from app.agents.penalties._summary_output import PenaltySummaryOutputBase


class PenaltyMitigationSummaryOutput(PenaltySummaryOutputBase):
    """Output of mitigation-summary generation; no fields beyond the shared base."""
