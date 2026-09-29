"""Tool definitions for penalty-mitigation-summary generation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field


class GetCarrierReliabilityDetailInput(BaseModel):
    """Input schema for get_carrier_reliability_detail: the carrier to look up."""

    carrier_id: str = Field(description="The carrier's business id.")


class GetActualPenaltiesForPurchaseOrderInput(BaseModel):
    """Empty input schema: get_actual_penalties_for_purchase_order takes no arguments."""


def build_penalty_mitigation_summary_tools(
    *,
    carrier_reliability: Callable[[str], dict[str, Any]],
    actual_penalties: Callable[[], list[dict[str, Any]]],
    order_status: str,
) -> list[BaseTool]:
    """Build the LLM-callable tools scoped to one mitigation-summary request.

    Closing over this order's lookups (and over order_status, since actual penalties
    exist only after delivery) keeps the model from reaching a different order.
    """

    @tool(args_schema=GetCarrierReliabilityDetailInput)
    def get_carrier_reliability_detail(carrier_id: str) -> dict[str, Any]:
        """Look up a carrier's historical reliability, for judging the FASTER_CARRIER option."""
        return carrier_reliability(carrier_id)

    @tool(args_schema=GetActualPenaltiesForPurchaseOrderInput)
    def get_actual_penalties_for_purchase_order() -> list[dict[str, Any]] | dict[str, Any]:
        """Look up actual penalties recorded for the current order.

        Returns an unavailable marker until the order reaches DELIVERED.
        """
        if order_status != "DELIVERED":
            return {"available": False, "reason": f"order_status is {order_status!r}, not DELIVERED"}

        return actual_penalties()

    return [
        get_carrier_reliability_detail,
        get_actual_penalties_for_purchase_order,
    ]
