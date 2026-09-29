"""Schema for the output of the penalty projection summary generation."""

from __future__ import annotations

from app.agents.penalties._summary_output import PenaltySummaryOutputBase


class PenaltyProjectionSummaryOutput(PenaltySummaryOutputBase):
    """Output of projection-summary generation; no fields beyond the shared base."""
