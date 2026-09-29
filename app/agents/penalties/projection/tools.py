"""Tool definitions for penalty-projection-summary generation."""

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


class GetTierBandsForRuleInput(BaseModel):
    """Input schema for get_tier_bands_for_rule: the penalty rule to look up."""

    rule_id: str = Field(description="The penalty rule's business id.")


def build_penalty_projection_summary_tools(
    *,
    carrier_reliability: Callable[[str], dict[str, Any]],
    actual_penalties: Callable[[], list[dict[str, Any]]],
    tier_bands: Callable[[str], dict[str, Any]],
    order_status: str,
) -> list[BaseTool]:
    """Build the LLM-callable tools scoped to one projection-summary request.

    Closing over this order's lookups (and over order_status, since actual penalties
    exist only after delivery) keeps the model from reaching a different order.
    """

    @tool(args_schema=GetCarrierReliabilityDetailInput)
    def get_carrier_reliability_detail(carrier_id: str) -> dict[str, Any]:
        """Look up a carrier's historical reliability, which drives its delay probability."""
        return carrier_reliability(carrier_id)

    @tool(args_schema=GetActualPenaltiesForPurchaseOrderInput)
    def get_actual_penalties_for_purchase_order() -> list[dict[str, Any]] | dict[str, Any]:
        """Look up actual penalties recorded for the current order.

        Returns an unavailable marker until the order reaches DELIVERED.
        """
        if order_status != "DELIVERED":
            return {"available": False, "reason": f"order_status is {order_status!r}, not DELIVERED"}

        return actual_penalties()

    @tool(args_schema=GetTierBandsForRuleInput)
    def get_tier_bands_for_rule(rule_id: str) -> dict[str, Any]:
        """Look up the rate tiers for a penalty rule whose calc_type is tiered, not flat."""
        return tier_bands(rule_id)

    return [
        get_carrier_reliability_detail,
        get_actual_penalties_for_purchase_order,
        get_tier_bands_for_rule,
    ]
