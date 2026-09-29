"""Base schema for penalty summary outputs."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class PenaltySummaryOutputBase(BaseModel):
    """Common fields returned by every penalty-domain LLM summary call."""

    order_id: str
    as_of_date: date
    prompt_version: str
    model_name: str
    summary: str
