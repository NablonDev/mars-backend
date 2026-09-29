"""Tool definitions for dispute-summary generation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel


class GetRuleDetailInput(BaseModel):
    """Empty input schema: get_rule_detail takes no arguments."""


class GetFactsUsedInput(BaseModel):
    """Empty input schema: get_facts_used takes no arguments."""


class GetPriorDisputeHistoryInput(BaseModel):
    """Empty input schema: get_prior_dispute_history_for_purchase_order takes no arguments."""


def build_dispute_summary_tools(
    *,
    rule_detail: Callable[[], dict[str, Any]],
    facts_used: Callable[[], dict[str, Any]],
    prior_dispute_history: Callable[[], list[dict[str, Any]]],
) -> list[BaseTool]:
    """Build the LLM-callable tools scoped to one dispute-summary request.

    Closes over the current dispute's rule detail, computed facts, and prior
    dispute history so the model can call each tool with no arguments and
    always get data for the dispute already under narration, never one it
    could name itself.
    """

    @tool(args_schema=GetRuleDetailInput)
    def get_rule_detail() -> dict[str, Any]:
        """Return the penalty rule matched by the deterministic engine."""
        return rule_detail()

    @tool(args_schema=GetFactsUsedInput)
    def get_facts_used() -> dict[str, Any]:
        """Return the final facts used by the deterministic engine."""
        return facts_used()

    @tool(args_schema=GetPriorDisputeHistoryInput)
    def get_prior_dispute_history_for_purchase_order() -> list[dict[str, Any]]:
        """Return all prior disputes for the purchase order, oldest first."""
        return prior_dispute_history()

    return [get_rule_detail, get_facts_used, get_prior_dispute_history_for_purchase_order]
