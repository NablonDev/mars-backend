"""Schema for the output of dispute-summary generation."""

from __future__ import annotations

from app.agents.penalties._summary_output import PenaltySummaryOutputBase


class DisputeSummaryOutput(PenaltySummaryOutputBase):
    """Output of dispute-summary generation; no fields beyond the shared base."""
