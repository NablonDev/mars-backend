"""Typed contract for the data handed to the dispute-summary LLM call.

Contains the required dispute and order information used to generate a
dispute summary. Additional supporting details can be retrieved through the
dispute tools when needed.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class DisputeOrderContext(BaseModel):
    """Order-identifying fields the dispute is filed against."""

    order_id: str
    retailer_name: str
    sku_description: str


class DisputeSummaryContext(BaseModel):
    """Everything the model needs to narrate an already-resolved dispute verdict."""

    dispute_number: str
    reason_code: str
    claimed_amount: float
    computed_amount: float
    delta_amount: float
    verdict: str
    dispute_status: str
    analyzed_at: date
    order: DisputeOrderContext
