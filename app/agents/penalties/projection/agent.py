"""Bounded tool-calling loop for penalty projection summary generation."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from app.agents.penalties.projection.context import PenaltyProjectionSummaryContext
from app.agents.penalties.projection.schema import PenaltyProjectionSummaryOutput
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.exceptions import ExternalServiceError
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.utils.heartbeat import invoke_heartbeat
from app.utils.json_helpers import json_default, wrap_data

logger = logging.getLogger(__name__)

# Keep tool use bounded so a model cannot trigger an unbounded sequence of
# LLM and tool calls. The final LLM call occurs after this loop.
MAX_TOOL_ROUNDS = 4

# Error codes are selected by summary domain because the same generation
# machinery is shared by projection and mitigation summaries.
_UPSTREAM_FAILURE_CODES: dict[str, str] = {
    "projection": "PENALTY_PROJECTION_SUMMARY_UPSTREAM_FAILED",
    "mitigation": "PENALTY_MITIGATION_SUMMARY_UPSTREAM_FAILED",
}


class PenaltyProjectionAgent:
    """The projection-summary sub-domain's LLM tool-calling loop."""

    def __init__(
        self,
        *,
        llm: AzureOpenAIChatClient,
        agent_registry: AgentRegistryRepository,
        agent_code: str,
        summary_domain: str,
        upstream_failure_message: str,
    ) -> None:
        self._llm = llm
        self._agent_registry = agent_registry
        self._agent_code = agent_code
        self._summary_domain = summary_domain
        self._upstream_failure_message = upstream_failure_message

    def generate_projection_summary(
        self,
        context: PenaltyProjectionSummaryContext,
        *,
        order_id: str,
        as_of_date: date,
        tools: list[BaseTool],
        heartbeat: Callable[[], None] | None = None,
    ) -> PenaltyProjectionSummaryOutput:
        """Run the bounded tool-calling loop and return the projection-narration summary.

        Seeds the conversation with the active projection-agent's system prompt and
        the projection context wrapped as untrusted <DATA>, then lets the model call
        the supplied tools for up to MAX_TOOL_ROUNDS - 1 rounds before forcing a
        final, tool-free response. Invokes heartbeat (if given) before each LLM call
        so a long-running worker isn't reaped mid-generation. Raises
        ExternalServiceError if the provider call fails or the model returns no
        usable text.
        """
        active_agent = self._active_agent_row()
        messages: list[BaseMessage] = [
            SystemMessage(content=active_agent["system_prompt"]),
            HumanMessage(content=wrap_data(context.model_dump(mode="json"), default=json_default)),
        ]

        try:
            for round_number in range(1, MAX_TOOL_ROUNDS):
                # Heartbeat between LLM calls keeps long-running workers alive.
                invoke_heartbeat(heartbeat, order_id, logger)

                logger.info(
                    "Calling LLM for order_id=%s round=%s/%s",
                    order_id,
                    round_number,
                    MAX_TOOL_ROUNDS - 1,
                )

                started = time.monotonic()
                response = self._llm.invoke(messages, tools=tools)

                logger.info(
                    "Projection summary LLM round %s completed in %.1fs",
                    round_number,
                    time.monotonic() - started,
                )

                if not response.tool_calls:
                    break

                messages.append(response)
                tool_map = {tool.name: tool for tool in tools}

                for tool_call in response.tool_calls:
                    tool = tool_map.get(tool_call["name"])
                    if tool is None:
                        raise ValueError(f"Unknown tool returned by model: {tool_call['name']!r}")

                    result = tool.invoke(tool_call["args"])
                    messages.append(
                        ToolMessage(
                            content=wrap_data(result, default=json_default),
                            tool_call_id=tool_call["id"],
                        )
                    )

            final_response = self._llm.invoke(messages)

        except Exception as exc:
            raise ExternalServiceError(
                code=_UPSTREAM_FAILURE_CODES[self._summary_domain],
                message=self._upstream_failure_message,
                details={
                    "detail": (
                        f"projection summary generation failed after {MAX_TOOL_ROUNDS} rounds "
                        f"limit for order_id={order_id!r}, as_of_date={as_of_date.isoformat()}: {exc}"
                    )
                },
            ) from exc

        if not final_response.content:
            raise ExternalServiceError(
                code=_UPSTREAM_FAILURE_CODES[self._summary_domain],
                message=self._upstream_failure_message,
                details={
                    "detail": (
                        f"projection summary generation failed: Model returned no summary "
                        f"for order_id={order_id!r}, as_of_date={as_of_date.isoformat()}."
                    )
                },
            )

        if not isinstance(final_response.content, str):
            raise ExternalServiceError(
                code=_UPSTREAM_FAILURE_CODES[self._summary_domain],
                message=self._upstream_failure_message,
                details={
                    "detail": (
                        f"projection summary generation failed: Model returned non-text final "
                        f"content for order_id={order_id!r}, as_of_date={as_of_date.isoformat()}."
                    )
                },
            )

        return PenaltyProjectionSummaryOutput(
            order_id=order_id,
            as_of_date=as_of_date,
            prompt_version=active_agent["prompt_version"],
            model_name=self._llm.model_name,
            summary=final_response.content,
        )

    def _active_agent_row(self) -> dict:
        """Fetch the active process.agent row for this agent code, or raise ExternalServiceError."""
        active = self._agent_registry.get_active(self._agent_code)
        if active is None:
            raise ExternalServiceError(
                code=_UPSTREAM_FAILURE_CODES[self._summary_domain],
                message=self._upstream_failure_message,
                details={
                    "detail": f"No active process.agent row registered for agent_code={self._agent_code!r}."
                },
            )
        return active
